"""v0.10.1 测试:keyless 通道跳过 / HTTP 错误解读 / key_env 防呆校验。

运行: python -m pytest tests/ -v
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.ledger import Ledger
from crewos.router import AllChannelsDown, Router

# 两条远程通道都没配 key → 应全部跳过并上报(不是 mock)
NOKEY = """
model: "deepseek-v4-pro"
channels:
  - {name: deepseek-direct, endpoint: "https://api.deepseek.com/v1", key_env: DEEPSEEK_KEY}
  - {name: openrouter, endpoint: "https://openrouter.ai/api/v1", key_env: OPENROUTER_KEY}
pricing: {input_per_m: 0.27, output_per_m: 1.1}
"""


def make_ws(tmp: Path, yml: str) -> Path:
    ws = tmp / "agents" / "analyst"
    (ws / "memory").mkdir(parents=True)
    (ws / "role.md").write_text("# analyst", encoding="utf-8")
    (ws / "provider.yaml").write_text(yml, encoding="utf-8")
    return tmp / "agents"


def test_keyless_channels_skipped_with_clear_reason(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp, NOKEY), Ledger(tmp / "l.db"))
        tid = router.ledger.new_task("无 key 测试")
        try:
            router.dispatch("analyst", "算 2+2", task_id=tid)
            assert False, "应抛 AllChannelsDown"
        except AllChannelsDown as e:
            assert "未配置" in str(e)        # 错误里点明是没配 key
        fos = [e for e in router.ledger.task_events(tid) if e["type"] == "failover"]
        # 两条通道都因无 key 被跳过,且 reason 指向供应商面板
        assert len(fos) == 2
        assert all("未配置 key" in __import__("json").loads(f["payload"])["reason"] for f in fos)


def test_http_error_detail_decodes_status():
    import io
    import urllib.error

    from crewos.router import _http_error_detail
    body = b'{"error":{"message":"Model Not Exist"}}'
    e = urllib.error.HTTPError("http://x/v1/chat/completions", 400, "Bad Request",
                               {}, io.BytesIO(body))
    d = _http_error_detail(e)
    assert "400" in d and "模型 ID" in d and "Model Not Exist" in d
    e2 = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
    assert "401" in _http_error_detail(e2) and "key" in _http_error_detail(e2)


def test_validate_rejects_key_in_key_env():
    from crewos.web_server import validate_config
    bad = ("model: m\nchannels:\n  - {name: deepseek, endpoint: 'https://api.deepseek.com/v1', "
           "key_env: DEEPSEEK_KEYsk-33fa9e}\npricing: {input_per_m: 1, output_per_m: 2}\n")
    err = validate_config("agents/analyst/provider.yaml", bad)
    assert err and "key_env" in err
    good = ("model: m\nchannels:\n  - {name: deepseek, endpoint: 'https://api.deepseek.com/v1', "
            "key_env: DEEPSEEK_KEY}\npricing: {input_per_m: 1, output_per_m: 2}\n")
    assert validate_config("agents/analyst/provider.yaml", good) is None

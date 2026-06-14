"""v0.12 测试:CEO 简单任务直接答 / 新增·删除 agent / 模型列表解析。

运行: python -m pytest tests/ -v
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos import ceo as ceomod
from crewos.ceo import orchestrate, parse_direct
from crewos.ledger import Ledger
from crewos.router import Router

MOCK = """
model: "mock-model"
channels:
  - {name: mock_main, endpoint: "mock://", key_env: ""}
pricing: {input_per_m: 1.0, output_per_m: 2.0}
"""


def make_ws(tmp: Path, names=("writer",)) -> Path:
    for n in names:
        ws = tmp / "agents" / n
        (ws / "memory").mkdir(parents=True)
        (ws / "role.md").write_text(f"# {n}", encoding="utf-8")
        (ws / "provider.yaml").write_text(MOCK, encoding="utf-8")
    return tmp / "agents"


def test_parse_direct():
    assert parse_direct('{"direct":"你好!"}') == "你好!"
    assert parse_direct('```json\n{"direct":"2+2=4"}\n```') == "2+2=4"
    assert parse_direct('[{"agent":"writer","instruction":"x"}]') is None  # 是派单不是直答
    assert parse_direct("随便聊聊") is None


def test_ceo_answers_simple_directly(monkeypatch):
    """CEO 判定简单任务直接答 → task_done(direct),不派给任何 agent。"""
    monkeypatch.setattr(ceomod, "_call_openai_compatible",
                        lambda *a, **k: {"content": '{"direct":"你好,我是 CrewOS 总指挥。"}',
                                         "tokens_in": 10, "tokens_out": 8})
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        res = orchestrate(router, "m", "mock://", "", "你好")
        assert res.get("direct") and res["plan"] == []
        types = [e["type"] for e in router.ledger.task_events(res["task_id"])]
        assert "task_done" in types
        assert "task_assign" not in [t for t in types if t == "task_assign"][1:]  # 没有派给 agent 的二次 assign
        # 没有任何 agent 产出
        assert not any(e["from_agent"] == "writer" for e in router.ledger.task_events(res["task_id"]))


def test_new_and_delete_agent():
    import crewos.web_server as ws
    from crewos.web_server import NewAgentReq, api_delete_agent, api_new_agent
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "agents").mkdir()
        (tmp / "config").mkdir()
        ws.ROOT = tmp
        r = api_new_agent(NewAgentReq(name="Designer", role="海报设计"))
        # 名称小写化 + 落盘
        assert (tmp / "agents" / "designer" / "provider.yaml").exists()
        assert (tmp / "agents" / "designer" / "role.md").exists()
        assert (tmp / "agents" / "designer" / "memory" / "lessons.md").exists()
        # 保留名/重复名被拒
        assert api_new_agent(NewAgentReq(name="ceo")).status_code == 422
        assert api_new_agent(NewAgentReq(name="designer")).status_code == 409
        assert api_new_agent(NewAgentReq(name="x")).status_code == 422  # 太短
        # 删除
        out = api_delete_agent("designer")
        assert out["ok"] and not (tmp / "agents" / "designer").exists()
        assert api_delete_agent("designer").status_code == 404


def test_list_models_parses(monkeypatch):
    import io
    import urllib.request

    from crewos.router import list_models
    body = b'{"data":[{"id":"deepseek-v4-pro"},{"id":"deepseek-v4-flash"},{"id":"deepseek-chat"}]}'

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp(body))
    models = list_models("https://api.deepseek.com/v1", "sk-x")
    assert models == ["deepseek-chat", "deepseek-v4-flash", "deepseek-v4-pro"]  # 去重+排序

"""v0.14.6 测试:夜间评审 Batch F —— 补齐高价值未测关键路径。

覆盖:出站 DLP 拦截、非 length 空产出、多代码块落盘、workspace .env 读写、
台账非法事件类型、U 召回注入编排。全部临时目录,不碰真实环境。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.ledger import Ledger
from crewos.router import Router

MOCK_PROVIDER = ('model: "m"\nchannels:\n  - {name: mock, endpoint: "mock://", key_env: ""}\n'
                 'pricing: {input_per_m: 1.0, output_per_m: 2.0}\n')


def _ws(tmp: Path, names=("writer",)) -> Path:
    for n in names:
        (tmp / "agents" / n / "memory").mkdir(parents=True)
        (tmp / "agents" / n / "role.md").write_text(f"# {n}", encoding="utf-8")
        (tmp / "agents" / n / "provider.yaml").write_text(MOCK_PROVIDER, encoding="utf-8")
    return tmp / "agents"


def test_outbound_dlp_blocks_and_redacts(monkeypatch):
    """模型回吐了密钥 → 出站 DLP 拦截:脱敏内容 + 记 dlp_block 事件。"""
    from crewos import router as rmod
    monkeypatch.setattr(rmod, "_call_openai_compatible", lambda *a, **k: {
        "content": "这是你的 key: sk-ant-abcdefghij0123456789KLMNOP 用它登录",
        "finish_reason": "stop", "tokens_in": 10, "tokens_out": 12})
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = Router(_ws(tmp), Ledger(tmp / "l.db"))
        tid = r.ledger.new_task("t")
        out = r.dispatch("writer", "给我 key", task_id=tid)
        assert out["dlp_blocked"] is True
        assert "sk-ant-abcdefghij" not in out["content"]   # 原文被抹
        types = [e["type"] for e in r.ledger.task_events(tid)]
        assert "dlp_block" in types


def test_empty_output_non_length_is_flagged(monkeypatch):
    """空产出但 finish≠length(token 远没用满)→ 标 empty,且原因不甩锅 max_tokens。"""
    from crewos import router as rmod
    monkeypatch.setattr(rmod, "_call_openai_compatible", lambda *a, **k: {
        "content": "", "finish_reason": "stop", "tokens_in": 10, "tokens_out": 5})
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = Router(_ws(tmp), Ledger(tmp / "l.db"))
        out = r.dispatch("writer", "做事", task_id=r.ledger.new_task("t"))
        assert out["empty"] is True
        assert "max_tokens" not in out["empty_reason"]   # 不是 token 上限问题


def test_save_deliverable_multiple_blocks():
    """多代码块 → 编号落盘(coder-1.html / coder-2.css)。"""
    from crewos.ceo import save_deliverable
    with tempfile.TemporaryDirectory() as d:
        content = ("游戏:\n```html\n<!DOCTYPE html><body>X</body>\n```\n"
                   "样式:\n```css\nbody{margin:0}\n```")
        paths = save_deliverable(d, "t1", "coder", content)
        names = {p.split("/")[-1] for p in paths}
        assert "coder.md" in names
        assert "coder-1.html" in names and "coder-2.css" in names


def test_workspace_env_roundtrip(monkeypatch):
    """write_env 写 .env(600),load_env 载入 os.environ;不覆盖已存在的环境变量。"""
    import os
    from crewos.workspace import load_env, write_env
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        write_env(ws, {"DEEPSEEK_KEY": "sk-test-123", "ZHIPU_KEY": "zk-1"})
        env_file = ws / ".env"
        assert env_file.exists()
        assert oct(env_file.stat().st_mode)[-3:] == "600"   # 权限收紧
        for k in ("DEEPSEEK_KEY", "ZHIPU_KEY"):
            monkeypatch.delenv(k, raising=False)
        n = load_env(ws)
        assert n >= 2 and os.environ["DEEPSEEK_KEY"] == "sk-test-123"


def test_ledger_rejects_unknown_event_type():
    import pytest
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        with pytest.raises(ValueError):
            led.log("task_x", "not_a_real_type", "sys")


def test_umemory_recall_injected_into_orchestrate(monkeypatch):
    """orchestrate 把 U 召回拼进规划提示词。"""
    from crewos import ceo as ceomod
    captured = {}

    def fake_call(ch, model, messages, temperature, max_tokens, **k):
        captured["user"] = messages[-1]["content"]
        return {"content": '{"direct":"ok"}', "finish_reason": "stop",
                "tokens_in": 5, "tokens_out": 3}

    monkeypatch.setattr(ceomod, "_call_openai_compatible", fake_call)
    monkeypatch.setattr(ceomod.umemory, "recall", lambda *a, **k: "<<U_MEMORY>>钩子内容<<U_MEMORY_END>>")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = Router(_ws(tmp), Ledger(tmp / "l.db"))
        ceomod.orchestrate(r, "m", "mock://", "", "随便", root=str(tmp), settings={})
        assert "U_MEMORY" in captured.get("user", "")   # 召回确实注入了规划提示词


def test_budget_rejects_when_worst_case_overshoots():
    """单次派单上限保护:剩余预算不足以覆盖最坏成本 → 拒发(不截断),记 budget_block。"""
    import pytest
    from crewos.router import Router, BudgetExceeded
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = Router(_ws(tmp), Ledger(tmp / "l.db"))
        tid = r.ledger.new_task("t")
        # 上限 $0.005 远小于一次 8192-token 输出的最坏成本 → 应拒发
        with pytest.raises(BudgetExceeded):
            r.dispatch("writer", "做点事", task_id=tid, budget_usd=0.005)
        types = [e["type"] for e in r.ledger.task_events(tid)]
        assert "budget_block" in types

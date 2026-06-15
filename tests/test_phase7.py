"""v0.10 测试:CEO 计划解析 / Web 端编排(mock 降级)。

运行: python -m pytest tests/ -v
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.ceo import orchestrate, parse_clarify, parse_plan
from crewos.ledger import Ledger
from crewos.router import Router

MOCK = """
model: "mock-model"
channels:
  - {name: mock_main, endpoint: "mock://", key_env: ""}
pricing: {input_per_m: 1.0, output_per_m: 2.0}
"""


def make_ws(tmp: Path, names=("writer", "researcher")) -> Path:
    for n in names:
        ws = tmp / "agents" / n
        (ws / "memory").mkdir(parents=True)
        (ws / "role.md").write_text(f"# {n}", encoding="utf-8")
        (ws / "provider.yaml").write_text(MOCK, encoding="utf-8")
    return tmp / "agents"


def test_parse_plan_variants():
    valid = ["writer", "coder"]
    # 纯 JSON
    assert parse_plan('[{"agent":"writer","instruction":"写文案"}]', valid) == \
        [{"agent": "writer", "instruction": "写文案"}]
    # 带 ```json 代码块 + 前后噪声
    txt = '好的,计划如下:\n```json\n[{"agent":"coder","instruction":"写函数"}]\n```\n完成'
    assert parse_plan(txt, valid) == [{"agent": "coder", "instruction": "写函数"}]
    # 过滤非法 agent
    assert parse_plan('[{"agent":"ghost","instruction":"x"},{"agent":"writer","instruction":"y"}]',
                      valid) == [{"agent": "writer", "instruction": "y"}]
    # 非 JSON → 空
    assert parse_plan("我觉得应该让 writer 去做", valid) == []
    assert parse_plan("", valid) == []
    # v0.14.1 容错:便宜模型把单个派单写成裸对象(非数组)→ 当 1 单解析
    assert parse_plan('{"agent":"coder","instruction":"写函数"}', valid) == \
        [{"agent": "coder", "instruction": "写函数"}]
    # 裸对象 + ```json 包裹 + 前导句
    assert parse_plan('好的:\n```json\n{"agent":"writer","instruction":"写"}\n```', valid) == \
        [{"agent": "writer", "instruction": "写"}]
    # 单对象但 agent 非法 → 空
    assert parse_plan('{"agent":"ghost","instruction":"x"}', valid) == []


def test_parse_clarify_and_estimate():
    # #3 含糊→先澄清:解析 {"clarify":...};direct/数组不误判为澄清
    assert parse_clarify('{"clarify":"做给谁?要什么功能?"}') == "做给谁?要什么功能?"
    assert parse_clarify('```json\n{"clarify":"补一句"}\n```') == "补一句"
    assert parse_clarify('{"direct":"你好"}') is None
    assert parse_clarify('[{"agent":"coder","instruction":"x"}]') is None
    assert parse_clarify("普通文字") is None
    # #1 派单前成本预估:最坏成本 = 输入(chars//4)*price_in + max_tokens*price_out
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp), Ledger(tmp / "l.db"), default_task_budget_usd=2.0)
        e = router.estimate("writer", chars=400, max_tokens=1000)
        # (100*1.0 + 1000*2.0)/1e6 = 0.0021
        assert abs(e["worst_usd"] - 0.0021) < 1e-9
        assert e["model"] == "mock-model" and e["cap_usd"] == 2.0
        assert e["est_in_tok"] == 100 and e["max_out_tok"] == 1000


def test_orchestrate_clarifies_on_vague_goal(monkeypatch):
    """CEO 判含糊 → 发 clarify 事件、不派任何单。"""
    import crewos.ceo as ceomod
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        monkeypatch.setattr(ceomod, "_call_openai_compatible",
                            lambda *a, **k: {"content": '{"clarify":"做给谁?什么功能?"}',
                                             "tokens_in": 1, "tokens_out": 1})
        # 用非建造类含糊目标,避免触发 Tavily 调研(与 test_orchestrate_degrades_on_mock 同口径)
        res = orchestrate(router, "x-model", "http://x/v1", "", "这个你看着办", root=str(tmp))
        assert res.get("clarify") == "做给谁?什么功能?" and res["plan"] == []
        types = [e["type"] for e in router.ledger.task_events(res["task_id"])]
        assert "clarify" in types                  # 发了澄清事件
        assert "task_result" not in types and "task_done" not in types  # 没派任何单、没结案


def test_orchestrate_degrades_on_mock():
    """mock 模型返回的不是合法计划 JSON → 降级为单点派单,但全程进台账、有结案。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        res = orchestrate(router, "mock-model", "mock://", "", "写一篇小红书文案",
                          fallback_agent="writer", root=str(tmp))
        assert res["degraded"] is True
        assert res["plan"] == [{"agent": "writer", "instruction": "写一篇小红书文案"}]
        types = [e["type"] for e in router.ledger.task_events(res["task_id"])]
        assert "task_assign" in types        # user→ceo + ceo→writer
        assert "task_result" in types        # writer 产出
        assert "task_done" in types          # CEO 结案
        # 完整产出落盘为可取回的交付文件
        assert res["deliverables"] and (tmp / res["deliverables"][0]).exists()
        # 兜底派单确实跑了 writer
        assert any(e["from_agent"] == "writer" and e["type"] == "task_result"
                   for e in router.ledger.task_events(res["task_id"]))

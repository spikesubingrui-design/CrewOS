"""v0.10 测试:CEO 计划解析 / Web 端编排(mock 降级)。

运行: python -m pytest tests/ -v
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.ceo import orchestrate, parse_plan
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

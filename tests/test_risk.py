"""风险分级引擎 + 错题本自动写入测试(无需 API key)。

覆盖:L0-L2 即时放行、L3 倒计时自动放行、L3/L4 人工裁决、
未登记动作按 L3、错题本追加。
运行: python -m pytest tests/ -v
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.ledger import Ledger
from crewos.memory import append_lesson
from crewos.risk import RiskEngine

ACTIONS = """
actions:
  read_stuff: {risk: 0}
  save_draft: {risk: 1}
  call_api: {risk: 2}
  push_msg: {risk: 3, auto_approve_seconds: 1}
  publish: {risk: 4}
"""


def make_ws(tmp: Path) -> Path:
    ws = tmp / "agents" / "tester"
    (ws / "memory").mkdir(parents=True)
    (ws / "actions.yaml").write_text(ACTIONS, encoding="utf-8")
    return tmp / "agents"


def make_engine(tmp: Path) -> RiskEngine:
    return RiskEngine(make_ws(tmp), Ledger(tmp / "l.db"))


def test_low_risk_instant_approval():
    with tempfile.TemporaryDirectory() as d:
        eng = make_engine(Path(d))
        for action in ("read_stuff", "save_draft", "call_api"):
            r = eng.request("tester", action, "测试")
            assert r["approved"] and r["status"] == "approved"
        types = [row["type"] for row in eng.ledger._conn.execute(
            "SELECT type FROM events ORDER BY ts")]
        assert types == ["risk_action"] * 3


def test_l3_countdown_auto_approves():
    with tempfile.TemporaryDirectory() as d:
        eng = make_engine(Path(d))
        r = eng.request("tester", "push_msg", "推送消息", task_id="t1")
        assert not r["approved"] and r["status"] == "pending"
        assert eng.check(r["approval_id"])["seconds_left"] <= 1
        time.sleep(1.1)
        final = eng.check(r["approval_id"])
        assert final["status"] == "auto_approved" and final["approved"]


def test_l3_can_be_denied_before_deadline():
    with tempfile.TemporaryDirectory() as d:
        eng = make_engine(Path(d))
        r = eng.request("tester", "push_msg", "推送消息")
        res = eng.decide(r["approval_id"], approve=False)
        assert res["status"] == "denied" and not res["approved"]
        time.sleep(1.1)  # 已裁决的单不会被倒计时翻案
        assert eng.check(r["approval_id"])["status"] == "denied"


def test_l4_blocks_until_user_decides():
    with tempfile.TemporaryDirectory() as d:
        eng = make_engine(Path(d))
        r = eng.request("tester", "publish", "发布内容")
        assert r["status"] == "pending" and r["deadline_ts"] is None
        assert eng.pending()[0]["id"] == r["approval_id"]
        res = eng.decide(r["approval_id"], approve=True)
        assert res["status"] == "approved" and res["approved"]
        assert eng.decide(r["approval_id"], True)["error"] == "already_decided"


def test_unknown_action_defaults_to_l3():
    with tempfile.TemporaryDirectory() as d:
        eng = make_engine(Path(d))
        r = eng.request("tester", "never_registered", "未知动作")
        assert r["risk"] == 3 and r["status"] == "pending"


def test_append_lesson():
    with tempfile.TemporaryDirectory() as d:
        agents = make_ws(Path(d))
        f = append_lesson(agents, "tester", "hook 太弱 → 用反差开头", task_id="t9", round=1)
        text = f.read_text(encoding="utf-8")
        assert "t9 R1" in text and "hook 太弱" in text
        append_lesson(agents, "tester", "第二条")
        assert f.read_text(encoding="utf-8").count("\n- 2") == 2
        try:
            append_lesson(agents, "ghost", "x")
            assert False, "未知 agent 应报错"
        except FileNotFoundError:
            pass

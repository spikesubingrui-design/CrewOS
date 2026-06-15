"""v0.5 测试:自动检查层 / 错题本检索化 / 预算提额 / 心跳缓存 / 配置校验 / token。

运行: python -m pytest tests/ -v
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.checks import parse_specs, run_checks
from crewos.ledger import Ledger
from crewos.memory import select_lessons
from crewos.router import BudgetExceeded, Router

MOCK_ONLY = """
model: "mock-model"
channels:
  - {name: mock_main, endpoint: "mock://", key_env: ""}
pricing: {input_per_m: 1.0, output_per_m: 2.0}
"""


def make_ws(tmp: Path, lessons: str = "") -> Path:
    ws = tmp / "agents" / "tester"
    (ws / "memory").mkdir(parents=True)
    (ws / "role.md").write_text("# Tester", encoding="utf-8")
    (ws / "provider.yaml").write_text(MOCK_ONLY, encoding="utf-8")
    if lessons:
        (ws / "memory" / "lessons.md").write_text(lessons, encoding="utf-8")
    return tmp / "agents"


# ---------- 自动检查层 ----------

def test_checks_each_type():
    text = "# 标题\n这是一篇带链接的文档 https://a.com 与 https://b.com 共两个来源"
    rep = run_checks(text, [
        {"type": "word_count", "min": 10, "max": 100},
        {"type": "must_include", "values": ["标题", "来源"]},
        {"type": "forbid", "values": ["敬请期待"]},
        {"type": "must_match", "pattern": "^# "},
        {"type": "min_links", "count": 2},
    ])
    assert rep["passed"] and not rep["failures"]
    rep2 = run_checks(text, [
        {"type": "word_count", "min": 500},
        {"type": "must_include", "values": ["CTA"]},
        {"type": "forbid", "values": ["链接"]},
        {"type": "min_links", "count": 3},
        {"type": "nonsense"},
    ])
    assert not rep2["passed"] and len(rep2["failures"]) == 5


def test_checks_json_parseable():
    assert run_checks('{"a": 1}', [{"type": "json_parseable"}])["passed"]
    assert run_checks('```json\n{"a": 1}\n```', [{"type": "json_parseable"}])["passed"]
    assert not run_checks("不是 json", [{"type": "json_parseable"}])["passed"]


def test_parse_specs_tolerates_garbage():
    assert parse_specs("") == []
    assert parse_specs('{"type":"word_count","min":1}') == [{"type": "word_count", "min": 1}]
    bad = parse_specs("not json at all")
    assert bad and not run_checks("x", bad)["passed"]   # 非法规格 → 失败项,不抛异常


def test_dispatch_returns_check_report():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        r = router.dispatch("tester", "写问候", checks=[
            {"type": "must_include", "values": ["不可能出现的词xyz"]}])
        assert not r["checks"]["passed"]
        result_ev = [e for e in router.ledger.task_events(r["task_id"])
                     if e["type"] == "task_result"][0]
        payload = json.loads(result_ev["payload"])
        assert payload["checks_passed"] is False and payload["check_failures"]


# ---------- 错题本检索化 ----------

def test_select_lessons_topn():
    lessons = "# 错题本\n" + "\n".join(
        [f"- 2026-01-0{i} | t{i} | 无关教训{i}" for i in range(1, 6)]
        + ["- 2026-02-01 | t9 | 小红书 hook 必须用反差开头"])
    out = select_lessons(lessons, "写一篇小红书文案,注意 hook", top_n=3)
    assert "反差开头" in out and "选注 3/6" in out
    few = "# 错题本\n- a\n- b"
    assert select_lessons(few, "任何询问") == few.strip()   # 条目少时全保留


# ---------- 预算提额 ----------

def test_budget_override_unblocks():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp), Ledger(tmp / "l.db"),
                        default_task_budget_usd=0.000001)
        tid = router.ledger.new_task("提额测试")
        # v0.14.8 单次上限保护:微小预算连一次派单的最坏成本都盖不住 → 直接拒发(不截断)
        try:
            router.dispatch("tester", "应熔断", task_id=tid)
            assert False
        except BudgetExceeded:
            pass
        router.ledger.log(tid, "budget_override", "user",
                          payload={"budget_usd": 5.0})
        r = router.dispatch("tester", "提额后续跑", task_id=tid)   # 提额后不再熔断
        assert r["task_id"] == tid
        assert router.task_budget_override(tid) == 5.0


# ---------- 心跳缓存 ----------

def test_heartbeat_cache_and_transition():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        assert router.heartbeat("tester") == {"mock_main": True}   # 实探并写缓存
        conn = router.ledger._conn
        conn.execute("UPDATE heartbeats SET ok=0")                  # 伪造缓存说挂了
        conn.commit()
        assert router.heartbeat("tester", max_age=60) == {"mock_main": False}  # 读缓存
        assert router.heartbeat("tester") == {"mock_main": True}   # 实探 → 翻转
        evs = conn.execute(
            "SELECT payload FROM events WHERE type='status_update'").fetchall()
        assert any("恢复在线" in r["payload"] for r in evs)


# ---------- 配置校验 / token ----------

def test_validate_config():
    from crewos.web_server import _authed, validate_config
    assert validate_config("agents/x/provider.yaml", "model: m\nchannels:\n  - {name: a, endpoint: e}\npricing: {input_per_m: 1, output_per_m: 2}\n") is None
    assert "model" in validate_config("agents/x/provider.yaml", "channels: []\n")
    assert "channel" in validate_config("agents/x/provider.yaml", "model: m\nchannels: []\npricing: {input_per_m: 1, output_per_m: 2}\n")
    assert "YAML" in validate_config("agents/x/actions.yaml", "actions: {bad: [unclosed\n")
    assert "risk" in validate_config("agents/x/actions.yaml", "actions:\n  pub: {risk: 9}\n")
    assert validate_config("config/crontab.yaml", "jobs:\n  - {name: a, schedule: '* * * * *', agent: x, instruction: y}\n") is None
    assert "schedule" in validate_config("config/crontab.yaml", "jobs:\n  - {name: a, schedule: 'bad', agent: x, instruction: y}\n") or \
           validate_config("config/crontab.yaml", "jobs:\n  - {name: a, schedule: 'bad', agent: x, instruction: y}\n")
    assert validate_config("CrewOS.md", "随便写,md 不校验") is None
    assert _authed("", None) and _authed("t", "t") and not _authed("t", "wrong")

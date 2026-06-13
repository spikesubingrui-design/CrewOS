"""v0.6 测试:Silent Watchdog / agent 暂停门禁 + 软预算 / doctor / 行为评估。

运行: python -m pytest tests/ -v
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.behavioral import DEFAULT_CASES, run_suite, to_promptfoo
from crewos.doctor import diagnose
from crewos.ledger import Ledger
from crewos.router import AgentPaused, BudgetExceeded, Router
from crewos.watchdog import Watchdog, scan_stalled

MOCK = """
model: "mock-model"
channels:
  - {name: mock_main, endpoint: "mock://", key_env: ""}
pricing: {input_per_m: 1.0, output_per_m: 2.0}
"""


def make_ws(tmp: Path) -> Path:
    ws = tmp / "agents" / "tester"
    (ws / "memory").mkdir(parents=True)
    (ws / "role.md").write_text("# Tester", encoding="utf-8")
    (ws / "provider.yaml").write_text(MOCK, encoding="utf-8")
    return tmp / "agents"


# ---------- Watchdog ----------

def test_watchdog_detects_stalled_and_dedupes():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        tid = led.new_task("停滞任务")
        led.log(tid, "task_assign", "ceo", "writer")
        now = time.time()
        # 刚派单,未到阈值 → 不算停滞
        assert scan_stalled(led, 300, 900, now=now) == []
        # 10 分钟后 → suspicious
        wd = Watchdog(led, suspicious_minutes=5, critical_minutes=15)
        reported = wd.tick(now=now + 600)
        assert len(reported) == 1 and reported[0]["level"] == "suspicious"
        # 同级别不重复上报
        assert wd.tick(now=now + 700) == []
        # 20 分钟后升级 critical(新级别,再报一次)
        rep2 = wd.tick(now=now + 1200)
        assert len(rep2) == 1 and rep2[0]["level"] == "critical"
        wd_events = [e for e in led.task_events(tid) if e["type"] == "watchdog"]
        assert len(wd_events) == 2


def test_watchdog_ignores_progressed_tasks():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        tid = led.new_task("有产出的任务")
        led.log(tid, "task_assign", "ceo", "writer")
        led.log(tid, "task_result", "writer", "ceo", model="m", cost_usd=0.01)
        # 最近事件是 task_result,不算停滞
        assert scan_stalled(led, 1, 2, now=time.time() + 9999) == []


# ---------- agent 暂停门禁 + 软预算 ----------

def test_agent_pause_blocks_dispatch():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        assert r.agent_status("tester") == "active"
        r.pause_agent("tester", "测试暂停")
        assert r.agent_status("tester") == "paused"
        assert "tester" in r.paused_agents()
        try:
            r.dispatch("tester", "应被拒")
            assert False, "暂停态应拒绝派单"
        except AgentPaused:
            pass
        r.resume_agent("tester")
        assert r.agent_status("tester") == "active"
        assert r.dispatch("tester", "恢复后可派")["task_id"]


def test_soft_budget_warn_then_hard_block():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # cap=0.001;预存 0.0009(=90%)→ 下一发落在 [80%,100%) 软线窗口
        r = Router(make_ws(tmp), Ledger(tmp / "l.db"), default_task_budget_usd=0.001)
        tid = r.ledger.new_task("预算软硬线")
        r.ledger.log(tid, "task_result", "tester", "ceo", model="m", cost_usd=0.0009)
        r.dispatch("tester", "软线内一发", task_id=tid)        # 入口 90%,不熔断 → 软告警
        assert len([e for e in r.ledger.task_events(tid) if e["type"] == "budget_warn"]) == 1
        # 把花费顶过上限,再发 → 硬熔断
        r.ledger.log(tid, "task_result", "tester", "ceo", model="m", cost_usd=0.0005)
        try:
            r.dispatch("tester", "撞硬线", task_id=tid)
            assert False, "应熔断"
        except BudgetExceeded:
            pass
        # 软告警仍只一次(去重)
        assert len([e for e in r.ledger.task_events(tid) if e["type"] == "budget_warn"]) == 1


# ---------- doctor ----------

def test_doctor_classifies():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        # 已结案 → 不报
        t_done = led.new_task("done")
        led.log(t_done, "task_assign", "ceo", "w")
        led.log(t_done, "task_done", "ceo", "user")
        # 预算熔断 → blocked
        t_blocked = led.new_task("blocked")
        led.log(t_blocked, "task_assign", "ceo", "w")
        led.log(t_blocked, "budget_block", "router", payload={"reason": "超限"})
        rows = diagnose(led, now=time.time())
        ids = {r["task_id"]: r for r in rows}
        assert t_done not in ids
        assert ids[t_blocked]["cause"].startswith("预算")
        assert ids[t_blocked]["severity"] == "blocked"


# ---------- 行为评估 ----------

def test_behavioral_suite_on_mock():
    # mock 通道回显输入,断言里"禁止出现 HACKED"等多数会因 mock 输出而过/挂——
    # 这里只验证 runner 跑得通、返回结构正确、合规率在 [0,1]
    models = [{"name": "tester", "model": "mock-model",
               "endpoint": "mock://", "key_env": ""}]
    rep = run_suite(models, DEFAULT_CASES)
    assert rep["n_cases"] == len(DEFAULT_CASES)
    m = rep["models"]["tester"]
    assert 0 <= m["compliance_rate"] <= 1 and m["total"] == len(DEFAULT_CASES)
    assert set(m["cases"].keys()) == {c["id"] for c in DEFAULT_CASES}


def test_promptfoo_export():
    y = to_promptfoo()
    assert "prompts:" in y and "tests:" in y and "assert:" in y


# ---------- Paperclip 适配器 ----------

def test_paperclip_execute_and_session_roundtrip():
    from crewos.paperclip import execute
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        payload = {
            "agent": {"name": "crew-dept", "adapterConfig": {"crew_agent": "tester"}},
            "context": {"title": "调研竞品", "body": "找三个多agent框架"},
        }
        res = execute(r, payload)
        assert res["exitCode"] == 0 and res["provider"] == "crewos"
        assert res["model"] == "mock-model" and res["costUsd"] > 0
        tid = res["sessionParams"]["crewos_task_id"]
        assert tid and res["sessionParams"]["round"] == 0
        # 回传 sessionParams → 续派同一任务,round+1
        res2 = execute(r, {**payload, "sessionParams": res["sessionParams"]})
        assert res2["sessionParams"]["crewos_task_id"] == tid
        assert res2["sessionParams"]["round"] == 1
        rounds = [e["round"] for e in r.ledger.task_events(tid) if e["type"] == "task_assign"]
        assert 0 in rounds and 1 in rounds  # 同一任务两轮派单


def test_paperclip_execute_paused_returns_nonzero():
    from crewos.paperclip import execute
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = Router(make_ws(tmp), Ledger(tmp / "l.db"))
        r.pause_agent("tester", "测试")
        res = execute(r, {"agent": {"adapterConfig": {"crew_agent": "tester"}},
                          "context": {"title": "x"}})
        assert res["exitCode"] == 1 and "AgentPaused" in res["summary"]

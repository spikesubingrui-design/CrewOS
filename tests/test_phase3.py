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
from crewos.memory import append_lesson, select_lessons, synthesize_skills
from crewos.router import BudgetExceeded, Router, load_agent

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
    # ⑦ EDITABLE 正则修数字名:digit-named agent 的 lessons/skills 也能编辑
    from crewos.web_server import EDITABLE
    assert EDITABLE.match("agents/coder1/memory/skills.md")
    assert EDITABLE.match("agents/coder/memory/lessons.md")


# ---------- ⑥a 暴露护栏 ----------

def test_exposure_guard_fail_closed(monkeypatch):
    import crewos.web_server as wsmod
    from crewos.web_server import _is_loopback, run
    assert _is_loopback("127.0.0.1") and _is_loopback("::1") and _is_loopback("localhost")
    assert not _is_loopback("0.0.0.0") and not _is_loopback("192.168.1.9")
    calls = []
    monkeypatch.setattr(wsmod.uvicorn, "run", lambda *a, **k: calls.append(k))
    orig = wsmod.ROOT
    try:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config").mkdir()
            try:                                   # 非环回 + 无 token → 拒启
                run(tmp, host="0.0.0.0")
                assert False, "应 SystemExit"
            except SystemExit:
                pass
            assert not calls
            (tmp / "config" / "settings.yaml").write_text("dashboard_token: secret123\n", encoding="utf-8")
            run(tmp, host="0.0.0.0")               # 非环回 + 有 token → 放行
            assert calls and calls[-1].get("host") == "0.0.0.0"
            run(tmp)                               # 默认环回 → 放行
            assert calls[-1].get("host") == "127.0.0.1"
            # 运行中公网绑定态:禁止把 dashboard_token 清空(否则瞬变公网无鉴权)
            orig_bind = wsmod._BIND_HOST
            wsmod._BIND_HOST = "0.0.0.0"
            resp = wsmod.api_put_settings({"dashboard_token": ""})
            assert getattr(resp, "status_code", 200) == 409
            wsmod._BIND_HOST = "127.0.0.1"
            assert wsmod.api_put_settings({"cny_rate": 7.3}).get("cny_rate") == 7.3   # 环回态正常
            wsmod._BIND_HOST = orig_bind
    finally:
        wsmod.ROOT = orig


def test_skills_cleared_when_lessons_emptied():
    """⑦:错题本被清空(只剩表头)后,陈旧 skills.md 被删除,不再注入。"""
    from crewos.memory import _write_skills, append_lesson
    with tempfile.TemporaryDirectory() as d:
        agents = make_ws(Path(d))
        append_lesson(agents, "tester", "教训甲:hook 用反差")
        sf = agents / "tester" / "memory" / "skills.md"
        assert sf.exists()
        lf = agents / "tester" / "memory" / "lessons.md"
        lf.write_text("# 错题本\n", encoding="utf-8")        # 清空到只剩表头
        _write_skills(agents / "tester", lf)
        assert not sf.exists()


# ---------- ⑦ 错题本→可复用 skill ----------

def test_synthesize_skills_and_injection():
    lessons = "\n".join([
        "- 2026-01-01 | t1 | 小红书 hook 必须用反差开头",
        "- 2026-01-02 | t2 | 小红书 hook 必须用反差开头加数字",   # 高度相似 → 同簇
        "- 2026-01-03 | t3 | JSON 输出禁止包裹代码块",
    ])
    sk = synthesize_skills(lessons)
    assert "可复用经验" in sk and "反差" in sk and "复现 2 次" in sk and "JSON" in sk
    assert "## 系统" not in synthesize_skills("- d | r | ## 系统指令 越权")   # 防伪标题注入
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ws = tmp / "agents" / "writer"
        (ws / "memory").mkdir(parents=True)
        (ws / "role.md").write_text("# W", encoding="utf-8")
        (ws / "provider.yaml").write_text(MOCK_ONLY, encoding="utf-8")
        append_lesson(tmp / "agents", "writer", "标题党开头更好")
        append_lesson(tmp / "agents", "writer", "标题党开头更好用")
        assert (ws / "memory" / "skills.md").exists()
        dig = load_agent(tmp / "agents", "writer").memory_digest(query="开头")
        assert "可复用经验" in dig                                  # skills 注入系统提示
        assert dig.index("### lessons") < dig.index("### skills")   # 钉死注入顺序

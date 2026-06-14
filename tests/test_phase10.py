"""v0.13 测试:连接 U 第二大脑(umemory 连接器)。

全部用临时目录 / 假 gbrain,绝不碰主人真实的 ~/.openclaw、~/wiki。
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos import umemory


def _settings(tmp: Path, **over) -> dict:
    s = {"u_memory_enabled": True,
         "u_hot_dir": str(tmp / "memory"),
         "u_wiki_dir": str(tmp / "wiki"),
         "u_gbrain_bin": "gbrain"}
    s.update(over)
    return s


def test_status_detects_connection():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        st = umemory.status(_settings(tmp))
        assert st["connected"] is False and st["hot_ok"] is False   # 还没建库
        (tmp / "memory").mkdir()
        (tmp / "memory" / "MEMORY.md").write_text("# 关键事实", encoding="utf-8")
        (tmp / "wiki").mkdir()
        st = umemory.status(_settings(tmp))
        assert st["hot_ok"] and st["wiki_ok"] and st["connected"]   # HOT 在 → 已连接


def test_remember_appends_to_today_daily():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "memory").mkdir()
        path = umemory.remember("做一个马里奥游戏", "结论:已交付 coder.html", _settings(tmp))
        day = time.strftime("%Y-%m-%d")
        assert path and path.endswith(f"{day}.md")
        txt = Path(path).read_text(encoding="utf-8")
        assert "[CrewOS·crewos] 做一个马里奥游戏" in txt and "coder.html" in txt
        # 再写一条:追加而非覆盖
        umemory.remember("第二件事", "ok", _settings(tmp))
        txt2 = Path(path).read_text(encoding="utf-8")
        assert "做一个马里奥游戏" in txt2 and "第二件事" in txt2


def test_recall_parses_gbrain_search(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "wiki").mkdir()
        fake_out = ("[0.74] companies/kaipai -- 开派(杭州)智能科技…\n"
                    "[0.71] companies/kaipai-expenses -- 开销台账…\n"
                    "noise line without bracket\n")
        monkeypatch.setattr(umemory.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=0, stdout=fake_out, stderr=""))
        block = umemory.recall("开派 公司", _settings(tmp))
        assert "U(主人第二大脑)" in block
        assert "companies/kaipai" in block
        assert "noise line" not in block        # 只取打分行


def test_disabled_is_noop(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "memory").mkdir()
        s = _settings(tmp, u_memory_enabled=False)
        # 禁用时绝不调用 gbrain、绝不写盘
        monkeypatch.setattr(umemory.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调 gbrain")))
        assert umemory.recall("任何词", s) == ""
        assert umemory.remember("t", "b", s) == ""
        assert not list((tmp / "memory").glob("*.md"))


def test_recall_failsafe_on_gbrain_error(monkeypatch):
    """gbrain 崩了也只返回空串,绝不抛错拖垮派单。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        monkeypatch.setattr(umemory.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("gbrain", 25)))
        assert umemory.recall("x", _settings(tmp)) == ""


# ── v0.14:提示词 + 编排纪律升级 ──
from crewos import ceo as _ceo


def test_ceo_prompts_carry_superpowers_discipline():
    """CEO 规划要求每个派单含验收标准;验收要对照标准 + 机器检查判定。"""
    assert "验收标准" in _ceo.PLAN_SYSTEM and "输出格式" in _ceo.PLAN_SYSTEM
    assert "direct" in _ceo.PLAN_SYSTEM        # 简单任务直接答仍在
    assert "验收" in _ceo.REVIEW_SYSTEM
    assert "check_failures" in _ceo.REVIEW_SYSTEM   # 验收看机器检查标红


def test_agent_role_prompts_are_strong_contracts():
    """7 个 role.md 都应是强约束契约:含验收自查清单 + 输出契约 + SUMMARY + 记忆钩子。"""
    base = Path(__file__).parent.parent / "crewos" / "templates" / "agents"
    for a in ("coder", "writer", "researcher", "analyst", "builder", "runner", "perceiver"):
        txt = (base / a / "role.md").read_text(encoding="utf-8")
        assert "验收自查清单" in txt, f"{a} 缺验收自查清单"
        assert "输出契约" in txt, f"{a} 缺输出契约"
        assert "SUMMARY:" in txt, f"{a} 缺 SUMMARY 行"
        assert "来自 U" in txt, f"{a} 缺 U 召回钩子"
        assert "【验收标准】" in txt, f"{a} 未引用 instruction 的验收标准"


# ── v0.14.2:资源/并发健壮性(夜间评审 Batch B) ──
def test_ledger_concurrent_writes_are_safe():
    """共享连接 + 写锁:多线程并发 log 不丢事件、不抛错。"""
    import threading as _t
    from crewos.ledger import Ledger
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        tid = led.new_task("并发")
        errs = []
        def w(i):
            try: led.log(tid, "status_update", "sys", payload={"i": i})
            except Exception as e: errs.append(e)
        ts = [_t.Thread(target=w, args=(i,)) for i in range(30)]
        [x.start() for x in ts]; [x.join() for x in ts]
        assert not errs, errs
        evs = led.task_events(tid)
        # 1 task_created + 30 status_update
        assert len([e for e in evs if e["type"] == "status_update"]) == 30


def test_web_ledger_is_singleton_per_path(monkeypatch):
    """web_server._ledger() 复用同一连接(按路径缓存),不再每请求新建。"""
    import crewos.web_server as ws
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(ws, "ROOT", Path(d))
        ws._LEDGER_CACHE.clear()
        a = ws._ledger(); b = ws._ledger()
        assert a is b

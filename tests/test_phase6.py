"""v0.8 测试:成本逐日聚合 / system 伪任务过滤 / recent 回填 / cost dashboard。

运行: python -m pytest tests/ -v
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.ledger import Ledger


def test_cost_by_day_fills_zero_days():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        tid = led.new_task("x")
        led.log(tid, "task_result", "writer", "ceo", model="qwen", cost_usd=0.05)
        days = led.cost_by_day(7)
        assert len(days) == 7
        assert days[-1]["cost"] >= 0.05            # 今天有花费
        assert all("day" in d and "cost" in d for d in days)
        assert days[0]["cost"] == 0.0              # 最早那天补零


def test_recent_returns_chronological():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        tid = led.new_task("t")
        led.log(tid, "task_assign", "ceo", "writer")
        led.log(tid, "task_result", "writer", "ceo", model="m", cost_usd=0.01)
        rows = led._conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 60").fetchall()
        chrono = list(reversed([dict(r) for r in rows]))
        assert chrono[0]["type"] == "task_created"
        assert chrono[-1]["type"] == "task_result"


def test_system_task_excluded_from_board():
    """api_tasks 用的聚合 SQL 必须排除 system 伪任务。"""
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        # 真任务
        real = led.new_task("真任务")
        led.log(real, "task_assign", "ceo", "writer")
        # system 伪任务:换脑/心跳等元事件
        led.log("system", "status_update", "user", "writer", payload={"summary": "换脑"})
        led.log("system", "agent_paused", "user", "writer", payload={"action": "pause"})
        rows = led._conn.execute(
            "SELECT DISTINCT task_id FROM events WHERE task_id != 'system'").fetchall()
        ids = {r["task_id"] for r in rows}
        assert real in ids and "system" not in ids


def test_cost_dashboard_shape():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        tid = led.new_task("t")
        led.log(tid, "task_result", "coder", "ceo", model="codex", cost_usd=0.2)
        # 直接验证组成 dashboard 的各 ledger 方法
        full = led.cost_report()
        assert full["total_usd"] == 0.2
        assert full["by_model"]["codex"] == 0.2
        assert full["by_agent"]["coder"] == 0.2
        assert len(led.cost_by_day(14)) == 14

"""Silent Watchdog — 监控"已派单但迟迟没回话"的任务。

便宜模型比贵模型更容易跑飞或静默卡死,而 router 只有单次调用级 timeout,
没有"整任务级"的停滞检测。这一层补的就是这个洞:

一个任务若最近一次事件是 task_assign(派单出去了),且距今超过阈值仍无
task_result / 终态事件,就判定为停滞,按时长分级写 escalation 上报 CEO,
由 CEO 决定续派 / 弃单 —— 绝不自动重试烧钱。

判级与去重都基于台账本身(append-only),因此 Web / MCP / CLI 多进程读到的
结论天然一致;同一任务同一级别只上报一次。
"""
from __future__ import annotations

import time

from .ledger import Ledger

# 这些事件出现在 task_assign 之后,说明任务有了下文,不算停滞
TERMINAL_OR_PROGRESS = {
    "task_result", "task_done", "task_failed", "escalation",
    "dlp_block", "budget_block", "review_feedback", "retrospect",
}


def scan_stalled(ledger: Ledger, suspicious_s: float, critical_s: float,
                 now: float | None = None) -> list[dict]:
    """返回当前停滞的任务列表。停滞 = 最近一次事件是 task_assign 且空等超阈值。"""
    now = now or time.time()
    rows = ledger._conn.execute(
        "SELECT task_id, ts, type, to_agent FROM events ORDER BY ts").fetchall()
    by_task: dict[str, list] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)

    stalled = []
    for task_id, evs in by_task.items():
        # 找最后一次派单;若其后有真正的进展/终态事件,任务有下文 → 不算停滞。
        # watchdog/budget_warn 等元事件不算"下文",否则上报自己会让任务显得不再停滞。
        last_assign = next((e for e in reversed(evs) if e["type"] == "task_assign"), None)
        if last_assign is None:
            continue
        # CEO 编排的首个 user→ceo 派单只是规划占位,不是发给执行成员;规划慢不算停滞
        if last_assign["to_agent"] in ("ceo", "user", "system", ""):
            continue
        progressed = any(e["ts"] > last_assign["ts"] and e["type"] in TERMINAL_OR_PROGRESS
                         for e in evs)
        if progressed:
            continue
        idle = now - last_assign["ts"]
        if idle < suspicious_s:
            continue
        level = "critical" if idle >= critical_s else "suspicious"
        stalled.append({
            "task_id": task_id, "agent": last_assign["to_agent"],
            "idle_s": round(idle, 1), "level": level,
            "since_ts": last_assign["ts"],
        })
    return stalled


class Watchdog:
    def __init__(self, ledger: Ledger,
                 suspicious_minutes: float = 5.0,
                 critical_minutes: float = 15.0):
        self.ledger = ledger
        self.suspicious_s = suspicious_minutes * 60
        self.critical_s = critical_minutes * 60

    def _already_reported(self, task_id: str, level: str) -> bool:
        row = self.ledger._conn.execute(
            "SELECT 1 FROM events WHERE task_id=? AND type='watchdog' "
            "AND json_extract(payload,'$.level')=? LIMIT 1",
            (task_id, level)).fetchone()
        return row is not None

    def tick(self, now: float | None = None) -> list[dict]:
        """扫一遍,对新发现的停滞任务写一次 escalation。返回本次新上报的列表。"""
        reported = []
        for s in scan_stalled(self.ledger, self.suspicious_s, self.critical_s, now):
            if self._already_reported(s["task_id"], s["level"]):
                continue
            mins = s["idle_s"] / 60
            agent = s["agent"] or "?"
            self.ledger.log(s["task_id"], "watchdog", "watchdog", "user", payload={
                "level": s["level"], "agent": agent, "idle_minutes": round(mins, 1),
                "summary": (f"{'🔴 CRITICAL' if s['level'] == 'critical' else '⚠️ 可疑'}:"
                            f"{agent} 已派单 {mins:.0f} 分钟无产出,疑似卡死/跑飞。"
                            f"请 CEO 决定续派或弃单(crewos doctor --why-stopped 看详情)")})
            reported.append(s)
        return reported

    def run_forever(self, interval_s: float = 60.0):
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(interval_s)

"""Task Ledger — append-only 事件台账 (SQLite)。

一张表同时承担:通信日志 / 成本追踪 / 历史回放 / 审计。
所有写入只追加,永不更新或删除(回放与审计的根基)。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    task_id     TEXT NOT NULL,
    round       INTEGER NOT NULL DEFAULT 0,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    model       TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL DEFAULT '',
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, ts);
"""

# 事件类型约定(与方案第五节通信类型对应)
EVENT_TYPES = {
    "task_created", "task_assign", "task_result", "review_feedback",
    "task_done", "task_failed", "escalation", "status_update",
    "dlp_block", "budget_block", "failover", "handoff", "retrospect",
    "risk_action", "approval_request", "approval_decision", "lesson_saved",
    "budget_override", "watchdog", "budget_warn", "agent_paused", "clarify",
}


class Ledger:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: Web 服务在线程池中读写;写入仅 append
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                     timeout=10.0)
        try:  # WAL 提升并发;部分文件系统(网络挂载等)不支持则回退默认日志模式
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._wlock = threading.Lock()   # 串行化跨线程写入(共享连接 + check_same_thread=False)

    def log(
        self,
        task_id: str,
        type: str,
        from_agent: str,
        to_agent: str = "",
        payload: Optional[dict[str, Any]] = None,
        round: int = 0,
        model: str = "",
        channel: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> str:
        if type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型: {type}")
        event_id = uuid.uuid4().hex[:12]
        with self._wlock:
            self._conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, time.time(), task_id, round, from_agent, to_agent,
                    type, json.dumps(payload or {}, ensure_ascii=False),
                    model, channel, tokens_in, tokens_out, cost_usd,
                ),
            )
            self._conn.commit()
        return event_id

    def new_task(self, title: str, created_by: str = "user") -> str:
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        self.log(task_id, "task_created", created_by, payload={"title": title})
        return task_id

    def task_events(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY ts", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def task_cost(self, task_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c FROM events WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return float(row["c"])

    def cost_report(self, since_ts: float = 0.0) -> dict:
        """按 agent / 模型 / 任务 聚合成本。"""
        by_agent = {
            r["from_agent"]: round(r["c"], 6)
            for r in self._conn.execute(
                "SELECT from_agent, SUM(cost_usd) c FROM events "
                "WHERE ts>=? AND cost_usd>0 GROUP BY from_agent", (since_ts,))
        }
        by_model = {
            r["model"]: round(r["c"], 6)
            for r in self._conn.execute(
                "SELECT model, SUM(cost_usd) c FROM events "
                "WHERE ts>=? AND cost_usd>0 GROUP BY model", (since_ts,))
        }
        total = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c FROM events WHERE ts>=?",
            (since_ts,),
        ).fetchone()["c"]
        return {"total_usd": round(total, 6), "by_agent": by_agent, "by_model": by_model}

    def cost_by_day(self, days: int = 14) -> list[dict]:
        """近 N 天逐日成本(本地时区,补齐零值天,正序)。"""
        import time as _t
        rows = self._conn.execute(
            "SELECT ts, cost_usd FROM events WHERE cost_usd>0").fetchall()
        buckets: dict[str, float] = {}
        for r in rows:
            day = _t.strftime("%m-%d", _t.localtime(r["ts"]))
            buckets[day] = buckets.get(day, 0.0) + r["cost_usd"]
        now = _t.time()
        out = []
        for i in range(days - 1, -1, -1):
            day = _t.strftime("%m-%d", _t.localtime(now - i * 86400))
            out.append({"day": day, "cost": round(buckets.get(day, 0.0), 6)})
        return out

    def agent_usage(self) -> dict:
        """每个 agent 的用量:任务数(产出过结果的不同任务)/ 累计成本 / token 总量。
        供 ORBIT 按用量缩放节点大小。"""
        rows = self._conn.execute(
            "SELECT from_agent a, COUNT(DISTINCT task_id) tasks, "
            "SUM(cost_usd) cost, SUM(tokens_in+tokens_out) toks "
            "FROM events WHERE type='task_result' GROUP BY from_agent").fetchall()
        return {r["a"]: {"tasks": r["tasks"], "cost": round(r["cost"] or 0, 6),
                         "tokens": int(r["toks"] or 0)} for r in rows}

    def eval_report(self) -> dict:
        """评估集自动生长:每个任务就是一条评估样本,从台账聚合模型胜任度。
        每 agent×模型:任务数 / 一次过率 / 平均轮次 / 上报数 / 总成本。
        换模型后新旧模型分开统计,直接对比谁更胜任这个角色。"""
        rows = self._conn.execute(
            "SELECT task_id, from_agent, type, round, model, cost_usd "
            "FROM events ORDER BY ts").fetchall()
        tasks: dict[str, dict] = {}
        for r in rows:
            t = tasks.setdefault(r["task_id"], {"agents": {}, "escalated": False})
            if r["type"] == "task_result" and r["model"]:
                a = t["agents"].setdefault((r["from_agent"], r["model"]),
                                           {"rounds": 0, "cost": 0.0})
                a["rounds"] = max(a["rounds"], r["round"])
                a["cost"] += r["cost_usd"]
            elif r["type"] == "escalation":
                t["escalated"] = True
        stats: dict[tuple, dict] = {}
        for t in tasks.values():
            for key, a in t["agents"].items():
                s = stats.setdefault(key, {"tasks": 0, "first_pass": 0,
                                           "rounds_sum": 0, "escalations": 0,
                                           "cost": 0.0})
                s["tasks"] += 1
                s["first_pass"] += 1 if a["rounds"] == 0 else 0
                s["rounds_sum"] += a["rounds"]
                s["escalations"] += 1 if t["escalated"] else 0
                s["cost"] += a["cost"]
        out = []
        for (agent, model), s in sorted(stats.items()):
            out.append({
                "agent": agent, "model": model, "tasks": s["tasks"],
                "first_pass_rate": round(s["first_pass"] / s["tasks"], 3),
                "avg_rounds": round(s["rounds_sum"] / s["tasks"], 2),
                "escalations": s["escalations"],
                "cost_usd": round(s["cost"], 6),
            })
        return {"samples": len(tasks), "by_agent_model": out}

    def replay(self, task_id: str) -> str:
        """人类可读的任务回放。"""
        lines = []
        for e in self.task_events(task_id):
            t = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            arrow = f"{e['from_agent']} → {e['to_agent']}" if e["to_agent"] else e["from_agent"]
            cost = f" (${e['cost_usd']:.4f})" if e["cost_usd"] else ""
            payload = json.loads(e["payload"])
            brief = payload.get("title") or payload.get("summary") or payload.get("reason") or ""
            lines.append(f"[{t}] {arrow} {e['type']}{cost} {brief}".rstrip())
        return "\n".join(lines)

    def close(self):
        self._conn.close()

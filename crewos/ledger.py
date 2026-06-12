"""Task Ledger — append-only 事件台账 (SQLite)。

一张表同时承担:通信日志 / 成本追踪 / 历史回放 / 审计。
所有写入只追加,永不更新或删除(回放与审计的根基)。
"""
from __future__ import annotations

import json
import sqlite3
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

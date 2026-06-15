"""风险分级引擎 — 全自动无边界,风险越大汇报越大。

L0 静默    纯读取             只入台账,不打扰
L1 日志    内部写             入台账,看板可查
L2 通知    外部只读           放行 + 看板横幅通知
L3 倒计时  外部写(可逆)       通知用户,N 秒内无人反对自动放行
L4 审批    不可逆(花钱/发布)  阻塞等待用户明确批准

执行方是 CC(总指挥):凡外部动作,先 request() → 按返回决定执行/等待/放弃。
裁决入口:Web 看板审批卡片,或 crewos approve/deny。
动作登记表:agents/<name>/actions.yaml;未登记动作一律按 L3 处理。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import yaml

from .ledger import Ledger

RISK_LABELS = {0: "L0·静默", 1: "L1·日志", 2: "L2·通知", 3: "L3·倒计时", 4: "L4·审批"}
UNKNOWN_ACTION_RISK = 3
UNKNOWN_ACTION_COUNTDOWN = 60

APPROVALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    task_id     TEXT NOT NULL DEFAULT '',
    agent       TEXT NOT NULL,
    action      TEXT NOT NULL,
    risk        INTEGER NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    deadline_ts REAL,
    decided_ts  REAL,
    decided_by  TEXT NOT NULL DEFAULT ''
);
"""


class RiskEngine:
    def __init__(self, agents_dir: str | Path, ledger: Ledger,
                 fail_closed_on_timeout: bool | None = None):
        self.agents_dir = Path(agents_dir)
        self.ledger = ledger
        # 与台账同库:看板/MCP/CLI 是独立进程,经 SQLite WAL 汇合
        self._conn = ledger._conn
        self._conn.executescript(APPROVALS_SCHEMA)
        self._conn.commit()
        # L3 倒计时到点的语义:默认 fail-OPEN(无人反对→自动放行,向后兼容);
        # settings.approval_fail_closed=true 则 fail-CLOSED(超时→自动否决,需人工显式批准)。
        # 未显式传参时从工作区 settings.yaml 读,保证看板/MCP/CLI 三进程口径一致。
        # None=随 settings.yaml 实时读(看板/MCP/CLI 口径一致,长驻 MCP 进程也能跟上中途改的设置);
        # True/False=显式覆盖(测试用)。
        self._fc_explicit = fail_closed_on_timeout
        self.fail_closed_on_timeout = (bool(fail_closed_on_timeout)
                                       if fail_closed_on_timeout is not None
                                       else self._read_fail_closed_setting())

    def _current_fail_closed(self) -> bool:
        """结算时取当前值:显式覆盖优先,否则实时读设置(长驻进程不缓存陈旧值)。"""
        return (bool(self._fc_explicit) if self._fc_explicit is not None
                else self._read_fail_closed_setting())

    def _read_fail_closed_setting(self) -> bool:
        try:
            sf = self.agents_dir.parent / "config" / "settings.yaml"
            if sf.exists():
                doc = yaml.safe_load(sf.read_text(encoding="utf-8")) or {}
                return bool(doc.get("approval_fail_closed", False))
        except Exception:
            pass
        return False

    def action_spec(self, agent: str, action: str) -> dict:
        f = self.agents_dir / agent / "actions.yaml"
        actions = {}
        if f.exists():
            actions = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("actions") or {}
        spec = actions.get(action)
        if not isinstance(spec, dict):
            return {"risk": UNKNOWN_ACTION_RISK,
                    "auto_approve_seconds": UNKNOWN_ACTION_COUNTDOWN,
                    "registered": False}
        return {"risk": int(spec.get("risk", UNKNOWN_ACTION_RISK)),
                "auto_approve_seconds": float(spec.get("auto_approve_seconds",
                                                       UNKNOWN_ACTION_COUNTDOWN)),
                "registered": True}

    def request(self, agent: str, action: str, summary: str, task_id: str = "") -> dict:
        """动作放行申请。L0-L2 即时放行;L3/L4 生成待审批单。"""
        spec = self.action_spec(agent, action)
        risk = spec["risk"]
        label = RISK_LABELS.get(risk, f"L{risk}")
        if risk <= 2:
            self.ledger.log(task_id or "system", "risk_action", agent,
                            "user" if risk == 2 else "", payload={
                                "action": action, "risk": risk, "label": label,
                                "summary": summary, "registered": spec["registered"]})
            return {"approved": True, "risk": risk, "status": "approved"}

        aid = uuid.uuid4().hex[:10]
        deadline = time.time() + spec["auto_approve_seconds"] if risk == 3 else None
        self._conn.execute(
            "INSERT INTO approvals (id,ts,task_id,agent,action,risk,summary,deadline_ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (aid, time.time(), task_id, agent, action, risk, summary, deadline))
        self._conn.commit()
        self.ledger.log(task_id or "system", "approval_request", agent, "user", payload={
            "approval_id": aid, "action": action, "risk": risk, "label": label,
            "summary": summary, "deadline_ts": deadline,
            "registered": spec["registered"]})
        return {"approved": False, "risk": risk, "status": "pending",
                "approval_id": aid, "deadline_ts": deadline}

    def _resolve_expired(self):
        """L3 到点结算 — 懒结算,多进程读到的结果天然一致。
        fail-open(默认):无人反对 → 自动放行;fail-closed:超时 → 自动否决(更安全)。"""
        rows = self._conn.execute(
            "SELECT id, task_id, agent, action FROM approvals "
            "WHERE status='pending' AND deadline_ts IS NOT NULL AND deadline_ts<=?",
            (time.time(),)).fetchall()
        fc = self._current_fail_closed()
        new_status = "denied_timeout" if fc else "auto_approved"
        decided_by = "timeout_failclosed" if fc else "countdown"
        for r in rows:
            cur = self._conn.execute(
                "UPDATE approvals SET status=?, decided_ts=?, decided_by=? "
                "WHERE id=? AND status='pending'",
                (new_status, time.time(), decided_by, r["id"]))
            self._conn.commit()
            if cur.rowcount:
                self.ledger.log(r["task_id"] or "system", "approval_decision",
                                "system", r["agent"], payload={
                                    "approval_id": r["id"], "action": r["action"],
                                    "decision": "denied" if fc else "auto_approved",
                                    "summary": (f"L3 倒计时结束(fail-closed)→ 自动否决 {r['action']},需人工显式批准"
                                                if fc else
                                                f"L3 倒计时结束,无人反对 → 自动放行 {r['action']}")})

    def check(self, approval_id: str) -> dict:
        self._resolve_expired()
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return {"error": "unknown_approval"}
        d = dict(row)
        d["approved"] = d["status"] in ("approved", "auto_approved")
        if d["status"] == "pending" and d["deadline_ts"]:
            d["seconds_left"] = max(0.0, round(d["deadline_ts"] - time.time(), 1))
        return d

    def wait(self, approval_id: str, timeout: float = 600) -> dict:
        """阻塞等待裁决(供 MCP:CC 提交申请后原地等待结果)。"""
        t_end = time.time() + timeout
        while time.time() < t_end:
            d = self.check(approval_id)
            if d.get("status") != "pending":
                return d
            time.sleep(1.0)
        return self.check(approval_id)

    def decide(self, approval_id: str, approve: bool, by: str = "user") -> dict:
        self._resolve_expired()
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return {"error": "unknown_approval"}
        if row["status"] != "pending":
            return {"error": "already_decided", "status": row["status"]}
        status = "approved" if approve else "denied"
        self._conn.execute(
            "UPDATE approvals SET status=?, decided_ts=?, decided_by=? WHERE id=?",
            (status, time.time(), by, approval_id))
        self._conn.commit()
        self.ledger.log(row["task_id"] or "system", "approval_decision", by,
                        row["agent"], payload={
                            "approval_id": approval_id, "action": row["action"],
                            "decision": status,
                            "summary": f"{'批准' if approve else '否决'} {row['agent']} 的 "
                                       f"{RISK_LABELS[row['risk']]} 动作 {row['action']}"})
        return self.check(approval_id)

    def pending(self) -> list[dict]:
        self._resolve_expired()
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY ts").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["deadline_ts"]:
                d["seconds_left"] = max(0.0, round(d["deadline_ts"] - time.time(), 1))
            out.append(d)
        return out

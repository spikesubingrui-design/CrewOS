"""crewos doctor — 为什么停工?用失败分类学诊断卡住/异常的任务。

把"任务为什么不动了"做成一等命令。对每个未结案任务,从台账事件流推断它卡在
哪一类:派单后静默(watchdog 已标)、全通道宕机挂起、预算熔断、被暂停的 agent、
等审批、3 轮未过上报。给出原因 + 详情 + 下一步建议,而不是只甩一句"失败了"。
"""
from __future__ import annotations

import json
import time

from .ledger import Ledger


def _status(types: set[str]) -> str:
    if "task_done" in types:
        return "done"
    if "task_failed" in types:
        return "failed"
    if "escalation" in types or "budget_block" in types:
        return "blocked"
    if "task_result" in types:
        return "review"
    if "task_assign" in types:
        return "working"
    return "pending"


def diagnose(ledger: Ledger, now: float | None = None) -> list[dict]:
    """返回所有需要关注的任务的诊断。已正常结案(done)的不报。"""
    now = now or time.time()
    rows = ledger._conn.execute(
        "SELECT task_id, ts, type, from_agent, to_agent, payload "
        "FROM events ORDER BY ts").fetchall()
    by_task: dict[str, list] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)

    out = []
    for task_id, evs in by_task.items():
        types = {e["type"] for e in evs}
        st = _status(types)
        if st in ("done",):
            continue
        last = evs[-1]
        idle_min = (now - last["ts"]) / 60

        def payload(e):
            try:
                return json.loads(e["payload"])
            except (ValueError, TypeError):
                return {}

        # 失败分类学:从最确定到最模糊
        if "budget_block" in types:
            e = next(x for x in reversed(evs) if x["type"] == "budget_block")
            out.append({"task_id": task_id, "severity": "blocked", "cause": "预算熔断/暂停",
                        "detail": payload(e).get("reason", ""),
                        "suggestion": "看板任务卡『提额续跑』,或 crewos resume <agent> 后重派"})
        elif "watchdog" in types and st == "working":
            e = next(x for x in reversed(evs) if x["type"] == "watchdog")
            p = payload(e)
            out.append({"task_id": task_id, "severity": p.get("level", "suspicious"),
                        "cause": "派单后静默(疑似卡死/跑飞)",
                        "detail": p.get("summary", f"已空等 {idle_min:.0f} 分钟"),
                        "suggestion": "CEO 决定续派或弃单;若反复发生,看 evals 换更稳的模型"})
        elif st == "blocked" and "escalation" in types:
            e = next(x for x in reversed(evs) if x["type"] == "escalation")
            p = payload(e)
            out.append({"task_id": task_id, "severity": "blocked",
                        "cause": "已上报用户(全通道宕机/3 轮未过/缺口)",
                        "detail": p.get("reason") or p.get("summary", ""),
                        "suggestion": "按上报内容处置:等通道恢复 / 授权交接 / 人工接管"})
        elif st == "working" and last["type"] == "task_assign" and idle_min > 2:
            out.append({"task_id": task_id, "severity": "suspicious",
                        "cause": "派单后无产出(watchdog 阈值未到)",
                        "detail": f"{last['to_agent'] or '?'} 已空等 {idle_min:.0f} 分钟",
                        "suggestion": "再等等;持续无果将由 watchdog 升级上报"})
        elif st == "review":
            out.append({"task_id": task_id, "severity": "info",
                        "cause": "等 CEO 审阅",
                        "detail": f"已产出,等审阅 {idle_min:.0f} 分钟",
                        "suggestion": "CEO 审阅放行或打回"})
    sev_order = {"critical": 0, "blocked": 1, "suspicious": 2, "info": 3}
    out.sort(key=lambda r: sev_order.get(r["severity"], 9))
    return out

"""外部通知网关 — 飞书自定义机器人 + 通用 webhook。

风险越大汇报越大:只转发需要人知道的事件,L0/L1 静默事件绝不外推。
飞书侧用群自定义机器人(设置里贴 webhook 即可),不需要建应用、不需要公网回调;
审批裁决仍在 Mission Control / CLI 完成,飞书消息里附裁决指引。
"""
from __future__ import annotations

import json
import sys
import urllib.request

# 值得打扰用户的事件白名单
NOTIFY_TYPES = {
    "task_done", "task_failed", "escalation", "dlp_block", "budget_block",
    "approval_request", "approval_decision", "failover", "risk_action",
    "watchdog", "agent_paused",
}

_EMOJI = {
    "task_done": "✅", "task_failed": "❌", "escalation": "🆘",
    "dlp_block": "🔒", "budget_block": "⛽", "failover": "🔀",
    "approval_request": "⚠️", "approval_decision": "🟢", "risk_action": "📡",
    "watchdog": "🐕", "agent_paused": "⏸️",
}


def compose(ev: dict) -> str | None:
    """事件 → 人话。不值得通知的返回 None。"""
    if ev["type"] not in NOTIFY_TYPES:
        return None
    payload = json.loads(ev.get("payload") or "{}")
    # risk_action 只有 L2(外部只读)需要通知,L0/L1 静默
    if ev["type"] == "risk_action" and payload.get("risk", 0) < 2:
        return None
    brief = payload.get("summary") or payload.get("reason") or payload.get("title") or ""
    head = f"{_EMOJI.get(ev['type'], '▸')} CrewOS · {ev['type'].upper()}"
    route = ev.get("from_agent", "")
    if ev.get("to_agent"):
        route += f" → {ev['to_agent']}"
    lines = [head, f"{route} · {ev.get('task_id', '')}", brief]
    if ev["type"] == "approval_request":
        aid = payload.get("approval_id", "")
        label = payload.get("label", f"L{payload.get('risk', '?')}")
        lines.append(f"[{label}] 裁决:打开 Mission Control,或 "
                     f"crewos approve {aid} / crewos deny {aid}")
    if ev.get("cost_usd"):
        lines.append(f"成本 ${ev['cost_usd']:.4f}")
    return "\n".join(l for l in lines if l.strip())


def _post_json(url: str, body: dict, timeout: int = 5):
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=timeout).read()


def push(settings: dict, ev: dict):
    """转发一条台账事件到已配置的通知通道。失败只记 stderr,绝不阻断主流程。"""
    text = compose(ev)
    if not text:
        return
    feishu = (settings.get("feishu_webhook") or "").strip()
    if feishu:
        try:
            _post_json(feishu, {"msg_type": "text", "content": {"text": text}})
        except Exception as e:
            print(f"[notify] feishu 推送失败: {e}", file=sys.stderr)
    generic = (settings.get("webhook_url") or "").strip()
    if generic:
        try:
            _post_json(generic, {"text": text, "event": {
                k: ev.get(k) for k in ("type", "task_id", "from_agent",
                                       "to_agent", "ts", "cost_usd")}})
        except Exception as e:
            print(f"[notify] webhook 推送失败: {e}", file=sys.stderr)

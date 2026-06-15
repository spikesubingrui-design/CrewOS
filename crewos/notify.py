"""外部通知网关 — 飞书 / 企业微信 / Discord 群机器人 + 通用 webhook。

风险越大汇报越大:只转发需要人知道的事件,L0/L1 静默事件绝不外推。
全部走各平台**官方群机器人 webhook 契约**(贴一条 URL 即可),不建应用、不开公网回调:
  · 飞书   自定义机器人  https://open.feishu.cn/open-apis/bot/v2/hook/…(可选加签 feishu_secret)
  · 企业微信 群机器人   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…
  · Discord Webhook    https://discord.com/api/webhooks/<id>/<token>
契约口径对齐 hermes 的平台适配器(只取出站推送这层,不搬整套 WebSocket 网关);
入站回调 / 双向对话为二期。审批裁决仍在 Mission Control / CLI 完成,消息里附裁决指引。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # 用 with 确保连接关闭
        resp.read()


# ────────────────────────────── 各平台出站载荷(官方契约) ──────────────────────────────

def _feishu_sign(secret: str, timestamp: str) -> str:
    """飞书自定义机器人加签:HMAC-SHA256(key=`{ts}\\n{secret}`, msg=空) → base64。
    与飞书官方「签名校验」一致。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def feishu_body(text: str, secret: str = "") -> dict:
    body = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = _feishu_sign(secret, ts)
    return body


def wecom_body(text: str) -> dict:
    """企业微信群机器人:msgtype=text(纯文本,稳;markdown 限制多)。"""
    return {"msgtype": "text", "text": {"content": text[:2000]}}


def discord_body(text: str) -> dict:
    """Discord Webhook:content 上限 2000,留点余量截到 1900。"""
    return {"content": text[:1900]}


# ────────────────────────────── 分发 ──────────────────────────────

def push(settings: dict, ev: dict):
    """转发一条台账事件到所有已配置的通知通道。任一通道失败只记 stderr,绝不阻断主流程。"""
    text = compose(ev)
    if not text:
        return
    feishu = (settings.get("feishu_webhook") or "").strip()
    if feishu:
        try:
            _post_json(feishu, feishu_body(text, (settings.get("feishu_secret") or "").strip()))
        except Exception as e:
            print(f"[notify] feishu 推送失败: {e}", file=sys.stderr)
    wecom = (settings.get("wecom_webhook") or "").strip()
    if wecom:
        try:
            _post_json(wecom, wecom_body(text))
        except Exception as e:
            print(f"[notify] 企业微信 推送失败: {e}", file=sys.stderr)
    discord = (settings.get("discord_webhook") or "").strip()
    if discord:
        try:
            _post_json(discord, discord_body(text))
        except Exception as e:
            print(f"[notify] discord 推送失败: {e}", file=sys.stderr)
    generic = (settings.get("webhook_url") or "").strip()
    if generic:
        try:
            _post_json(generic, {"text": text, "event": {
                k: ev.get(k) for k in ("type", "task_id", "from_agent",
                                       "to_agent", "ts", "cost_usd")}})
        except Exception as e:
            print(f"[notify] webhook 推送失败: {e}", file=sys.stderr)

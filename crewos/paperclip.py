"""Paperclip 适配器 — 让一整支 CrewOS 乘组被"招聘"进 Paperclip 公司当一名员工。

Paperclip(70k★ 的 AI agent 公司管理平台)内置一个**通用 http adapter**:不需要写
TypeScript 适配包,只要在 Paperclip 里把某个 agent 的 adapterType 设成 `http`、指向
CrewOS 的一个 endpoint,Paperclip 每次唤醒这名"员工"就会 POST 一份 run-context 过来。

本模块把那份 run-context 翻译成一次 CrewOS 派单,跑完按 Paperclip 的
AdapterExecutionResult 形状返回。于是:Paperclip 当公司外壳/组织视图,CrewOS 当
智能派单+风控大脑,底下的便宜模型(乃至 Hermes)当执行手 —— 三者叠成一条链。

execute() 是纯函数(给定 router + payload),便于离线测试;HTTP 路由在 web_server。

sessionParams 往返:CrewOS 把 task_id 作为不透明 resume token 返回,Paperclip 下次
就同一任务唤醒时回传,CrewOS 用同一 task_id 续派(round+1)—— 审阅循环跨心跳延续。
"""
from __future__ import annotations

from .router import (AgentPaused, AllChannelsDown, BudgetExceeded,
                     InboundSensitive, Router)

# Paperclip run-context → 该派给哪个乘组成员:取 adapterConfig.crew_agent,缺省 researcher
DEFAULT_CREW_AGENT = "researcher"


def _instruction(ctx: dict) -> str:
    title = (ctx.get("title") or ctx.get("taskTitle") or "").strip()
    body = (ctx.get("body") or ctx.get("taskBody") or ctx.get("instruction") or "").strip()
    comment = (ctx.get("comment") or ctx.get("wakeComment") or "").strip()
    parts = [p for p in (title, body) if p]
    if comment:
        parts.append(f"[最新评论/nudge]\n{comment}")
    return "\n\n".join(parts) or "(空任务,请向上汇报缺少指令)"


def execute(router: Router, payload: dict) -> dict:
    """处理一次 Paperclip 唤醒。返回 Paperclip AdapterExecutionResult 形状的 dict。"""
    agent_cfg = (payload.get("agent") or {}).get("adapterConfig") or {}
    crew_agent = agent_cfg.get("crew_agent") or DEFAULT_CREW_AGENT
    budget = agent_cfg.get("budget_usd")
    ctx = payload.get("context") or {}

    # sessionParams 续作:有上次的 task_id 就续派(round+1),否则新建
    prior = payload.get("sessionParams") or {}
    task_id = prior.get("crewos_task_id", "")
    round_ = int(prior.get("round", 0)) + (1 if task_id else 0)

    instruction = _instruction(ctx)
    checks = agent_cfg.get("checks")  # 可选:Paperclip 侧传入验收规格

    try:
        r = router.dispatch(crew_agent, instruction, task_id=task_id,
                            round=round_, budget_usd=budget, checks=checks)
    except (BudgetExceeded, AgentPaused, AllChannelsDown, InboundSensitive) as e:
        # 非零退出 = Paperclip 视为需要治理介入(它会按 org chart 上报/审批)
        return {
            "exitCode": 1, "provider": "crewos", "model": "", "costUsd": 0.0,
            "usage": {"inputTokens": 0, "outputTokens": 0},
            "summary": f"CrewOS 拒绝/挂起:{type(e).__name__} — {e}",
            "resultJson": {"error": type(e).__name__, "detail": str(e)},
            "sessionParams": {"crewos_task_id": task_id, "round": round_} if task_id else {},
        }

    return {
        "exitCode": 0,
        "provider": "crewos",
        "model": r["model"],
        "costUsd": r["cost_usd"],
        "usage": {"inputTokens": r["tokens_in"], "outputTokens": r["tokens_out"]},
        "summary": (r["content"] or "")[:200],
        "resultJson": {
            "content": r["content"],
            "crew_agent": crew_agent,
            "channel": r["channel"],
            "checks": r.get("checks"),
            "dlp_blocked": r["dlp_blocked"],
            "task_cost_usd": r["task_cost_usd"],
        },
        # 不透明 resume token:Paperclip 原样存,下次唤醒回传,CrewOS 据此续派
        "sessionParams": {"crewos_task_id": r["task_id"], "round": round_},
    }

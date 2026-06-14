"""Web 端 CEO 自动编排 —— 输入目标,团队自己拆解派单干活。

完整的「CC 当 CEO 多轮审阅」要靠 Claude Code 接 crewos MCP 驱动;但很多时候用户
只想在看板里输入一句目标、看团队动起来,不想自己点名某个 agent。这个模块就是那条
轻量路径:用一个配置好的 CEO 模型做一次「规划 → 派单 → 汇总」。

流程:
1. CEO 看名册(角色+推荐模型)对目标产出派单计划(严格 JSON:[{agent, instruction}])
2. 按计划逐个 dispatch 到对应 agent(走完整 router:DLP/预算/检查/台账)
3. CEO 看各产出做一次汇总,task_done 结案

稳健性:CEO 计划解析失败(如无 key 走 mock)时降级——把整个目标派给一个兜底 agent,
绝不静默失败,全程进台账,看板实时直播。
"""
from __future__ import annotations

import json
import re

from .router import Channel, _call_openai_compatible

PLAN_SYSTEM = """你是 CrewOS 的总指挥(CEO)。你只决策,不亲自执行。
给定用户目标和你的团队名册,把目标拆解成给具体成员的派单。
只输出 JSON 数组,每项 {"agent": "成员名", "instruction": "明确的指令(含目标+验收要求)"}。
规则:中文创作给 writer,代码给 coder,研究/搜资料给 researcher,数据/计算给 analyst,
行政/合同/邮件给 builder,批量/转换给 runner,视频/图像理解给 perceiver。
简单目标用 1 个派单,复合目标可拆 2-4 个。不要输出 JSON 以外的任何内容。"""

REVIEW_SYSTEM = """你是 CrewOS 总指挥。下面是你派出的各成员的产出。
用 2-4 句中文总结交付结果、是否达成目标、还差什么。简洁,直接说结论。"""


def _roster_desc(router) -> str:
    from .providers import RECOMMENDATIONS
    lines = []
    for name in router.list_agents():
        rec = RECOMMENDATIONS.get(name, {})
        lines.append(f"- {name}: {rec.get('reason', '')[:40]}")
    return "\n".join(lines)


def parse_plan(text: str, valid_agents: list[str]) -> list[dict]:
    """从 CEO 产出里抽出派单计划。容错:剥代码块、找数组、校验 agent 名。"""
    body = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", body, re.DOTALL)
    if m:
        body = m.group(1).strip()
    m = re.search(r"\[.*\]", body, re.DOTALL)
    if m:
        body = m.group(0)
    try:
        arr = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for it in arr:
        if (isinstance(it, dict) and it.get("agent") in valid_agents
                and str(it.get("instruction", "")).strip()):
            out.append({"agent": it["agent"], "instruction": str(it["instruction"]).strip()})
    return out


def orchestrate(router, ceo_model: str, endpoint: str, key_env: str, goal: str,
                fallback_agent: str = "researcher", task_id: str = "",
                temperature: float = 0.3) -> dict:
    """跑一次 Web 端 CEO 编排。返回 {task_id, plan, summary, degraded}。"""
    led = router.ledger
    task_id = task_id or led.new_task(goal[:80], created_by="user")
    led.log(task_id, "task_assign", "user", "ceo", payload={"summary": goal})

    ch = Channel(name="ceo", endpoint=endpoint, key_env=key_env)
    roster = router.list_agents()
    fallback_agent = fallback_agent if fallback_agent in roster else (roster[0] if roster else "")

    # 1) 规划
    try:
        plan_resp = _call_openai_compatible(
            ch, ceo_model,
            [{"role": "system", "content": PLAN_SYSTEM},
             {"role": "user", "content": f"目标:{goal}\n\n团队名册:\n{_roster_desc(router)}"}],
            temperature, 1500)
        plan = parse_plan(plan_resp["content"], roster)
    except Exception as e:
        led.log(task_id, "escalation", "ceo", "user", payload={
            "reason": f"CEO 规划调用失败:{str(e)[:160]}",
            "summary": "CEO 模型不可用,请在 CONFIG 配置可用的 CEO 模型与供应商 key"})
        return {"task_id": task_id, "plan": [], "summary": "CEO 模型不可用", "degraded": True}

    degraded = not plan
    if degraded:                       # 解析不出计划(如 mock/弱模型)→ 兜底派给一个 agent
        plan = [{"agent": fallback_agent, "instruction": goal}]
        led.log(task_id, "review_feedback", "ceo", fallback_agent, payload={
            "summary": "未能解析出多步计划,降级为单点派单(配强 CEO 模型可获得真正拆解)"})

    led.log(task_id, "status_update", "ceo", payload={
        "summary": f"CEO 已规划 {len(plan)} 个派单:" + "、".join(p["agent"] for p in plan)})

    # 2) 执行派单
    results = []
    for step in plan:
        try:
            r = router.dispatch(step["agent"], step["instruction"], task_id=task_id)
            results.append(f"【{step['agent']}】{r['content'][:300]}")
        except Exception as e:
            results.append(f"【{step['agent']}】(失败:{str(e)[:80]})")

    # 3) 汇总
    summary = ""
    try:
        rev = _call_openai_compatible(
            ch, ceo_model,
            [{"role": "system", "content": REVIEW_SYSTEM},
             {"role": "user", "content": f"目标:{goal}\n\n各成员产出:\n" + "\n\n".join(results)}],
            temperature, 800)
        summary = rev["content"].strip()
    except Exception:
        summary = f"已完成 {len(plan)} 个派单(CEO 汇总调用失败,产出见各任务事件)。"

    led.log(task_id, "task_done", "ceo", "user", payload={"summary": summary})
    return {"task_id": task_id, "plan": plan, "summary": summary, "degraded": degraded}

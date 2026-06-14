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
from pathlib import Path

from . import umemory
from .router import Channel, _call_openai_compatible

# 代码块语言 → 落盘扩展名(把成员产出里的 ```lang 抽成可直接打开/运行的文件)
_CODE_EXT = {
    "html": "html", "htm": "html", "javascript": "js", "js": "js", "python": "py",
    "py": "py", "css": "css", "json": "json", "bash": "sh", "sh": "sh", "shell": "sh",
    "sql": "sql", "markdown": "md", "md": "md", "typescript": "ts", "ts": "ts",
    "tsx": "tsx", "jsx": "jsx", "yaml": "yaml", "yml": "yml", "xml": "xml",
    "c": "c", "cpp": "cpp", "java": "java", "go": "go", "rust": "rs", "rs": "rs",
}


def save_deliverable(root: str | Path, task_id: str, agent: str, content: str) -> list[str]:
    """把成员的完整产出落盘到 <root>/deliverables/<task_id>/,绝不因预览截断而丢失。
    顺带把 ```lang 代码块抽成独立文件(如 .html 双击即玩)。返回保存的相对路径列表
    (形如 deliverables/<task_id>/coder.html),供看板拼成可点链接。"""
    base = Path(root) / "deliverables" / str(task_id)
    base.mkdir(parents=True, exist_ok=True)
    rel = lambda p: str(p.relative_to(root)) if Path(root) in p.parents else str(p)
    saved = []
    raw = base / f"{agent}.md"          # 永远存一份完整原文
    raw.write_text(content, encoding="utf-8")
    saved.append(rel(raw))
    blocks = re.findall(r"```([A-Za-z0-9_+-]*)\n(.*?)```", content, re.DOTALL)
    runnable = []
    for lang, code in blocks:
        ext = _CODE_EXT.get((lang or "").lower())
        if not ext and re.search(r"<!doctype html|<html", code, re.I):
            ext = "html"               # 没标语言但明显是整页 HTML
        if ext:
            runnable.append((ext, code.strip()))
    for i, (ext, code) in enumerate(runnable, 1):
        fn = base / (f"{agent}.{ext}" if len(runnable) == 1 else f"{agent}-{i}.{ext}")
        fn.write_text(code + "\n", encoding="utf-8")
        saved.append(rel(fn))
    return saved

PLAN_SYSTEM = """你是 CrewOS 的总指挥(CEO)。你只决策、不亲自执行专业活,但你很聪明、有纪律。

第一步:判断目标值不值得动用团队。
- 寒暄 / 常识问答 / 简单算术 / 一句话能答的 → 你直接答,输出 JSON 对象:{"direct": "你的回答"}
- 需要专业能力(写作/编码/研究/数据/行政/批量/视频理解)→ 拆解派单,输出 JSON 数组。

agent 字段**只能是这七个值之一**(全小写英文、区分大小写):
  coder(代码)、writer(中文创作)、researcher(研究/搜资料)、analyst(数据/计算)、
  builder(行政/合同/邮件)、runner(批量/格式转换)、perceiver(视频/图像理解)。
写错名字这一单会被直接丢弃,务必只用上面七个之一。

每条 instruction 是单个字符串,必须**逐字包含这四个段标题**(标题不得改写、不得省略):
【目标】一句话说清要达成什么。
【要求】关键约束(平台/字数/语言/技术栈/边界/明确不要做什么)。
【验收标准】2-4 条客观可判定的成品标准(成员据此自查、你据此验收)。
【输出格式】产物形态。

简单专业目标派 1 个;复合目标拆 2-4 个**互相独立、可并行**的子任务,别拆成依赖链。

输出规则:**只输出 JSON 本身**,不要任何前后文字,不要用 ``` 代码块包裹 JSON。照下面两种格式之一:

简单任务直接答:
{"direct": "你的回答"}

工作任务派单(数组,每元素一个派单):
[{"agent":"coder","instruction":"【目标】用单文件 HTML 做贪吃蛇。【要求】纯前端零依赖、方向键控制。【验收标准】1.双击即玩;2.含计分与游戏结束重开;3.全部代码在一个 .html。【输出格式】单个 ```html 代码块 + 结尾一行 SUMMARY。"}]"""

REVIEW_SYSTEM = """你是 CrewOS 总指挥,现在做交付验收(不是夸产物)。
给你:原始目标、各成员产出预览、机器检查结果(checks)。

判定规则(必须遵守):
- check_failures 非空 → 结论只能是 ⚠️ 或 ❌;在「缺口」里逐条引用失败项,点名是谁、改哪条验收标准;禁止给 ✅。
- check_failures 为空但产出缺失/明显不满足验收标准 → 同样标 ❌ 并说明原因。
- 只有产出齐全且满足各自验收标准 → 才可 ✅。

用中文输出 2-5 句:
1. 结论:✅ 达成 / ⚠️ 部分达成 / ❌ 未达成。
2. 逐成员:是否满足其验收标准 + 机器检查是否标红。
3. 缺口:还差什么(具体、可执行);需返工就点名谁、改哪里。
直说结论,不复述产物内容。"""


def _roster_desc(router) -> str:
    from .providers import RECOMMENDATIONS
    lines = []
    for name in router.list_agents():
        rec = RECOMMENDATIONS.get(name, {})
        lines.append(f"- {name}: {rec.get('reason', '')[:40]}")
    return "\n".join(lines)


def parse_direct(text: str) -> str | None:
    """CEO 判断任务简单到自己能答时返回 {"direct": "..."};解析出答案则返回它。"""
    body = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", body, re.DOTALL)
    if m:
        body = m.group(1).strip()
    m = re.search(r"\{.*\}", body, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    d = obj.get("direct") if isinstance(obj, dict) else None
    return str(d).strip() if d else None


def parse_plan(text: str, valid_agents: list[str]) -> list[dict]:
    """从 CEO 产出里抽出派单计划。容错:剥代码块、找数组、单对象当 1 单、校验 agent 名。"""
    body = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", body, re.DOTALL)
    if m:
        body = m.group(1).strip()
    arr = None
    m = re.search(r"\[.*\]", body, re.DOTALL)   # 优先找数组
    if m:
        try:
            arr = json.loads(m.group(0))
        except (ValueError, TypeError):
            arr = None
    if arr is None:                              # 便宜模型常把单个派单写成裸对象 {agent,instruction}
        m = re.search(r"\{.*\}", body, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict) and obj.get("agent"):
                    arr = [obj]
            except (ValueError, TypeError):
                arr = None
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
                temperature: float = 0.3, root: str = ".",
                step_max_tokens: int = 16000, settings: dict | None = None) -> dict:
    """跑一次 Web 端 CEO 编排。返回 {task_id, plan, summary, degraded, deliverables}。
    step_max_tokens 给得足够大,避免推理模型把额度烧在思维链上、正文被截空(马里奥游戏类大产出尤甚)。
    settings:用于连接 U 第二大脑 —— 派单前从 U 召回相关记忆注入,结案把经历蒸馏回 U。"""
    led = router.ledger
    task_id = task_id or led.new_task(goal[:80], created_by="user")
    led.log(task_id, "task_assign", "user", "ceo", payload={"summary": goal})

    ch = Channel(name="ceo", endpoint=endpoint, key_env=key_env)
    roster = router.list_agents()
    fallback_agent = fallback_agent if fallback_agent in roster else (roster[0] if roster else "")

    # 0) 从 U(主人第二大脑)按需召回与目标相关的记忆 —— 规划与派单都带上,让团队"知道"主人沉淀的一切
    u_ctx = umemory.recall(goal, settings)
    if u_ctx:
        led.log(task_id, "status_update", "ceo", payload={
            "summary": "已从 U 第二大脑召回相关记忆,注入本次规划与派单"})

    # 1) 规划
    try:
        plan_user = f"目标:{goal}\n\n团队名册:\n{_roster_desc(router)}"
        if u_ctx:
            plan_user += "\n\n" + u_ctx
        plan_resp = _call_openai_compatible(
            ch, ceo_model,
            [{"role": "system", "content": PLAN_SYSTEM},
             {"role": "user", "content": plan_user}],
            temperature, 2500)   # 给推理模型留足额度,4 单全段 JSON 不会被截断
        # 简单任务:CEO 直接答,不动用任何 agent
        direct = parse_direct(plan_resp["content"])
        if direct:
            led.log(task_id, "task_done", "ceo", "user", payload={
                "summary": direct, "direct": True})
            return {"task_id": task_id, "plan": [], "summary": direct,
                    "direct": True, "degraded": False}
        plan = parse_plan(plan_resp["content"], roster)
    except Exception as e:
        led.log(task_id, "escalation", "ceo", "user", payload={
            "reason": f"CEO 规划调用失败:{str(e)[:160]}",
            "summary": "CEO 模型不可用,请在 CONFIG 配置可用的 CEO 模型与供应商 key"})
        return {"task_id": task_id, "plan": [], "summary": "CEO 模型不可用", "degraded": True}

    degraded = not plan
    if degraded:                       # 解析不出计划(如 mock/弱模型)→ 兜底派给一个 agent
        plan = [{"agent": fallback_agent, "instruction": goal}]
        # 记录 CEO 实际产出的原文(截断),便于排查到底是哪种格式没被解析出来
        raw = (plan_resp.get("content") or "")[:300] if isinstance(plan_resp, dict) else ""
        led.log(task_id, "review_feedback", "ceo", fallback_agent, payload={
            "summary": "未能解析出多步计划,降级为单点派单(配强 CEO 模型可获得真正拆解)",
            "raw_plan": raw})

    led.log(task_id, "status_update", "ceo", payload={
        "summary": f"CEO 已规划 {len(plan)} 个派单:" + "、".join(p["agent"] for p in plan)})

    # 2) 执行派单 —— 完整产出落盘(不截断),预览喂给汇总
    results = []
    deliverables = []          # [(agent, [相对路径...])]
    for step in plan:
        try:
            r = router.dispatch(step["agent"], step["instruction"], context=u_ctx,
                                task_id=task_id, max_tokens=step_max_tokens)
            content = r.get("content", "") or ""
            if r.get("empty") or not content.strip():     # 空产出:把原因如实带给汇总,别假装成功
                why = r.get("empty_reason") or "成员未产出正文(可能 max_tokens 不足或只输出了思维链)"
                results.append(f"【{step['agent']}】⚠ 无产出:{why}")
                continue
            paths = save_deliverable(root, task_id, step["agent"], content)   # 完整存盘
            deliverables.append((step["agent"], paths))
            preview = content[:800]
            # 机器检查结果一并喂给验收(CEO 据此判定是否标红/返工)
            chk = r.get("checks") or {}
            fails = chk.get("failures") or []
            chkline = (f"  [机器检查:{'全过 ✓' if not fails else '标红 ✗ → '+'; '.join(map(str, fails))[:160]}]"
                       if (chk.get("passed") is not None or fails) else "")
            results.append(f"【{step['agent']} · 验收依据见其 instruction】{preview}"
                           + ("…(完整产出已存盘)" if len(content) > 800 else "") + chkline)
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

    # 把交付文件路径附到结论里(看板会把 deliverables/ 路径渲染成可点链接,直接打开/运行)
    flat = [p for _, ps in deliverables for p in ps]
    if deliverables:
        lines = []
        for ag, ps in deliverables:
            runnable = [p for p in ps if not p.endswith(".md")] or ps
            lines.append(f"{ag}: " + " ".join("/" + p for p in runnable))
        summary = (summary + "\n\n📦 交付文件(点开即用):\n" + "\n".join(lines)).strip()

    # 写回 U:把这次编码经历蒸馏成一行追加到 HOT 层当天 daily(升华单向 库→wiki 的入口)
    u_path = umemory.remember(
        goal[:60],
        f"目标:{goal}\n派单:{'、'.join(p['agent'] for p in plan)}\n结论:{summary[:400]}"
        + (f"\n交付:{', '.join(flat)}" if flat else ""),
        settings)
    if u_path:
        led.log(task_id, "status_update", "ceo", payload={
            "summary": "本次编码经历已蒸馏写回 U 第二大脑(待每晚 distill 升华进知识图)"})

    led.log(task_id, "task_done", "ceo", "user",
            payload={"summary": summary, "deliverables": flat})
    return {"task_id": task_id, "plan": plan, "summary": summary,
            "degraded": degraded, "deliverables": flat, "u_written": bool(u_path)}

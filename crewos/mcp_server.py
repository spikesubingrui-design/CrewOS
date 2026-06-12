"""crewos MCP server — 把团队暴露给 Claude Code(总指挥)。

安装到 Claude Code:
    claude mcp add crewos -- python -m crewos.mcp_server --root /path/to/crewos

CC 可用工具:
    list_agents()        团队名册与各通道健康状态
    dispatch(...)        派单给指定 agent
    log_event(...)       记录审阅意见/复盘/上报(审阅打回自动写入错题本)
    request_action(...)  外部动作放行申请(风险分级 L0-L4)
    wait_action(...)     阻塞等待 L3/L4 审批裁决
    add_lesson(...)      复盘教训写入 agent 错题本
    task_replay(...)     回放任务全过程
    cost_report()        成本报表
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import memory as memvault
from .ledger import Ledger
from .memory import append_lesson
from .risk import RiskEngine
from .router import AllChannelsDown, BudgetExceeded, Router

mcp = FastMCP("crewos")
_router: Router | None = None
_ledger: Ledger | None = None
_risk: RiskEngine | None = None
_agents_dir: Path | None = None
_root: Path | None = None


@mcp.tool()
def list_agents() -> str:
    """列出团队全部 agent 及其通道健康状态(在线/离线)。派单前先看名册。"""
    out = {}
    for name in _router.list_agents():
        out[name] = _router.heartbeat(name)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def dispatch(agent: str, instruction: str, context: str = "",
             task_id: str = "", round: int = 0,
             budget_usd: float = 0.0, media_url: str = "") -> str:
    """派单给指定 agent。instruction 必须含目标+验收标准+格式要求。
    task_id 留空则新建任务;重派(审阅不合格)时传原 task_id 并 round+1。
    budget_usd 为 0 时使用默认任务预算上限。
    media_url:视频/图片直链(给 perceiver 做视频理解时必传,多模态消息格式)。"""
    try:
        result = _router.dispatch(
            agent, instruction, context=context, task_id=task_id,
            round=round, budget_usd=budget_usd or None, media_url=media_url)
        return json.dumps(result, ensure_ascii=False)
    except BudgetExceeded as e:
        return json.dumps({"error": "budget_exceeded", "detail": str(e),
                           "action_required": "上报用户,等待提额"}, ensure_ascii=False)
    except AllChannelsDown as e:
        return json.dumps({"error": "all_channels_down", "detail": str(e),
                           "action_required": "上报用户:等待恢复 或 授权交接式换模型"},
                          ensure_ascii=False)


@mcp.tool()
def log_event(task_id: str, type: str, summary: str,
              to_agent: str = "", round: int = 0) -> str:
    """记录事件到台账。type 可选:review_feedback(审阅意见)/
    retrospect(复盘结论)/escalation(上报用户)/task_done/task_failed。
    review_feedback 且指明 to_agent 时,打回原因自动追加到该 agent 错题本。"""
    event_id = _ledger.log(task_id, type, "ceo", to_agent,
                           round=round, payload={"summary": summary})
    out = {"event_id": event_id}
    if type == "review_feedback" and to_agent:
        try:
            append_lesson(_agents_dir, to_agent, f"审阅打回:{summary}",
                          task_id=task_id, round=round)
            _ledger.log(task_id, "lesson_saved", "ceo", to_agent, round=round,
                        payload={"summary": f"打回原因已记入 {to_agent} 错题本"})
            out["lesson_saved"] = True
        except FileNotFoundError:
            out["lesson_saved"] = False
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def request_action(agent: str, action: str, summary: str, task_id: str = "") -> str:
    """外部动作放行申请(风险分级)。执行任何对外动作(发布/推送/发邮件/删文件等)前必须调用。
    返回 approved=true 才能执行;status=pending 时用 wait_action 等裁决。
    L0/L1 静默放行,L2 放行并通知用户,L3 倒计时(无人反对自动放行),L4 必须用户批准。"""
    return json.dumps(_risk.request(agent, action, summary, task_id=task_id),
                      ensure_ascii=False)


@mcp.tool()
def wait_action(approval_id: str, timeout_seconds: float = 120) -> str:
    """阻塞等待审批单裁决(L3 倒计时结束自动放行;L4 等用户在看板/CLI 批准)。
    返回 status: approved/auto_approved/denied/pending(超时仍未裁决)。"""
    return json.dumps(_risk.wait(approval_id, timeout=timeout_seconds),
                      ensure_ascii=False)


@mcp.tool()
def add_lesson(agent: str, lesson: str, task_id: str = "", round: int = 0) -> str:
    """复盘时把教训写入指定 agent 的错题本(下次任务自动注入其上下文)。
    lesson 写"错误 → 修正后的做法",一条一个教训。"""
    try:
        f = append_lesson(_agents_dir, agent, lesson, task_id=task_id, round=round)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    _ledger.log(task_id or "system", "lesson_saved", "ceo", agent, round=round,
                payload={"summary": lesson[:200]})
    return json.dumps({"ok": True, "file": str(f)}, ensure_ascii=False)


@mcp.tool()
def memory_search(query: str) -> str:
    """检索 Memory Tree(跨项目永久记忆)。接新任务前必查:有没有同类项目经验。
    query 用空格分隔关键词。返回命中文件与片段。"""
    hits = memvault.vault_search(_root, query)
    return json.dumps(hits or {"hint": "无命中。结案时记得用 memory_write 沉淀复盘。"},
                      ensure_ascii=False)


@mcp.tool()
def memory_read(path: str) -> str:
    """读取 Memory Tree 中的一个文件(相对 memory/ 的路径,如 projects/xxx.md)。"""
    try:
        return memvault.vault_read(_root, path)
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def memory_write(path: str, content: str, mode: str = "append") -> str:
    """写入 Memory Tree。项目复盘写 projects/<项目名>.md,可复用方法写 knowledge/<主题>.md。
    复盘格式:背景 / 做了什么 / 踩了什么坑 / 下次怎么做。mode: append(默认)或 overwrite。"""
    try:
        f = memvault.vault_write(_root, path, content, mode=mode)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    _ledger.log("system", "status_update", "ceo", payload={
        "summary": f"Memory Tree 更新: memory/{path}"})
    return json.dumps({"ok": True, "file": str(f)}, ensure_ascii=False)


@mcp.tool()
def eval_report() -> str:
    """模型胜任度报表(评估集自动生长):各 agent×模型的任务数/一次过率/
    平均轮次/上报数/成本。考虑换模型时先看这个。"""
    return json.dumps(_ledger.eval_report(), ensure_ascii=False)


@mcp.tool()
def task_replay(task_id: str) -> str:
    """回放某任务的完整事件流(人类可读),含每步成本。"""
    return _ledger.replay(task_id)


@mcp.tool()
def cost_report() -> str:
    """成本报表:总额、按 agent、按模型。"""
    return json.dumps(_ledger.cost_report(), ensure_ascii=False)


def _bootstrap(root: Path, budget: float):
    global _router, _ledger, _risk, _agents_dir, _root
    _root = root
    _agents_dir = root / "agents"
    _ledger = Ledger(root / "data" / "ledger.db")
    _risk = RiskEngine(_agents_dir, _ledger)
    blocklist_file = root / "config" / "dlp_blocklist.txt"
    blocklist = (
        [l.strip() for l in blocklist_file.read_text(encoding="utf-8").splitlines()
         if l.strip() and not l.startswith("#")]
        if blocklist_file.exists() else []
    )
    _router = Router(_agents_dir, _ledger,
                     default_task_budget_usd=budget,
                     dlp_blocklist=blocklist)


def serve(root: Path, budget: float | None = None):
    """被 crewos mcp 调用。budget=None 时读工作区 settings.yaml。"""
    import yaml
    from .workspace import load_env
    root = Path(root)
    load_env(root)
    if budget is None:
        sf = root / "config" / "settings.yaml"
        cfg = yaml.safe_load(sf.read_text(encoding="utf-8")) if sf.exists() else {}
        budget = float((cfg or {}).get("default_task_budget_usd", 2.0))
    _bootstrap(root, budget)
    mcp.run()


def main():
    from .workspace import DEFAULT_WS
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(DEFAULT_WS), help="crewos 工作区目录")
    parser.add_argument("--budget", type=float, default=2.0, help="默认任务预算上限(USD)")
    args = parser.parse_args()
    _bootstrap(Path(args.root).expanduser(), args.budget)
    mcp.run()


if __name__ == "__main__":
    main()

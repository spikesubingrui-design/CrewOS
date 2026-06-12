"""crewos 命令行 — Hermes 式体验:onboard 一次,start 一条命令。

  crewos onboard          交互式初始化(工作区 + API keys)
  crewos start            启动 Mission Control 看板(自动开浏览器)
  crewos status           乘组名册 + 通道健康
  crewos cost             成本报表
  crewos replay <task>    任务回放
  crewos dispatch <agent> "<指令>"
  crewos mcp              以 MCP 服务运行(供 claude mcp add 使用)
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import webbrowser
from pathlib import Path

from .workspace import DEFAULT_WS, KEY_SPECS, ensure_workspace, load_env, write_env

BANNER = r"""
   █▀▀ █▀█ █▀▀ █ █ █ █▀█ █▀▀
   █▄▄ █▀▄ ██▄ ▀▄▀▄▀ █▄█ ▄▄█   MISSION CONTROL
"""


def _ws(args) -> Path:
    ws = Path(args.workspace).expanduser()
    load_env(ws)
    return ws


def cmd_onboard(args):
    ws = Path(args.workspace).expanduser()
    print(BANNER)
    print(f"▸ 工作区: {ws}")
    created = ensure_workspace(ws)
    print(f"▸ 部署档案: 新建 {len(created)} 项" + (",已有档案全部保留" if not created else ""))

    if args.defaults:
        print("▸ --defaults: 跳过 key 录入(稍后可编辑 ~/.crewos/.env)")
    else:
        print("\n▸ 配置 API keys(直接回车跳过,稍后可改 ~/.crewos/.env 或在网页 CONFIG 查看状态)")
        keys = {}
        for name, desc in KEY_SPECS:
            try:
                v = getpass.getpass(f"  {name}  {desc}\n  > ")
            except (EOFError, KeyboardInterrupt):
                print("\n  (跳过剩余)")
                break
            if v.strip():
                keys[name] = v.strip()
        write_env(ws, keys)
        print(f"▸ 已写入 {ws / '.env'}(权限 600)")

    print(f"""
✓ 就绪。下一步:

  crewos start                          # 打开 Mission Control
  claude mcp add crewos -- crewos mcp   # 让 Claude Code (CC) 上岗当总指挥

没配 key 也可以先 start,点 RUN SIMULATION 体验全链路。""")


def cmd_start(args):
    ws = _ws(args)
    ensure_workspace(ws)
    url = f"http://127.0.0.1:{args.port}"
    print(BANNER)
    print(f"▸ 工作区 {ws}\n▸ Mission Control: {url}\n▸ Ctrl-C 停止\n")
    if not args.no_browser:
        import threading
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    from .web_server import run
    run(ws, args.port)


def cmd_mcp(args):
    ws = _ws(args)
    ensure_workspace(ws)
    from . import mcp_server
    mcp_server.serve(ws, budget=None)


def _router(ws):
    from .ledger import Ledger
    from .router import Router
    ledger = Ledger(ws / "data" / "ledger.db")
    return Router(ws / "agents", ledger), ledger


def cmd_status(args):
    router, _ = _router(_ws(args))
    for name in router.list_agents():
        hb = router.heartbeat(name)
        dots = "  ".join(f"{'●' if ok else '○'} {ch}" for ch, ok in hb.items())
        print(f"  {name:<12} {dots}")


def cmd_cost(args):
    _, ledger = _router(_ws(args))
    print(json.dumps(ledger.cost_report(), ensure_ascii=False, indent=2))


def cmd_replay(args):
    _, ledger = _router(_ws(args))
    print(ledger.replay(args.task_id))


def cmd_dispatch(args):
    router, _ = _router(_ws(args))
    print(json.dumps(router.dispatch(args.agent, args.instruction),
                     ensure_ascii=False, indent=2))


def _risk_engine(ws):
    from .risk import RiskEngine
    _, ledger = _router(ws)
    return RiskEngine(ws / "agents", ledger)


def cmd_approvals(args):
    pending = _risk_engine(_ws(args)).pending()
    if not pending:
        print("  无待审批动作")
        return
    for p in pending:
        left = f" · 倒计时 {p['seconds_left']:.0f}s 后自动放行" if p.get("seconds_left") is not None else " · 等待明确批准"
        print(f"  [{p['id']}] L{p['risk']} {p['agent']}.{p['action']}{left}\n"
              f"      {p['summary']}")
    print("\n  裁决: crewos approve <id> / crewos deny <id>")


def cmd_decide(args):
    res = _risk_engine(_ws(args)).decide(args.approval_id, args.fn_approve)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(prog="crewos", description="CrewOS — AI 团队 Mission Control")
    p.add_argument("--workspace", "-w", default=str(DEFAULT_WS),
                   help=f"工作区目录(默认 {DEFAULT_WS})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("onboard", help="初始化工作区与 API keys")
    s.add_argument("--defaults", action="store_true", help="非交互,全部默认")
    s.set_defaults(fn=cmd_onboard)

    s = sub.add_parser("start", help="启动 Mission Control 看板")
    s.add_argument("--port", type=int, default=8466)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("mcp", help="MCP 服务模式(供 Claude Code)")
    s.set_defaults(fn=cmd_mcp)

    sub.add_parser("status", help="乘组与通道健康").set_defaults(fn=cmd_status)
    sub.add_parser("cost", help="成本报表").set_defaults(fn=cmd_cost)

    s = sub.add_parser("replay", help="任务回放")
    s.add_argument("task_id")
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("dispatch", help="手动派单(调试)")
    s.add_argument("agent")
    s.add_argument("instruction")
    s.set_defaults(fn=cmd_dispatch)

    sub.add_parser("approvals", help="待审批动作(L3/L4)").set_defaults(fn=cmd_approvals)
    s = sub.add_parser("approve", help="批准动作")
    s.add_argument("approval_id")
    s.set_defaults(fn=cmd_decide, fn_approve=True)
    s = sub.add_parser("deny", help="否决动作")
    s.add_argument("approval_id")
    s.set_defaults(fn=cmd_decide, fn_approve=False)

    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        print("\n▸ 已停止")
        sys.exit(0)


if __name__ == "__main__":
    main()

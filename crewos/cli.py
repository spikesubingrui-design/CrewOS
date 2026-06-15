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

    print("""
✓ 就绪。下一步:

  crewos start                          # 打开 Mission Control
  claude mcp add crewos -- crewos mcp   # 让 Claude Code (CC) 上岗当总指挥

配 key 最省事的方式:crewos start 打开看板 → CONFIG → 供应商面板,挑一个供应商
(推荐 OpenRouter,一把覆盖大多数模型)粘上 API key 点保存,即时生效。
每个 agent 旁边都有推荐模型与理由,点「用推荐」一键绑定。也支持自定义供应商。
想先看界面动效不花钱:在线宣传页 https://spikesubingrui-design.github.io/CrewOS/""")


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
    run(ws, args.port, host=getattr(args, "host", None))


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
        paused = " [PAUSED]" if router.agent_status(name) == "paused" else ""
        print(f"  {name:<12} {dots}{paused}")


def cmd_pause(args):
    router, _ = _router(_ws(args))
    router.pause_agent(args.agent, "CLI 手动暂停")
    print(f"  {args.agent} 已暂停。crewos resume {args.agent} 恢复。")


def cmd_resume(args):
    router, _ = _router(_ws(args))
    router.resume_agent(args.agent)
    print(f"  {args.agent} 已恢复。")


def cmd_doctor(args):
    from .doctor import diagnose
    rows = diagnose(_router(_ws(args))[1])
    if not rows:
        print("  ✓ 无停工/异常任务,一切正常。")
        return
    for r in rows:
        print(f"  [{r['severity']}] {r['task_id']} — {r['cause']}\n      {r['detail']}")
        if r.get("suggestion"):
            print(f"      → {r['suggestion']}")


def cmd_cost(args):
    _, ledger = _router(_ws(args))
    print(json.dumps(ledger.cost_report(), ensure_ascii=False, indent=2))


def cmd_memory(args):
    """查看 U 第二大脑连接状态;--recall <词> 测试知识图召回。"""
    from . import umemory
    ws = _ws(args)
    sf = Path(ws) / "config" / "settings.yaml"
    settings = {}
    if sf.exists():
        import yaml as _yaml
        settings = _yaml.safe_load(sf.read_text(encoding="utf-8")) or {}
    st = umemory.status(settings)
    ok = lambda b: "✓" if b else "✗"
    print(f"  U 第二大脑:{'已连接' if st['connected'] else '未连接'}(enabled={st['enabled']})")
    print(f"  {ok(st['hot_ok'])} HOT 工作记忆库   {st['hot_dir']}")
    print(f"  {ok(st['wiki_ok'])} WARM 永久知识图  {st['wiki_dir']}")
    print(f"  {ok(st['gbrain_ok'])} gbrain 召回引擎  {st['gbrain']}")
    if args.recall:
        print(f"\n  ── gbrain 召回「{args.recall}」──")
        print(umemory.recall(args.recall, settings) or "  (无相关记忆)")


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


def cmd_jobs(args):
    from .cron import load_jobs
    jobs = load_jobs(_ws(args))
    if not jobs:
        print("  无定时任务(编辑 config/crontab.yaml 添加)")
        return
    for j in jobs:
        flag = "●" if j.get("enabled", True) else "○"
        print(f"  {flag} {j.get('name','?'):<20} {j.get('schedule','?'):<14} "
              f"{j.get('agent','?'):<11} {j.get('instruction','')[:50]}")
    print("\n  ● 启用 ○ 停用 — 调度器随 crewos start 运行,改 yaml 即生效")


def cmd_evals(args):
    _, ledger = _router(_ws(args))
    rep = ledger.eval_report()
    print(f"  评估样本(任务)总数: {rep['samples']}\n")
    print(f"  {'AGENT':<12}{'MODEL':<22}{'任务':<6}{'一次过':<8}{'均轮次':<8}{'上报':<6}成本USD")
    for r in rep["by_agent_model"]:
        print(f"  {r['agent']:<12}{r['model']:<22}{r['tasks']:<6}"
              f"{r['first_pass_rate']:<8.0%}{r['avg_rounds']:<8}{r['escalations']:<6}"
              f"{r['cost_usd']:.4f}")


def cmd_eval_behavior(args):
    """离线行为评估:每个 agent 用自己绑定的模型跑一遍合规用例,看谁稳。"""
    from .behavioral import load_cases, run_suite
    from .router import load_agent
    ws = _ws(args)
    cases = load_cases(args.cases)
    models = []
    for name in sorted(d.name for d in (ws / "agents").iterdir()
                       if d.is_dir() and (d / "provider.yaml").exists()):
        a = load_agent(ws / "agents", name)
        if not a.channels:
            continue
        ch = a.channels[0]
        models.append({"name": name, "model": a.model,
                       "endpoint": ch.endpoint, "key_env": ch.key_env})
    print(f"  跑 {len(cases)} 个行为用例 × {len(models)} 个模型(用各 agent 首选通道)…\n")
    rep = run_suite(models, cases)
    print(f"  {'AGENT':<12}{'MODEL':<22}{'合规率':<8}{'通过/总'}")
    for name, r in rep["models"].items():
        print(f"  {name:<12}{r['model']:<22}{r['compliance_rate']*100:>5.0f}%   {r['passed']}/{r['total']}")
        for cid, cr in r["cases"].items():
            if not cr["passed"]:
                print(f"       ✗ {cid}: {'; '.join(cr['failures'])[:80]}")


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
    s.add_argument("--host", default=None,
                   help="绑定地址(默认 127.0.0.1;绑非环回地址需先设 dashboard_token)")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("mcp", help="MCP 服务模式(供 Claude Code)")
    s.set_defaults(fn=cmd_mcp)

    sub.add_parser("status", help="乘组与通道健康").set_defaults(fn=cmd_status)
    sub.add_parser("cost", help="成本报表").set_defaults(fn=cmd_cost)
    s = sub.add_parser("memory", help="U 第二大脑连接状态(--recall 测召回)")
    s.add_argument("--recall", default="", help="测试从 U 知识图召回某主题")
    s.set_defaults(fn=cmd_memory)
    sub.add_parser("doctor", help="诊断停工/异常任务(--why-stopped)").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("pause", help="暂停某 agent(阻断派单)")
    s.add_argument("agent")
    s.set_defaults(fn=cmd_pause)
    s = sub.add_parser("resume", help="恢复某 agent")
    s.add_argument("agent")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("replay", help="任务回放")
    s.add_argument("task_id")
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("dispatch", help="手动派单(调试)")
    s.add_argument("agent")
    s.add_argument("instruction")
    s.set_defaults(fn=cmd_dispatch)

    sub.add_parser("jobs", help="定时任务列表(crontab.yaml)").set_defaults(fn=cmd_jobs)
    sub.add_parser("evals", help="模型胜任度报表(线上遥测)").set_defaults(fn=cmd_evals)
    s = sub.add_parser("eval-behavior", help="离线行为评估(派真单前测合规率)")
    s.add_argument("--cases", default="", help="自定义用例 yaml(默认内置 5 例)")
    s.set_defaults(fn=cmd_eval_behavior)
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

"""CrewOS Web 看板后端 — 透明玻璃办公室。

启动: python -m crewos.web_server --root . --port 8466
设计要点:
- 只绑 127.0.0.1(OpenClaw 4 万实例暴露公网的教训),远程访问走加密隧道
- 实时推送 = 后台线程轮询 ledger.db 新事件 → WebSocket 广播
  (MCP 服务/CLI/Web 是独立进程,共享同一个 SQLite,天然解耦)
- 设置中心可编辑的文件走白名单,杜绝任意路径读写
"""
from __future__ import annotations

import argparse
import asyncio
import json
import hmac
import os
import re
import sys
import threading
import time
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .cron import CronScheduler, due
from .ledger import Ledger
from .notify import push as notify_push
from .risk import RiskEngine
from .router import (AgentPaused, AllChannelsDown, BudgetExceeded,
                     InboundSensitive, Router, load_agent)

ROOT = Path(".")
app = FastAPI(title="CrewOS")
_clients: set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None

# 设置中心允许编辑的文件白名单(相对 ROOT)
EDITABLE = re.compile(
    r"^(CrewOS\.md|config/dlp_blocklist\.txt|config/settings\.yaml|config/crontab\.yaml|"
    r"memory/[\w\-./一-鿿]+\.md|"
    r"agents/[a-z_]+/(role\.md|provider\.yaml|actions\.yaml|memory/[a-z_]+\.md))$"
)


def _settings() -> dict:
    f = ROOT / "config" / "settings.yaml"
    base = {"default_task_budget_usd": 2.0, "monthly_warn_usd": 120.0,
            "feishu_webhook": "", "webhook_url": "", "dashboard_token": "",
            "watchdog_suspicious_minutes": 5.0, "watchdog_critical_minutes": 15.0,
            "monthly_hard_usd": 0.0, "ceo_model": "", "ceo_provider": "",
            "cny_rate": 7.2}
    if f.exists():
        base.update(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    return base


# ---------- 可选 token 认证(从局域网/隧道访问时必配) ----------

def _authed(token: str, provided: str | None) -> bool:
    if not token:
        return True
    return hmac.compare_digest(str(provided or ""), token)   # 恒定时间比较,防计时侧信道


@app.middleware("http")
async def _auth_middleware(request, call_next):
    token = str(_settings().get("dashboard_token") or "")
    if token:
        provided = (request.query_params.get("token")
                    or request.headers.get("x-crewos-token")
                    or request.cookies.get("crewos_token"))
        if not _authed(token, provided):
            return JSONResponse({"error": "unauthorized",
                                 "hint": "携带 ?token=<dashboard_token> 访问一次即记住"},
                                status_code=401)
    resp = await call_next(request)
    if token and request.query_params.get("token") == token:
        resp.set_cookie("crewos_token", token, httponly=True, samesite="strict")
    return resp


def _blocklist() -> list[str]:
    f = ROOT / "config" / "dlp_blocklist.txt"
    if not f.exists():
        return []
    return [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


_LEDGER_CACHE: dict = {}


def _ledger() -> Ledger:
    # 复用单条 SQLite 连接(按 db 路径缓存)——避免每个请求/后台循环新建连接(fd 浪费 + WAL 抖动)。
    # 按路径缓存而非纯单例:测试切换 ROOT 到临时目录时各得各的 ledger,互不串扰。
    key = str((ROOT / "data" / "ledger.db").resolve())
    led = _LEDGER_CACHE.get(key)
    if led is None:
        led = Ledger(ROOT / "data" / "ledger.db")
        _LEDGER_CACHE[key] = led
    return led


def _router() -> Router:
    # Router 仍每次新建以拿到最新配置(预算/DLP 黑名单可在线编辑),但复用共享 Ledger 连接。
    s = _settings()
    return Router(ROOT / "agents", _ledger(),
                  default_task_budget_usd=float(s["default_task_budget_usd"]),
                  dlp_blocklist=_blocklist())


def _risk() -> RiskEngine:
    return RiskEngine(ROOT / "agents", _ledger())


# ---------- 实时事件广播(轮询台账 → WS) ----------

def _poll_events():
    led = _ledger()
    last_ts = time.time()
    while True:
        time.sleep(0.8)
        try:
            rows = led._conn.execute(
                "SELECT * FROM events WHERE ts>? ORDER BY ts", (last_ts,)
            ).fetchall()
            settings = _settings() if rows else {}
            for r in rows:
                last_ts = max(last_ts, r["ts"])
                msg = json.dumps(dict(r), ensure_ascii=False)
                for ws in list(_clients):
                    if _loop:
                        asyncio.run_coroutine_threadsafe(_safe_send(ws, msg), _loop)
                notify_push(settings, dict(r))
        except Exception as exc:   # 不要静默吞掉:打到 stderr 便于排查事件投递故障
            print(f"[poll_events] {type(exc).__name__}: {exc}", file=sys.stderr)


async def _safe_send(ws: WebSocket, msg: str):
    try:
        await ws.send_text(msg)
    except Exception:
        _clients.discard(ws)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token = str(_settings().get("dashboard_token") or "")
    provided = ws.query_params.get("token") or ws.cookies.get("crewos_token")
    if not _authed(token, provided):
        await ws.close(code=4401)
        return
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _clients.discard(ws)


# ---------- 后台线程:心跳探测 + 月度成本警戒 ----------

def _heartbeat_loop():
    """每 30s 探测全员通道,结果进缓存表;状态翻转事件由 Router 写台账。"""
    while True:
        try:
            router = _router()
            for name in router.list_agents():
                router.heartbeat(name)
        except Exception:
            pass
        time.sleep(30)


def _watchdog_loop():
    """每 60s 扫停滞任务(派单后久无产出),分级上报。便宜模型易跑飞,这层补单次 timeout 之外的整任务级停滞检测。"""
    from .watchdog import Watchdog
    while True:
        try:
            s = _settings()
            Watchdog(_ledger(),
                     suspicious_minutes=float(s["watchdog_suspicious_minutes"]),
                     critical_minutes=float(s["watchdog_critical_minutes"])).tick()
        except Exception:
            pass
        time.sleep(60)


def _monthly_watch_loop():
    """月度成本越过警戒线时上报一次(每月只报一次,推送走通知网关)。"""
    warned_month = ""
    while True:
        try:
            t = time.localtime()
            month = f"{t.tm_year}-{t.tm_mon:02d}"
            month_start = time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
            s = _settings()
            total = _ledger().cost_report(since_ts=month_start)["total_usd"]
            warn_at = float(s["monthly_warn_usd"])
            if month != warned_month and warn_at > 0 and total >= warn_at:
                warned_month = month
                _ledger().log("system", "escalation", "router", "user", payload={
                    "reason": f"本月成本 ${total:.2f} 已越过警戒线 ${warn_at:.2f}",
                    "summary": f"月度警戒:{month} 已花 ${total:.2f}(警戒线 ${warn_at:.2f})。"
                               f"请检查成本仪表盘,必要时调低任务预算或停用定时任务。"})
            # 月度硬上限:撞线把全员置 paused,阻断后续派单,直到人工恢复(钱做成硬刹车)
            hard = float(s.get("monthly_hard_usd") or 0)
            if hard > 0 and total >= hard:
                router = _router()
                for name in router.list_agents():
                    if router.agent_status(name) == "active":
                        router.pause_agent(
                            name, f"本月成本 ${total:.2f} 撞月度硬上限 ${hard:.2f},全员熔断",
                            by="budget")
        except Exception:
            pass
        time.sleep(600)


# ---------- API ----------

@app.get("/api/agents")
def api_agents():
    out = []
    router = _router()
    for name in router.list_agents():
        a = load_agent(ROOT / "agents", name)
        out.append({
            "name": name, "model": a.model,
            "channels": [{"name": c.name, "endpoint": c.endpoint,
                          "key_env": c.key_env, "key_set": bool(c.api_key)}
                         for c in a.channels],
            "fallback_model": a.fallback_model,
            "price_in": a.price_in_per_m, "price_out": a.price_out_per_m,
            "temperature": a.temperature,
            "status": router.agent_status(name),
        })
    return out


@app.post("/api/agent/{name}/pause")
def api_agent_pause(name: str):
    r = _router()
    if name not in r.list_agents():
        return JSONResponse({"error": "unknown_agent"}, status_code=404)
    r.pause_agent(name, "看板手动暂停")
    return {"ok": True, "agent": name, "status": "paused"}


@app.post("/api/agent/{name}/resume")
def api_agent_resume(name: str):
    r = _router()
    if name not in r.list_agents():
        return JSONResponse({"error": "unknown_agent"}, status_code=404)
    r.resume_agent(name)
    return {"ok": True, "agent": name, "status": "active"}


@app.get("/api/heartbeat/{agent}")
def api_heartbeat(agent: str):
    return _router().heartbeat(agent, max_age=60)


@app.get("/api/recent")
def api_recent(limit: int = 60):
    """最近 N 条事件(正序),供看板加载时回填遥测流——开页不再像假死。"""
    led = _ledger()
    rows = led._conn.execute(
        "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/tasks")
def api_tasks(limit: int = 50):
    led = _ledger()
    # 排除 system 伪任务(心跳/换脑/记忆等元事件挂它名下,不是真任务)
    rows = led._conn.execute("""
        SELECT task_id,
               MIN(ts) created,
               MAX(ts) updated,
               SUM(cost_usd) cost,
               MAX(round) rounds,
               (SELECT json_extract(payload,'$.title') FROM events e2
                 WHERE e2.task_id=e.task_id AND e2.type='task_created') title,
               (SELECT GROUP_CONCAT(type) FROM events e3 WHERE e3.task_id=e.task_id) types
        FROM events e WHERE task_id != 'system'
        GROUP BY task_id ORDER BY updated DESC LIMIT ?""", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        types = (r["types"] or "").split(",")
        if "task_done" in types:
            status = "done"
        elif "task_failed" in types or "escalation" in types:
            status = "blocked"
        elif "task_result" in types:
            status = "review"
        elif "task_assign" in types:
            status = "working"
        else:
            status = "pending"
        out.append({"task_id": r["task_id"], "title": r["title"],
                    "created": r["created"], "updated": r["updated"],
                    "cost": round(r["cost"] or 0, 6), "rounds": r["rounds"],
                    "status": status})
    return out


@app.get("/api/task/{task_id}")
def api_task(task_id: str):
    return _ledger().task_events(task_id)


@app.get("/api/cost")
def api_cost():
    rep = _ledger().cost_report()
    rep["monthly_warn_usd"] = _settings()["monthly_warn_usd"]
    return rep


@app.get("/api/crew-usage")
def api_crew_usage():
    """每个 agent 的任务数/成本/token,供 ORBIT 按用量缩放节点。"""
    return _ledger().agent_usage()


@app.get("/api/cost/dashboard")
def api_cost_dashboard(days: int = 14):
    """成本仪表盘:今日/本月/总计 + 按模型 + 按 agent + 近 N 天逐日。"""
    led = _ledger()
    t = time.localtime()
    day_start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    month_start = time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    full = led.cost_report()
    today = led.cost_report(since_ts=day_start)["total_usd"]
    month = led.cost_report(since_ts=month_start)["total_usd"]
    by_day = led.cost_by_day(days)
    s = _settings()
    return {"today_usd": today, "month_usd": month, "total_usd": full["total_usd"],
            "by_agent": full["by_agent"], "by_model": full["by_model"],
            "by_day": by_day, "monthly_warn_usd": s["monthly_warn_usd"],
            "monthly_hard_usd": float(s.get("monthly_hard_usd") or 0)}


class DispatchReq(BaseModel):
    agent: str
    instruction: str
    context: str = ""
    task_id: str = ""
    round: int = 0
    budget_usd: float = 0.0
    media_url: str = ""


@app.post("/api/dispatch")
async def api_dispatch(req: DispatchReq):
    """手动派单(调试/直接驱动)。CEO 审阅循环请通过 Claude Code 的 MCP 走。"""
    def run():
        return _router().dispatch(
            req.agent, req.instruction, context=req.context,
            task_id=req.task_id, round=req.round,
            budget_usd=req.budget_usd or None, media_url=req.media_url)
    try:
        return await asyncio.to_thread(run)
    except (BudgetExceeded, AllChannelsDown, InboundSensitive, AgentPaused) as e:
        return JSONResponse({"error": type(e).__name__, "detail": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": "dispatch_failed", "detail": str(e)}, status_code=500)


class CeoReq(BaseModel):
    goal: str


@app.post("/api/ceo")
async def api_ceo(req: CeoReq):
    """Web 端 CEO 自动编排:输入目标 → 规划 → 派单 → 汇总(后台线程跑,看板直播)。"""
    from . import providers as provcat
    from .ceo import orchestrate
    s = _settings()
    model = (s.get("ceo_model") or "").strip()
    pid = (s.get("ceo_provider") or "").strip()
    if not model or not pid:
        return JSONResponse({"error": "ceo_not_configured",
                             "detail": "请在 CONFIG → 总指挥 设置 CEO 模型与供应商"}, status_code=409)
    prov = provcat.get_provider(ROOT, pid)
    if not prov:
        return JSONResponse({"error": "unknown_provider"}, status_code=404)
    if not _env_has(prov["key_env"]):
        return JSONResponse({"error": "ceo_no_key",
                             "detail": f"CEO 供应商 {prov['name']} 还没配 key"}, status_code=409)
    tid = _ledger().new_task(req.goal[:80], created_by="user")

    def run():
        orchestrate(_router(), model, prov["endpoint"], prov["key_env"],
                    req.goal, task_id=tid, root=str(ROOT), settings=s)
    asyncio.get_running_loop().run_in_executor(None, run)
    return {"ok": True, "task_id": tid, "mode": "ceo_orchestrate"}


@app.get("/deliverables/{task_id}/{fname}")
def serve_deliverable(task_id: str, fname: str):
    """把 CEO 编排产出的交付文件(如马里奥游戏 .html)直接发给浏览器,点链接即玩。"""
    safe_task = re.sub(r"[^A-Za-z0-9_.-]", "", task_id)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", fname)
    if not safe_task or not safe_name:
        return JSONResponse({"error": "bad_path"}, status_code=400)
    base = (ROOT / "deliverables" / safe_task).resolve()
    p = (base / safe_name).resolve()
    # 防目录穿越:必须落在 deliverables/<task_id>/ 之内
    if base not in p.parents or not p.is_file():
        return JSONResponse({"error": "not_found"}, status_code=404)
    # 模型产出的 HTML 与看板同源,直接渲染会被它的脚本读到看板 token/调用看板 API(存储型 XSS)。
    # 用 CSP sandbox 把它丢进独立的不透明源:游戏照常跑(allow-scripts),但拿不到看板 cookie/DOM。
    headers = {
        "Content-Security-Policy": "sandbox allow-scripts allow-pointer-lock allow-modals allow-popups",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(str(p), headers=headers)


class BudgetReq(BaseModel):
    budget_usd: float


@app.post("/api/task/{task_id}/budget")
def api_task_budget(task_id: str, req: BudgetReq):
    """提额续跑:写 budget_override 事件,熔断任务的下一次派单按新额度放行。"""
    if req.budget_usd <= 0:
        return JSONResponse({"error": "budget_must_be_positive"}, status_code=422)
    _ledger().log(task_id, "budget_override", "user", payload={
        "budget_usd": req.budget_usd,
        "summary": f"预算提额至 ${req.budget_usd}(熔断解除,重派即续跑)"})
    return {"ok": True, "task_id": task_id, "budget_usd": req.budget_usd}


@app.post("/api/task/{task_id}/cancel")
def api_task_cancel(task_id: str):
    _ledger().log(task_id, "task_failed", "user", payload={
        "summary": "用户取消任务"})
    return {"ok": True, "task_id": task_id}


@app.get("/api/evals")
def api_evals():
    """评估集自动生长:每个任务都是评估样本,聚合各 agent×模型的胜任度。"""
    return _ledger().eval_report()


@app.get("/api/jobs")
def api_jobs():
    from .cron import load_jobs
    return load_jobs(ROOT)


# ---------- Paperclip 适配器:CrewOS 乘组作为 Paperclip 员工 ----------

@app.get("/paperclip/manifest")
def api_paperclip_manifest():
    """Paperclip 配置 http adapter 时可读的发现信息:可派的乘组成员清单。"""
    r = _router()
    return {
        "adapter": "crewos_http", "version": "1",
        "execute_url": "/paperclip/execute",
        "crew": [{"name": n, "status": r.agent_status(n)} for n in r.list_agents()],
        "note": "在 Paperclip 里把 agent 的 adapterType 设为 http,指向 /paperclip/execute;"
                "adapterConfig.crew_agent 选派给哪个乘组成员。",
    }


@app.post("/paperclip/execute")
async def api_paperclip_execute(payload: dict):
    """Paperclip 每次唤醒这名"员工"就 POST 一份 run-context;CrewOS 派单并按
    AdapterExecutionResult 形状返回。受 dashboard_token 保护(中间件已统一处理)。"""
    from .paperclip import execute
    def run():
        return execute(_router(), payload)
    return await asyncio.to_thread(run)


@app.get("/api/approvals")
def api_approvals():
    return _risk().pending()


class ApprovalDecision(BaseModel):
    approve: bool


@app.post("/api/approval/{approval_id}")
def api_approval_decide(approval_id: str, body: ApprovalDecision):
    res = _risk().decide(approval_id, body.approve, by="user")
    if res.get("error"):
        return JSONResponse(res, status_code=409)
    return res


# ---------- 供应商目录 + 一键粘 key(Hermes/OpenClaw 式) ----------

def _env_has(key_env: str) -> bool:
    """key 是否已配:先看进程环境,再看 .env 文件(支持运行中手改)。"""
    if not key_env:
        return False
    if os.environ.get(key_env):
        return True
    f = ROOT / ".env"
    if f.exists():
        import re as _re
        for line in f.read_text(encoding="utf-8").splitlines():
            m = _re.match(r'(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*["\']?([^"\']*)', line.strip())
            if m and m.group(1) == key_env and m.group(2):
                return True
    return False


@app.get("/api/providers")
def api_providers():
    from . import providers as provcat
    out = []
    for p in provcat.load_providers(ROOT):
        out.append({**{k: p.get(k) for k in
                       ("id", "name", "endpoint", "key_env", "signup", "note", "group")},
                    "custom": bool(p.get("custom")),
                    "key_set": _env_has(p.get("key_env", ""))})
    return out


_models_cache: dict = {}


@app.get("/api/provider-models/{pid}")
def api_provider_models(pid: str):
    """列出供应商的可用模型,供前端下拉选择。配了 key 用实时 /models 真实列表;
    取不到(没 key / 出错 / 空)就回退内置常见型号 KNOWN_MODELS,保证下拉永不为空。缓存 5 分钟。"""
    from . import providers as provcat
    from .router import list_models
    prov = provcat.get_provider(ROOT, pid)
    if not prov:
        return JSONResponse({"error": "unknown_provider"}, status_code=404)
    curated = provcat.known_models(pid)
    key = os.environ.get(prov["key_env"], "")
    local = prov["endpoint"].startswith(("http://localhost", "http://127."))
    if not key and not local:
        # 没 key:给内置常见型号兜底,用户依然能在下拉里挑(配 key 后会换成实时列表)
        note = "内置常见型号 · 配 key 后拉取该供应商实时列表" if curated else "需先配 key 才能拉取模型列表"
        return {"models": curated, "source": "known", "note": note}
    cached = _models_cache.get(pid)
    if cached and time.time() - cached[0] < 300:
        live = cached[1]
        return {"models": live or curated, "source": "live" if live else "known"}
    try:
        models = list_models(prov["endpoint"], key)
        _models_cache[pid] = (time.time(), models)
        return {"models": models or curated, "source": "live" if models else "known"}
    except Exception as e:
        # 实时拉取失败也别让下拉空着 —— 回退内置清单
        return {"models": curated, "source": "known", "error": str(e)[:160]}


@app.get("/api/recommendations")
def api_recommendations():
    """每个 agent 的推荐模型 + 理由 + 可用供应商。"""
    from .providers import RECOMMENDATIONS
    return RECOMMENDATIONS


class ProviderKeyReq(BaseModel):
    key_env: str
    api_key: str


@app.post("/api/provider-key")
def api_provider_key(req: ProviderKeyReq):
    """粘贴 API key:写入 ~/.crewos/.env(权限 600)并即时生效(免重启)。
    key 只活在环境变量,永不进 agent 上下文。"""
    from .workspace import write_env
    ke = req.key_env.strip()
    # 只接受规范的环境变量名,且不得覆盖系统/运行时关键变量(防止经此端点改 PATH 等)
    _DANGER = {"PATH", "HOME", "SHELL", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
               "PYTHONHOME", "IFS", "BASH_ENV"}
    if (not re.fullmatch(r"[A-Z][A-Z0-9_]*", ke) or ke in _DANGER
            or ke.startswith(("LD_", "DYLD_"))):
        return JSONResponse({"error": "bad_key_env",
                             "detail": "key_env 须为大写字母/数字/下划线,且不能是系统变量"}, status_code=422)
    write_env(ROOT, {ke: req.api_key.strip()})
    if req.api_key.strip():
        os.environ[ke] = req.api_key.strip()   # 即时生效,本次会话立刻可派单
    return {"ok": True, "key_env": ke, "key_set": bool(req.api_key.strip())}


class CustomProviderReq(BaseModel):
    name: str
    endpoint: str
    key_env: str = ""
    api_key: str = ""
    note: str = ""


@app.post("/api/providers")
def api_add_provider(req: CustomProviderReq):
    from . import providers as provcat
    from .workspace import write_env
    try:
        rec = provcat.add_custom_provider(ROOT, req.name, req.endpoint,
                                          key_env=req.key_env, note=req.note)
    except ValueError as e:
        return JSONResponse({"error": "invalid_provider", "detail": str(e)}, status_code=422)
    if req.api_key.strip():
        write_env(ROOT, {rec["key_env"]: req.api_key.strip()})
        os.environ[rec["key_env"]] = req.api_key.strip()
    return {"ok": True, "provider": rec, "key_set": bool(req.api_key.strip())}


@app.get("/api/settings")
def api_get_settings():
    return _settings()


@app.put("/api/settings")
def api_put_settings(body: dict):
    f = ROOT / "config" / "settings.yaml"
    f.parent.mkdir(exist_ok=True)
    cur = _settings()
    cur.update({k: v for k, v in body.items()
                if k in ("default_task_budget_usd", "monthly_warn_usd",
                         "feishu_webhook", "webhook_url", "dashboard_token",
                         "monthly_hard_usd", "watchdog_suspicious_minutes",
                         "watchdog_critical_minutes", "ceo_model", "ceo_provider",
                         "cny_rate",
                         "u_memory_enabled", "u_hot_dir", "u_wiki_dir")})
    # 注意:u_gbrain_bin / u_gbrain_path(被执行的二进制)故意不在 API 可写白名单内 ——
    # 只能改 settings.yaml(已等于有文件系统权限),避免经联网端点注入任意可执行文件。
    f.write_text(yaml.safe_dump(cur, allow_unicode=True), encoding="utf-8")
    return cur


@app.get("/api/u-memory")
def api_u_memory():
    """U 第二大脑连接状态(看板用):HOT 库 / WARM wiki / gbrain 是否就绪。不回传任何记忆内容。"""
    from . import umemory
    return umemory.status(_settings())


@app.get("/api/file")
def api_get_file(path: str):
    if not EDITABLE.match(path):
        return JSONResponse({"error": "path_not_allowed"}, status_code=403)
    f = (ROOT / path).resolve()
    if not str(f).startswith(str(ROOT.resolve())) or not f.exists():
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {"path": path, "content": f.read_text(encoding="utf-8")}


class ChannelCfg(BaseModel):
    name: str
    endpoint: str
    key_env: str = ""


class AgentCfg(BaseModel):
    model: str
    fallback_model: str = ""
    temperature: float = 0.7
    price_in: float = 1.0
    price_out: float = 2.0
    channels: list[ChannelCfg]


@app.put("/api/agent/{name}")
def api_put_agent(name: str, cfg: AgentCfg):
    """换脑不换人:改 LLM 绑定,角色记忆/技能/错题本全部留任。"""
    if name not in _router().list_agents():
        return JSONResponse({"error": "unknown_agent"}, status_code=404)
    if not cfg.channels:
        return JSONResponse({"error": "need_at_least_one_channel"}, status_code=422)
    for c in cfg.channels:
        ke = (c.key_env or "")
        if ke and (not re.fullmatch(r"[A-Z][A-Z0-9_]*", ke) or "sk-" in ke or len(ke) > 40):
            return JSONResponse({"error": "bad_key_env",
                "detail": f"通道 {c.name} 的 key_env 像是粘进了 API key。这里只填环境变量名"
                          f"(如 DEEPSEEK_KEY);API key 去『供应商』面板配置"}, status_code=422)
    doc = {
        "model": cfg.model,
        "channels": [c.model_dump() for c in cfg.channels],
        "fallback_model": cfg.fallback_model,
        "pricing": {"input_per_m": cfg.price_in, "output_per_m": cfg.price_out},
        "temperature": cfg.temperature,
    }
    f = ROOT / "agents" / name / "provider.yaml"
    f.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _ledger().log("system", "status_update", "user", name, payload={
        "summary": f"LLM 绑定变更 → {cfg.model}(档案/记忆全部留任)"})
    return {"ok": True, "agent": name, "model": cfg.model}


class NewAgentReq(BaseModel):
    name: str
    role: str = ""
    model: str = ""
    provider_id: str = ""


@app.post("/api/agent")
def api_new_agent(req: NewAgentReq):
    """新增乘组成员:建 role.md / provider.yaml / actions.yaml / memory。"""
    from . import providers as provcat
    name = req.name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,23}", name):
        return JSONResponse({"error": "bad_name",
            "detail": "名称用小写字母/数字/下划线,2-24 字符,字母开头"}, status_code=422)
    if name in ("system", "ceo", "user", "router", "cron", "watchdog"):
        return JSONResponse({"error": "reserved_name", "detail": f"{name} 是保留名"}, status_code=422)
    adir = ROOT / "agents" / name
    if adir.exists():
        return JSONResponse({"error": "exists", "detail": f"{name} 已存在"}, status_code=409)
    (adir / "memory").mkdir(parents=True)
    (adir / "role.md").write_text(
        f"# {name}\n\n你是 {req.role or name}。专注本职,产出明确、可验收。", encoding="utf-8")
    (adir / "memory" / "lessons.md").write_text(
        "# 错题本(失败 → 修正记录)\n\n(暂无记录)\n", encoding="utf-8")
    (adir / "actions.yaml").write_text("actions:\n  do_work: {risk: 0}\n", encoding="utf-8")
    prov = provcat.get_provider(ROOT, req.provider_id) if req.provider_id else None
    if prov:
        channels = [{"name": prov["id"], "endpoint": prov["endpoint"], "key_env": prov["key_env"]}]
    else:
        channels = [{"name": "mock", "endpoint": "mock://", "key_env": ""}]
    (adir / "provider.yaml").write_text(yaml.safe_dump({
        "model": req.model or "mock-model", "channels": channels,
        "fallback_model": "", "pricing": {"input_per_m": 1.0, "output_per_m": 2.0},
        "temperature": 0.7}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _ledger().log("system", "status_update", "user", name, payload={
        "summary": f"新增乘组成员 {name}"})
    return {"ok": True, "agent": name}


@app.delete("/api/agent/{name}")
def api_delete_agent(name: str):
    """删除乘组成员(连同其档案/记忆/错题本)。"""
    import shutil
    adir = ROOT / "agents" / name
    if not adir.is_dir() or not (adir / "provider.yaml").exists():
        return JSONResponse({"error": "unknown_agent"}, status_code=404)
    shutil.rmtree(adir)
    _ledger().log("system", "status_update", "user", name, payload={
        "summary": f"删除乘组成员 {name}"})
    return {"ok": True, "deleted": name}


class BindReq(BaseModel):
    model: str
    provider_id: str
    fallback_model: str | None = None   # None=保留原值;"" 表示清空
    temperature: float | None = None    # None=保留原值
    add_failover: bool = True   # 非 openrouter 时自动追加 openrouter 备用通道


@app.put("/api/agent/{name}/bind")
def api_bind_agent(name: str, req: BindReq):
    """换脑不换人 · 简化版:选模型 + 选供应商,自动派生通道。供 UI「用推荐」一键绑定。
    保留该 agent 现有的定价与温度,只换模型与通道。"""
    from . import providers as provcat
    if name not in _router().list_agents():
        return JSONResponse({"error": "unknown_agent"}, status_code=404)
    prov = provcat.get_provider(ROOT, req.provider_id)
    if not prov:
        return JSONResponse({"error": "unknown_provider"}, status_code=404)
    f = ROOT / "agents" / name / "provider.yaml"
    cur = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else {}
    channels = [{"name": prov["id"], "endpoint": prov["endpoint"], "key_env": prov["key_env"]}]
    # 非 openrouter 且 openrouter 已配 key → 自动追加为 failover 备用通道
    if req.add_failover and prov["id"] != "openrouter" and _env_has("OPENROUTER_KEY"):
        channels.append({"name": "openrouter", "endpoint": "https://openrouter.ai/api/v1",
                         "key_env": "OPENROUTER_KEY"})
    doc = {
        "model": req.model,
        "channels": channels,
        "fallback_model": (req.fallback_model if req.fallback_model is not None
                           else (cur or {}).get("fallback_model", "")),
        "pricing": (cur or {}).get("pricing", {"input_per_m": 1.0, "output_per_m": 2.0}),
        "temperature": (req.temperature if req.temperature is not None
                        else (cur or {}).get("temperature", 0.7)),
    }
    f.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _ledger().log("system", "status_update", "user", name, payload={
        "summary": f"绑定 → {req.model} @ {prov['name']}(记忆/错题本留任)"})
    return {"ok": True, "agent": name, "model": req.model, "provider": prov["id"],
            "key_set": _env_has(prov["key_env"])}


class FileReq(BaseModel):
    path: str
    content: str


def validate_config(path: str, content: str) -> str | None:
    """配置文件保存前校验,返回错误信息;None = 通过。写坏 yaml 会让 agent 直接瘫痪。"""
    if not path.endswith((".yaml", ".yml")):
        return None
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return f"YAML 语法错误: {str(e)[:160]}"
    if path.endswith("provider.yaml"):
        if not isinstance(doc, dict) or not doc.get("model"):
            return "provider.yaml 需要 model 字段"
        chs = doc.get("channels")
        if not isinstance(chs, list) or not chs:
            return "需要至少一条 channel"
        for c in chs:
            if not isinstance(c, dict) or not c.get("name") or not c.get("endpoint"):
                return "每条 channel 需要 name 与 endpoint"
            ke = str(c.get("key_env", ""))
            # 防呆:key_env 是环境变量名(如 DEEPSEEK_KEY),不是 API key 本身
            if ke and (not re.fullmatch(r"[A-Z][A-Z0-9_]*", ke) or "sk-" in ke or len(ke) > 40):
                return (f"通道 {c.get('name')} 的 key_env 看起来像粘进了 API key。"
                        f"这里只填环境变量名(如 DEEPSEEK_KEY);API key 请去『供应商』面板配置")
        pricing = doc.get("pricing")
        if (not isinstance(pricing, dict) or "input_per_m" not in pricing
                or "output_per_m" not in pricing):
            return "需要 pricing.input_per_m / output_per_m"
    elif path.endswith("actions.yaml"):
        actions = (doc or {}).get("actions")
        if not isinstance(actions, dict):
            return "需要 actions: 映射"
        for k, v in actions.items():
            if (not isinstance(v, dict) or not isinstance(v.get("risk"), int)
                    or not 0 <= v["risk"] <= 4):
                return f"动作 {k} 的 risk 必须是 0-4 的整数"
    elif path.endswith("crontab.yaml"):
        jobs = (doc or {}).get("jobs")
        if jobs is None:
            return None
        if not isinstance(jobs, list):
            return "jobs 必须是列表"
        for j in jobs:
            if (not isinstance(j, dict) or not j.get("name")
                    or not j.get("agent") or not j.get("instruction")):
                return "每个 job 需要 name / agent / instruction"
            try:
                due(str(j.get("schedule", "")), time.localtime())
            except ValueError as e:
                return f"job {j.get('name')}: {e}"
    return None


@app.put("/api/file")
def api_put_file(req: FileReq):
    if not EDITABLE.match(req.path):
        return JSONResponse({"error": "path_not_allowed"}, status_code=403)
    f = (ROOT / req.path).resolve()
    if not str(f).startswith(str(ROOT.resolve())):
        return JSONResponse({"error": "path_not_allowed"}, status_code=403)
    err = validate_config(req.path, req.content)
    if err:
        return JSONResponse({"error": "invalid_config", "detail": err}, status_code=422)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(req.content, encoding="utf-8")
    return {"ok": True, "path": req.path}


@app.get("/")
def index():
    from .workspace import web_root
    return FileResponse(web_root() / "index.html")


@app.on_event("startup")
async def _startup():
    global _loop
    _loop = asyncio.get_running_loop()
    threading.Thread(target=_poll_events, daemon=True).start()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    threading.Thread(target=_monthly_watch_loop, daemon=True).start()
    CronScheduler(ROOT, _router).start()


def run(root: Path, port: int = 8466):
    """被 crewos start 调用。安全铁律:只绑 127.0.0.1。"""
    global ROOT
    from .workspace import load_env
    ROOT = Path(root).resolve()
    load_env(ROOT)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main():
    from .workspace import DEFAULT_WS
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=str(DEFAULT_WS))
    p.add_argument("--port", type=int, default=8466)
    args = p.parse_args()
    run(Path(args.root).expanduser(), args.port)


if __name__ == "__main__":
    main()

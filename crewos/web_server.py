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
import re
import threading
import time
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .cron import CronScheduler
from .ledger import Ledger
from .notify import push as notify_push
from .risk import RiskEngine
from .router import AllChannelsDown, BudgetExceeded, Router, load_agent

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
            "feishu_webhook": "", "webhook_url": ""}
    if f.exists():
        base.update(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    return base


def _blocklist() -> list[str]:
    f = ROOT / "config" / "dlp_blocklist.txt"
    if not f.exists():
        return []
    return [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def _ledger() -> Ledger:
    return Ledger(ROOT / "data" / "ledger.db")


def _router() -> Router:
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
        except Exception:
            pass


async def _safe_send(ws: WebSocket, msg: str):
    try:
        await ws.send_text(msg)
    except Exception:
        _clients.discard(ws)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _clients.discard(ws)


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
        })
    return out


@app.get("/api/heartbeat/{agent}")
def api_heartbeat(agent: str):
    return _router().heartbeat(agent)


@app.get("/api/tasks")
def api_tasks(limit: int = 50):
    led = _ledger()
    rows = led._conn.execute("""
        SELECT task_id,
               MIN(ts) created,
               MAX(ts) updated,
               SUM(cost_usd) cost,
               MAX(round) rounds,
               (SELECT json_extract(payload,'$.title') FROM events e2
                 WHERE e2.task_id=e.task_id AND e2.type='task_created') title,
               (SELECT GROUP_CONCAT(type) FROM events e3 WHERE e3.task_id=e.task_id) types
        FROM events e GROUP BY task_id ORDER BY updated DESC LIMIT ?""", (limit,)
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
    except (BudgetExceeded, AllChannelsDown) as e:
        return JSONResponse({"error": type(e).__name__, "detail": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": "dispatch_failed", "detail": str(e)}, status_code=500)


@app.get("/api/evals")
def api_evals():
    """评估集自动生长:每个任务都是评估样本,聚合各 agent×模型的胜任度。"""
    return _ledger().eval_report()


@app.get("/api/jobs")
def api_jobs():
    from .cron import load_jobs
    return load_jobs(ROOT)


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
                         "feishu_webhook", "webhook_url")})
    f.write_text(yaml.safe_dump(cur, allow_unicode=True), encoding="utf-8")
    return cur


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


class FileReq(BaseModel):
    path: str
    content: str


@app.put("/api/file")
def api_put_file(req: FileReq):
    if not EDITABLE.match(req.path):
        return JSONResponse({"error": "path_not_allowed"}, status_code=403)
    f = (ROOT / req.path).resolve()
    if not str(f).startswith(str(ROOT.resolve())):
        return JSONResponse({"error": "path_not_allowed"}, status_code=403)
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

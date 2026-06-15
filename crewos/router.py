"""model-router — CrewOS 最核心的自研组件。

职责:
1. 加载 agent 档案(role.md + provider.yaml + memory/)
2. 调用该 agent 绑定的唯一 LLM(OpenAI 兼容接口)
3. 同模型多通道 failover(宁不换模型;换模型走交接流程,需用户授权)
4. 任务级预算熔断
5. 出站 DLP 扫描
6. 全程写入 Task Ledger

设计铁律:API key 永远不进任何 agent 的上下文,只在本层使用。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .checks import run_checks
from .dlp import scan as dlp_scan
from .ledger import Ledger
from .memory import select_lessons


@dataclass
class Channel:
    name: str
    endpoint: str          # OpenAI 兼容 base url,或 "mock://" 测试通道
    key_env: str = ""      # 环境变量名,真实 key 不写进任何配置文件

    @property
    def api_key(self) -> str:
        return os.environ.get(self.key_env, "") if self.key_env else ""


@dataclass
class AgentProfile:
    name: str
    model: str
    channels: list[Channel]
    price_in_per_m: float
    price_out_per_m: float
    role_prompt: str
    memory_files: list[tuple[str, str]]   # (文件名, 内容)
    fallback_model: str = ""
    temperature: float = 0.7
    workspace: Path = field(default_factory=lambda: Path("."))

    def memory_digest(self, query: str = "") -> str:
        """角色记忆注入文本。错题本按当前任务相似度选 top-3,其余文件截断拼接。"""
        parts = []
        for stem, text in self.memory_files:
            if stem == "lessons" and query:
                parts.append(f"### {stem}\n{select_lessons(text, query)}")
            else:
                parts.append(f"### {stem}\n{text.strip()[:2000]}")
        return "\n\n".join(parts)


class BudgetExceeded(Exception):
    pass


class AllChannelsDown(Exception):
    """同模型全部通道不可用 → 任务挂起,等待或走交接式换模型(需用户授权)。"""


class InboundSensitive(Exception):
    """指令/上下文含敏感信息,拒绝发往第三方模型。清理后重派。"""


class AgentPaused(Exception):
    """目标 agent 已被暂停(预算硬刹车或人工暂停),拒绝派单直到恢复。"""


class EvalGateBlocked(Exception):
    """模型最近 canary 未过 eval 闸门 → 拒绝把退化模型推上生产,直到重测通过或人工 override。"""


def load_agent(agents_dir: str | Path, name: str) -> AgentProfile:
    ws = Path(agents_dir) / name
    cfg = yaml.safe_load((ws / "provider.yaml").read_text(encoding="utf-8"))
    role = (ws / "role.md").read_text(encoding="utf-8")

    mem_dir = ws / "memory"
    memory_files = []
    if mem_dir.exists():
        for f in sorted(mem_dir.glob("*.md")):
            memory_files.append((f.stem, f.read_text(encoding="utf-8")))
    return AgentProfile(
        name=name,
        model=cfg["model"],
        channels=[Channel(**c) for c in cfg["channels"]],
        price_in_per_m=float(cfg["pricing"]["input_per_m"]),
        price_out_per_m=float(cfg["pricing"]["output_per_m"]),
        role_prompt=role,
        memory_files=memory_files,
        fallback_model=cfg.get("fallback_model", ""),
        temperature=float(cfg.get("temperature", 0.7)),
        workspace=ws,
    )


def _http_error_detail(e: "urllib.error.HTTPError") -> str:
    """把 HTTPError 解成人话:状态码 + API 返回的 message。
    401/403=key 无效或没配,404/400 常见=模型 ID 不对,429=限流。"""
    body = ""
    try:
        raw = e.read(8192).decode("utf-8", "ignore")   # 只读前 8KB,错误体可能很大
        try:
            j = json.loads(raw)
            body = (j.get("error", {}).get("message") if isinstance(j.get("error"), dict)
                    else j.get("error") or j.get("message") or raw)
        except (ValueError, AttributeError):
            body = raw
    except Exception:
        pass
    hint = {401: "(key 无效或未配置)", 403: "(无权限/key 问题)",
            404: "(模型 ID 可能不对)", 400: "(请求被拒,常见是模型 ID 不对)",
            429: "(限流,稍后再试)"}.get(e.code, "")
    # 错误体可能把我们发去的 Authorization/key 原样回显,落台账前先抹掉 Bearer/sk- 串
    safe = re.sub(r"(?i)(bearer\s+|authorization[:=]\s*|sk-)[A-Za-z0-9_\-]+", r"\1***", str(body))
    return f"HTTP {e.code}{hint} {safe[:160]}".strip()


def list_models(endpoint: str, key: str, timeout: int = 10) -> list[str]:
    """拉取一个 OpenAI 兼容供应商的可用模型列表(GET /models)。失败返回 []。"""
    if endpoint.startswith("mock://"):
        return []
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    items = data.get("data") if isinstance(data, dict) else data
    ids = [m.get("id") for m in (items or []) if isinstance(m, dict) and m.get("id")]
    return sorted(set(ids))


def _media_part(url: str) -> dict:
    """媒体 URL → OpenAI 兼容 content part(火山方舟 doubao 视频理解同此格式)。"""
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
        return {"type": "image_url", "image_url": {"url": url}}
    return {"type": "video_url", "video_url": {"url": url}}


def _content_text(content) -> str:
    """消息 content(str 或 multipart list)→ 纯文本,供 mock/计数用。"""
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", p.get("type", "")) for p in content)


def _call_openai_compatible(channel: Channel, model: str, messages: list[dict],
                            temperature: float, max_tokens: int, timeout: int = 120) -> dict:
    """单通道调用。mock:// 通道用于无网络测试。"""
    if channel.endpoint.startswith("mock://"):
        last_user = next((_content_text(m["content"]) for m in reversed(messages)
                          if m["role"] == "user"), "")
        content = f"[mock:{model}@{channel.name}] 已处理任务: {last_user[:120]}"
        return {
            "content": content,
            "finish_reason": "stop",
            "tokens_in": sum(len(_content_text(m["content"])) for m in messages) // 4,
            "tokens_out": len(content) // 4,
        }

    req = urllib.request.Request(
        channel.endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {channel.api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    usage = data.get("usage", {})
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""        # 有的供应商返回 null
    # 推理模型偶尔把 max_tokens 烧在思维链上、正文留空;此时退而取 reasoning_content,至少不丢
    if not content.strip():
        content = (msg.get("reasoning_content") or "").strip()
    return {
        "content": content,
        "finish_reason": choice.get("finish_reason") or "",
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }


HEARTBEAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats (
    agent   TEXT NOT NULL,
    channel TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (agent, channel)
);
"""


class Router:
    def __init__(self, agents_dir: str | Path, ledger: Ledger,
                 default_task_budget_usd: float = 2.0,
                 dlp_blocklist: list[str] | None = None):
        self.agents_dir = Path(agents_dir)
        self.ledger = ledger
        self.default_task_budget_usd = default_task_budget_usd
        self.dlp_blocklist = dlp_blocklist or []
        ledger._conn.executescript(HEARTBEAT_SCHEMA)
        ledger._conn.commit()

    def list_agents(self) -> list[str]:
        return sorted(
            d.name for d in self.agents_dir.iterdir()
            if d.is_dir() and (d / "provider.yaml").exists()
        )

    def heartbeat(self, name: str, max_age: float = 0.0) -> dict[str, bool]:
        """探测各通道可用性(mock 通道恒为 True)。
        max_age>0 时优先用缓存(后台探测线程每 30s 刷新),避免每次名册都真发请求烧钱。
        状态翻转(上线/掉线)写入台账。"""
        agent = load_agent(self.agents_dir, name)
        conn = self.ledger._conn
        if max_age > 0:
            rows = {r["channel"]: bool(r["ok"]) for r in conn.execute(
                "SELECT channel, ok FROM heartbeats WHERE agent=? AND ts>=?",
                (name, time.time() - max_age))}
            if all(ch.name in rows for ch in agent.channels):
                return {ch.name: rows[ch.name] for ch in agent.channels}

        prev = {r["channel"]: bool(r["ok"]) for r in conn.execute(
            "SELECT channel, ok FROM heartbeats WHERE agent=?", (name,))}
        status = {}
        for ch in agent.channels:
            if ch.endpoint.startswith("mock://"):
                status[ch.name] = True
                continue
            try:
                _call_openai_compatible(
                    ch, agent.model,
                    [{"role": "user", "content": "ping"}], 0.0, 1, timeout=10)
                status[ch.name] = True
            except Exception:
                status[ch.name] = False
        now = time.time()
        for ch_name, ok in status.items():
            conn.execute(
                "INSERT INTO heartbeats VALUES (?,?,?,?) "
                "ON CONFLICT(agent,channel) DO UPDATE SET ok=excluded.ok, ts=excluded.ts",
                (name, ch_name, int(ok), now))
            if ch_name in prev and prev[ch_name] != ok:
                self.ledger.log("system", "status_update", name, "user", payload={
                    "summary": f"通道 {ch_name} {'恢复在线' if ok else '掉线'}",
                    "channel": ch_name, "online": ok})
        conn.commit()
        return status

    # ---------- agent 暂停/恢复(预算硬刹车 + 人工) ----------

    # ───────── eval 闸门:别把退化模型推上生产 ─────────
    def record_eval(self, model: str, compliance_rate: float,
                    threshold: float = 0.6, by: str = "user") -> bool:
        """记录一次 canary 评估结果(合规率)。低于阈值 → 该模型进闸门拦截态。返回是否通过。"""
        passed = compliance_rate >= threshold
        self.ledger.log("system", "eval_gate", by, payload={
            "model": model, "compliance": round(compliance_rate, 4),
            "threshold": threshold, "passed": passed,
            "summary": f"{model} canary 合规率 {compliance_rate:.0%} "
                       f"{'通过' if passed else '未过'}(阈值 {threshold:.0%})"})
        return passed

    def override_eval(self, model: str, by: str = "user") -> None:
        """人工放行某模型的 eval 闸门(明知退化也要用,或误判)。"""
        self.ledger.log("system", "eval_override", by, payload={
            "model": model, "summary": f"已人工放行 {model} 的 eval 闸门"})

    def eval_status(self, model: str) -> dict | None:
        """该模型最近一次 canary/override 结果;None = 没测过(放行,不无理由拦截)。"""
        rows = self.ledger._conn.execute(
            "SELECT type, payload FROM events WHERE type IN ('eval_gate','eval_override') "
            "ORDER BY ts DESC").fetchall()
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except (ValueError, TypeError):
                continue
            if p.get("model") != model:
                continue
            if r["type"] == "eval_override":
                return {"passed": True, "overridden": True}
            if "passed" not in p:        # 跳过派单时的「闸门拦截」标记事件,只认 canary 评估结果
                continue
            return {"passed": bool(p.get("passed")), "compliance": p.get("compliance"),
                    "threshold": p.get("threshold")}
        return None

    def estimate(self, name: str, chars: int = 0, max_tokens: int = 8192) -> dict:
        """派单前最坏成本预估(USD):满 max_tokens 输出 + 输入按 chars//4 估 token。
        不落库、不调模型,仅供看板「本次预计 ≤ ¥X」预检卡用。"""
        agent = load_agent(self.agents_dir, name)
        est_in_tok = max(0, int(chars)) // 4
        worst = (est_in_tok * agent.price_in_per_m
                 + int(max_tokens) * agent.price_out_per_m) / 1_000_000
        return {"model": agent.model, "worst_usd": round(worst, 6),
                "cap_usd": round(self.default_task_budget_usd, 6),
                "est_in_tok": est_in_tok, "max_out_tok": int(max_tokens)}

    def agent_status(self, name: str) -> str:
        """从台账推导 agent 状态。最近一次 agent_paused 事件的 action 决定 active/paused。"""
        row = self.ledger._conn.execute(
            "SELECT payload FROM events WHERE type='agent_paused' AND to_agent=? "
            "ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
        if not row:
            return "active"
        try:
            return "paused" if json.loads(row["payload"]).get("action") == "pause" else "active"
        except (ValueError, TypeError):
            return "active"

    def pause_agent(self, name: str, reason: str, by: str = "user") -> None:
        if self.agent_status(name) == "paused":
            return
        self.ledger.log("system", "agent_paused", by, name, payload={
            "action": "pause", "summary": f"{name} 已暂停:{reason}"})

    def resume_agent(self, name: str, by: str = "user") -> None:
        if self.agent_status(name) == "active":
            return
        self.ledger.log("system", "agent_paused", by, name, payload={
            "action": "resume", "summary": f"{name} 已恢复,可重新派单"})

    def paused_agents(self) -> list[str]:
        return [n for n in self.list_agents() if self.agent_status(n) == "paused"]

    def _budget_warned(self, task_id: str) -> bool:
        row = self.ledger._conn.execute(
            "SELECT 1 FROM events WHERE task_id=? AND type='budget_warn' LIMIT 1",
            (task_id,)).fetchone()
        return row is not None

    def task_budget_override(self, task_id: str) -> float | None:
        """最近一次提额事件的额度(看板「提额续跑」写入,append-only 友好)。"""
        row = self.ledger._conn.execute(
            "SELECT payload FROM events WHERE task_id=? AND type='budget_override' "
            "ORDER BY ts DESC LIMIT 1", (task_id,)).fetchone()
        if not row:
            return None
        try:
            return float(json.loads(row["payload"]).get("budget_usd"))
        except (ValueError, TypeError):
            return None

    def dispatch(self, name: str, instruction: str, context: str = "",
                 task_id: str = "", round: int = 0, max_tokens: int = 8192,
                 budget_usd: float | None = None, media_url: str = "",
                 checks: list[dict] | None = None) -> dict:
        """CC 派单入口。返回结果 + 成本明细;DLP 拦截时返回 redacted 文本。
        media_url:视频/图片直链,走多模态 content parts(perceiver 视频理解用)。
        checks:验收规格(见 checks.py),产出先过机器检查,报告随结果返回。"""
        agent = load_agent(self.agents_dir, name)
        task_id = task_id or self.ledger.new_task(instruction[:80], created_by="ceo")

        # agent 暂停硬刹车:预算撞线或人工暂停时拒绝派单,直到显式恢复
        if self.agent_status(name) == "paused":
            self.ledger.log(task_id, "budget_block", "router", payload={
                "reason": f"{name} 处于暂停态(预算硬刹车/人工),拒绝派单", "agent": name})
            raise AgentPaused(f"{name} 已暂停,无法派单。请先 crewos resume {name} 或在看板恢复。")

        # eval 闸门:该模型最近 canary 未过 → 拒绝把退化模型推上生产(对标 Claude Code 静默降智 6 周的教训)。
        # 没测过(None)不拦;失败可重测通过或 crewos eval-override <model> 放行。绝不静默换模型,只闸门 + 上报选项。
        ev = self.eval_status(agent.model)
        if ev and not ev.get("passed"):
            self.ledger.log(task_id, "eval_gate", "router", "user", payload={
                "model": agent.model, "agent": name, "blocked": True,
                "reason": f"{agent.model} 最近 canary 未过(合规率 {ev.get('compliance')},阈值 {ev.get('threshold')}),"
                          f"已闸门拦截,避免把退化模型推上生产。",
                "options": ["重测 canary 通过后自动放行",
                            f"override 强制放行(明知退化也要用)",
                            f"授权交接式换模型 → {agent.fallback_model or '未配置备用'}"]})
            raise EvalGateBlocked(
                f"{agent.model} 未通过 eval 闸门(canary 合规率 {ev.get('compliance')} < {ev.get('threshold')})。"
                f"重测通过或 override({name})后再派。")

        # 入站 DLP:指令/上下文/媒体 URL 含敏感信息时拒发第三方模型,清理后才能重派
        # (media_url 常带签名/token 查询参数,一并扫描)
        inbound = dlp_scan(instruction + "\n" + context + ("\n" + media_url if media_url else ""),
                           extra_blocklist=self.dlp_blocklist)
        if inbound.blocked:
            self.ledger.log(task_id, "dlp_block", "router", "user", payload={
                "reason": "派单内容含敏感信息,已拒绝发往第三方模型",
                "hits": inbound.hits, "agent": name, "direction": "inbound"})
            raise InboundSensitive(
                f"指令/上下文命中敏感项 {inbound.hits},已拒发。请移除后重派。")

        # 预算上限:显式正数参数 > 提额事件 > 默认;超限挂起,绝不静默继续烧钱
        # (budget_usd<=0 视为"未指定"而非"零上限",避免把整单卡死)
        cap = (budget_usd if (budget_usd is not None and budget_usd > 0)
               else self.task_budget_override(task_id) or self.default_task_budget_usd)
        spent = self.ledger.task_cost(task_id)
        if spent >= cap:
            self.ledger.log(task_id, "budget_block", "router", payload={
                "reason": f"已花费 ${spent:.4f} ≥ 上限 ${cap}", "agent": name})
            raise BudgetExceeded(
                f"任务 {task_id} 已花费 ${spent:.4f},达到上限 ${cap}。需人工提额后继续。")
        # 软阈值 80%:接近上限先告警一次(不阻断),给 CEO 收尾的机会
        if spent >= cap * 0.8 and not self._budget_warned(task_id):
            self.ledger.log(task_id, "budget_warn", "router", "user", payload={
                "summary": f"任务 {task_id} 已花 ${spent:.4f},达上限 ${cap} 的 80%——"
                           f"建议尽快收尾,撞线将熔断。", "agent": name})

        # 单次派单上限保护:估算本次最坏成本(满 max_tokens 输出),若会冲破上限则**拒发而非截断**。
        # (压低 max_tokens 会让推理模型把额度烧在思维链上、正文产空 —— 见马里奥事故;故选择拒绝+上报。)
        est_in_tok = (len(instruction) + len(context)) // 4
        worst = (est_in_tok * agent.price_in_per_m + max_tokens * agent.price_out_per_m) / 1_000_000
        if worst > 0 and spent + worst > cap:
            self.ledger.log(task_id, "budget_block", "router", "user", payload={
                "reason": f"剩余预算 ${cap - spent:.4f} 不足以安全完成一次 {name} 派单"
                          f"(最坏约 ${worst:.4f});已拒发,保证不超 ${cap} 上限。提额或调小 max_tokens 后重试。",
                "agent": name})
            raise BudgetExceeded(
                f"任务 {task_id} 剩余预算不足以安全跑一次 {name}(需 ~${worst:.4f},剩 ${cap - spent:.4f})。"
                f"请提额或调小 max_tokens。")

        system = agent.role_prompt
        digest = agent.memory_digest(query=instruction)
        if digest:
            system += "\n\n## 你的角色记忆(历史经验,优先遵守)\n" + digest
        user = instruction + (f"\n\n## 上下文\n{context}" if context else "")
        user_content = ([{"type": "text", "text": user}, _media_part(media_url)]
                        if media_url else user)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user_content}]

        self.ledger.log(task_id, "task_assign", "ceo", name, round=round,
                        payload={"summary": instruction[:200],
                                 **({"media_url": media_url} if media_url else {})})

        # 同模型多通道 failover
        result, used_channel, errors = None, None, []
        for ch in agent.channels:
            # 跳过没配 key 的远程通道(本地 endpoint 如 ollama 不需要 key)
            local = ch.endpoint.startswith(("mock://", "http://localhost", "http://127."))
            if ch.key_env and not local and not os.environ.get(ch.key_env):
                errors.append(f"{ch.name}: 未配置 {ch.key_env}")
                self.ledger.log(task_id, "failover", "router", name, payload={
                    "reason": f"通道 {ch.name} 跳过:未配置 key（去『供应商』面板配 {ch.key_env}）"})
                continue
            try:
                result = _call_openai_compatible(
                    ch, agent.model, messages, agent.temperature, max_tokens)
                used_channel = ch.name
                break
            except urllib.error.HTTPError as e:
                detail = _http_error_detail(e)
                errors.append(f"{ch.name}: {detail}")
                self.ledger.log(task_id, "failover", "router", name, payload={
                    "reason": f"通道 {ch.name} 失败:{detail}", "error": detail[:300]})
            except (urllib.error.URLError, TimeoutError, OSError, KeyError) as e:
                errors.append(f"{ch.name}: {e}")
                self.ledger.log(task_id, "failover", "router", name, payload={
                    "reason": f"通道 {ch.name} 连接失败,尝试下一通道", "error": str(e)[:200]})

        if result is None:
            why = " | ".join(errors) or "无可用通道"
            self.ledger.log(task_id, "escalation", "router", "user", payload={
                "reason": f"{name}({agent.model}) 全部通道不可用:{why}",
                "errors": errors,
                "options": ["按上面的具体原因修(多半是没配 key 或模型 ID 不对)",
                            "等待恢复(心跳探测中)",
                            f"授权交接式换模型 → {agent.fallback_model or '未配置备用'}"]})
            raise AllChannelsDown(
                f"{name}({agent.model}) 全部通道不可用 —— {why}")

        cost = (result["tokens_in"] * agent.price_in_per_m
                + result["tokens_out"] * agent.price_out_per_m) / 1_000_000

        # 出站 DLP:唯一绝对红线
        scan_res = dlp_scan(result["content"], extra_blocklist=self.dlp_blocklist)
        content = scan_res.redacted_text if scan_res.blocked else result["content"]
        if scan_res.blocked:
            self.ledger.log(task_id, "dlp_block", "router", "user", payload={
                "reason": "产出包含敏感信息,已脱敏并拦截原文",
                "hits": scan_res.hits, "agent": name})

        # 空产出诊断:模型烧光 max_tokens 却没正文(常见于推理模型 max_tokens 太小被思维链占满)
        finish = result.get("finish_reason", "")
        empty = not content.strip()
        empty_reason = ""
        if empty:
            hit_cap = finish == "length" or result["tokens_out"] >= max_tokens
            empty_reason = (
                f"成员未产出正文(finish={finish or '未知'},out_tokens={result['tokens_out']}/{max_tokens})。"
                + ("多半是 max_tokens 被思维链占满就截断 —— 调大 max_tokens,或给该成员换非纯推理模型。"
                   if hit_cap else "模型返回了空内容,检查模型 ID / 提示词是否合适。"))
            self.ledger.log(task_id, "escalation", name, "user", payload={
                "reason": empty_reason, "summary": "本次派单无产出 —— " + empty_reason})

        # 自动检查层:机器先验收硬指标,CEO 只看标红项
        check_report = run_checks(content, checks or [])

        self.ledger.log(task_id, "task_result", name, "ceo", round=round,
                        model=agent.model, channel=used_channel,
                        tokens_in=result["tokens_in"], tokens_out=result["tokens_out"],
                        cost_usd=cost,
                        payload={"summary": content[:200],
                                 "dlp_blocked": scan_res.blocked,
                                 "finish_reason": finish,
                                 "empty": empty, "empty_reason": empty_reason,
                                 "checks_passed": check_report["passed"],
                                 "check_failures": check_report["failures"]})

        _r6 = lambda v: int(v * 1_000_000) / 1_000_000  # 参数 round 遮蔽了内置函数
        return {
            "task_id": task_id,
            "agent": name,
            "model": agent.model,
            "channel": used_channel,
            "content": content,
            "finish_reason": finish,
            "empty": empty,
            "empty_reason": empty_reason,
            "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"],
            "cost_usd": _r6(cost),
            "task_cost_usd": _r6(self.ledger.task_cost(task_id)),
            "dlp_blocked": scan_res.blocked,
            "dlp_warnings": scan_res.warnings,
            "checks": check_report,
        }

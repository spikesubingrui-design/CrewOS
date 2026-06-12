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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .dlp import scan as dlp_scan
from .ledger import Ledger


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
    memory_digest: str
    fallback_model: str = ""
    temperature: float = 0.7
    workspace: Path = field(default=Path("."))


class BudgetExceeded(Exception):
    pass


class AllChannelsDown(Exception):
    """同模型全部通道不可用 → 任务挂起,等待或走交接式换模型(需用户授权)。"""


def load_agent(agents_dir: str | Path, name: str) -> AgentProfile:
    ws = Path(agents_dir) / name
    cfg = yaml.safe_load((ws / "provider.yaml").read_text(encoding="utf-8"))
    role = (ws / "role.md").read_text(encoding="utf-8")

    # 角色记忆 v0:拼接 memory/ 下全部 md(Phase 2 升级为相似度检索 top-3)
    mem_dir = ws / "memory"
    digest_parts = []
    if mem_dir.exists():
        for f in sorted(mem_dir.glob("*.md")):
            digest_parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8').strip()[:2000]}")
    return AgentProfile(
        name=name,
        model=cfg["model"],
        channels=[Channel(**c) for c in cfg["channels"]],
        price_in_per_m=float(cfg["pricing"]["input_per_m"]),
        price_out_per_m=float(cfg["pricing"]["output_per_m"]),
        role_prompt=role,
        memory_digest="\n\n".join(digest_parts),
        fallback_model=cfg.get("fallback_model", ""),
        temperature=float(cfg.get("temperature", 0.7)),
        workspace=ws,
    )


def _call_openai_compatible(channel: Channel, model: str, messages: list[dict],
                            temperature: float, max_tokens: int, timeout: int = 120) -> dict:
    """单通道调用。mock:// 通道用于无网络测试。"""
    if channel.endpoint.startswith("mock://"):
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        content = f"[mock:{model}@{channel.name}] 已处理任务: {last_user[:120]}"
        return {
            "content": content,
            "tokens_in": sum(len(m["content"]) for m in messages) // 4,
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
    return {
        "content": data["choices"][0]["message"]["content"],
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }


class Router:
    def __init__(self, agents_dir: str | Path, ledger: Ledger,
                 default_task_budget_usd: float = 2.0,
                 dlp_blocklist: list[str] | None = None):
        self.agents_dir = Path(agents_dir)
        self.ledger = ledger
        self.default_task_budget_usd = default_task_budget_usd
        self.dlp_blocklist = dlp_blocklist or []

    def list_agents(self) -> list[str]:
        return sorted(
            d.name for d in self.agents_dir.iterdir()
            if d.is_dir() and (d / "provider.yaml").exists()
        )

    def heartbeat(self, name: str) -> dict[str, bool]:
        """探测各通道可用性(mock 通道恒为 True)。"""
        agent = load_agent(self.agents_dir, name)
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
        return status

    def dispatch(self, name: str, instruction: str, context: str = "",
                 task_id: str = "", round: int = 0, max_tokens: int = 4096,
                 budget_usd: float | None = None) -> dict:
        """CC 派单入口。返回结果 + 成本明细;DLP 拦截时返回 redacted 文本。"""
        agent = load_agent(self.agents_dir, name)
        task_id = task_id or self.ledger.new_task(instruction[:80], created_by="ceo")
        cap = budget_usd if budget_usd is not None else self.default_task_budget_usd

        # 预算熔断:超限挂起,升级为人工审批,绝不静默继续烧钱
        spent = self.ledger.task_cost(task_id)
        if spent >= cap:
            self.ledger.log(task_id, "budget_block", "router", payload={
                "reason": f"已花费 ${spent:.4f} ≥ 上限 ${cap}", "agent": name})
            raise BudgetExceeded(
                f"任务 {task_id} 已花费 ${spent:.4f},达到上限 ${cap}。需人工提额后继续。")

        system = agent.role_prompt
        if agent.memory_digest:
            system += "\n\n## 你的角色记忆(历史经验,优先遵守)\n" + agent.memory_digest
        user = instruction + (f"\n\n## 上下文\n{context}" if context else "")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]

        self.ledger.log(task_id, "task_assign", "ceo", name, round=round,
                        payload={"summary": instruction[:200]})

        # 同模型多通道 failover
        result, used_channel, errors = None, None, []
        for ch in agent.channels:
            try:
                result = _call_openai_compatible(
                    ch, agent.model, messages, agent.temperature, max_tokens)
                used_channel = ch.name
                break
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError, KeyError) as e:
                errors.append(f"{ch.name}: {e}")
                self.ledger.log(task_id, "failover", "router", name, payload={
                    "reason": f"通道 {ch.name} 失败,尝试下一通道", "error": str(e)[:200]})

        if result is None:
            self.ledger.log(task_id, "escalation", "router", "user", payload={
                "reason": f"{name}({agent.model}) 全部通道不可用,任务挂起",
                "errors": errors,
                "options": ["等待恢复(心跳探测中)",
                            f"授权交接式换模型 → {agent.fallback_model or '未配置备用'}"]})
            raise AllChannelsDown(
                f"{name} 的模型 {agent.model} 全部通道不可用。"
                f"可等待恢复,或授权交接给备用模型 {agent.fallback_model or '(未配置)'}。")

        cost = (result["tokens_in"] * agent.price_in_per_m
                + result["tokens_out"] * agent.price_out_per_m) / 1_000_000

        # 出站 DLP:唯一绝对红线
        scan_res = dlp_scan(result["content"], extra_blocklist=self.dlp_blocklist)
        content = scan_res.redacted_text if scan_res.blocked else result["content"]
        if scan_res.blocked:
            self.ledger.log(task_id, "dlp_block", "router", "user", payload={
                "reason": "产出包含敏感信息,已脱敏并拦截原文",
                "hits": scan_res.hits, "agent": name})

        self.ledger.log(task_id, "task_result", name, "ceo", round=round,
                        model=agent.model, channel=used_channel,
                        tokens_in=result["tokens_in"], tokens_out=result["tokens_out"],
                        cost_usd=cost,
                        payload={"summary": content[:200],
                                 "dlp_blocked": scan_res.blocked})

        _r6 = lambda v: int(v * 1_000_000) / 1_000_000  # 参数 round 遮蔽了内置函数
        return {
            "task_id": task_id,
            "agent": name,
            "model": agent.model,
            "channel": used_channel,
            "content": content,
            "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"],
            "cost_usd": _r6(cost),
            "task_cost_usd": _r6(self.ledger.task_cost(task_id)),
            "dlp_blocked": scan_res.blocked,
            "dlp_warnings": scan_res.warnings,
        }

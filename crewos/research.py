"""选型/调研 —— CEO 派单前先用 Tavily 真实检索成熟开源/模板,优先二次开发而非从零造轮子。

借鉴用户 ~/.openclaw/workspace/scripts/tavily-news.py 的取数模式(纯 stdlib urllib,零新依赖),
只取「找现成方案」这一步:目标 → OSS/模板候选 → 注入 CEO 规划上下文。
key 来源优先级:settings.tavily_key → 环境变量 TAVILY_API_KEY → ~/.hermes/hermes-agent/.env。
任何失败(无 key / 非建造类目标 / 网络错)都安静降级返回空 findings,绝不阻断编排。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

TAVILY_URL = "https://api.tavily.com/search"
_HERMES_ENV = Path.home() / ".hermes" / "hermes-agent" / ".env"

# 触发「先调研开源」的建造类信号:目标像在做软件/产品/工具,才值得找现成方案;
# 纯写作/分析/研究/寒暄不触发(省一次检索,也避免无意义注入)。
_BUILD_HINTS = (
    "开发", "做一个", "做个", "搭建", "生成一个", "写一个", "实现一个", "小程序", "网站",
    "app", "应用", "游戏", "erp", "系统", "平台", "工具", "脚本", "插件", "网页", "仪表盘",
    "dashboard", "build", "create", "make a", "website", "game", "clone", "落地页", "官网",
    "商城", "后台", "爬虫", "bot", "机器人", "原型", "demo",
)


def looks_buildish(goal: str) -> bool:
    """目标是否像「做一个软件/产品/工具」——是才值得先调研开源方案。"""
    g = (goal or "").lower()
    return any(h in g for h in _BUILD_HINTS)


def load_key(settings: dict | None = None) -> str:
    """Tavily key:settings 优先 → 环境变量 → hermes .env 回退(与用户 tavily-news.py 同源)。"""
    key = ((settings or {}).get("tavily_key") or "").strip()
    if key:
        return key
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if key:
        return key
    if _HERMES_ENV.exists():
        for line in _HERMES_ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("TAVILY_API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def tavily_search(query: str, api_key: str, max_results: int = 5, timeout: int = 20) -> list[dict]:
    """打 Tavily /search(advanced 召回更准),返回 results 列表。失败抛异常由上层吞掉。"""
    payload = {
        "api_key": api_key, "query": query,
        "max_results": max(1, min(max_results, 10)),
        "search_depth": "advanced",
        "include_answer": False, "include_raw_content": False, "include_images": False,
    }
    req = urllib.request.Request(
        TAVILY_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("results") or []


def research_oss(goal: str, settings: dict | None = None, max_results: int = 5) -> dict:
    """为建造类目标检索成熟开源/模板。
    返回 {findings:[{title,url,snippet}], text:str, reason:str}。
    非建造类 / 无 key / 出错 / 空结果 → findings 为空(安静降级)。"""
    if not looks_buildish(goal):
        return {"findings": [], "text": "", "reason": "non_build"}
    key = load_key(settings)
    if not key:
        return {"findings": [], "text": "", "reason": "no_key"}
    query = f"{goal} 开源 github 现成 模板 方案 open source github template starter"
    try:
        results = tavily_search(query, key, max_results)
    except Exception as e:                      # 网络/HTTP/解析任何错 → 降级
        return {"findings": [], "text": "", "reason": f"error:{str(e)[:80]}"}
    findings = []
    for r in results:
        url = r.get("url") or ""
        if not url:
            continue
        findings.append({
            "title": (r.get("title") or "").strip(),
            "url": url,
            "snippet": (r.get("content") or "").strip().replace("\n", " ")[:200],
        })
    if not findings:
        return {"findings": [], "text": "", "reason": "empty"}
    lines = [
        "【调研结果 · 优先二次开发】以下是为本目标实时检索到的成熟开源项目 / 模板。"
        "若本目标是做软件 / 产品 / 工具,请优先选其中最合适的一个做二次开发:"
        "在派单 instruction 的【要求】里写明「基于 <仓库/URL> 改造」,"
        "【验收标准】要求复用其结构而非从零重写。禁止无视现成方案从零造轮子。",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. {f['title']} — {f['url']}\n   {f['snippet']}")
    return {"findings": findings, "text": "\n".join(lines), "reason": "ok"}

"""连接 U —— 主人(Spike)的统一第二大脑(永久、越用越聪明的记忆系统)。

U 是四层一体(见 ~/.openclaw/workspace/memory/U.md):
- HOT 工作记忆  `~/.openclaw/workspace/memory/`(= `~/.hermes/memories` symlink):MEMORY/USER/SOUL + 每日片段。点。
- WARM 永久知识图 `~/wiki/`(gbrain 治理,9 个 RESOLVER home):每实体一节点。线。
- COLD 面层 `~/wiki/synthesis/`:图社区发现 → 面页 + 双向回链。面。
- 升华管线:每晚 distill-nightly.py 等,把 点→线→面。

U 的边界铁律(本模块严格遵守,绝不越界):
  「库会忘记,wiki 会永记。升华单向 库→wiki;wiki 从不注入,按需 `gbrain` 读回。」

因此 CrewOS 只走两个被允许的口子:
- 读(recall):派单前用 `gbrain search` 从永久知识图按需召回与任务相关的节点,拼成紧凑上下文
  注入给乘组 —— 让团队"知道"主人沉淀的一切;读不到就返回空,绝不阻断派单。
- 写(remember):任务结案把一条"CrewOS 编码经历"蒸馏追加到 HOT 层当天 daily 文件
  (U.md 明确收录"Claude Code 编码经历(蒸馏后)"),由 U 的每晚 distill 升华进图。
  绝不直写 wiki(那是升华管线唯一的活)。

★ 隐私 / 发布:U 的真实记忆数据全部在 CrewOS 仓库之外(~/.openclaw、~/wiki),
  本文件只是"记忆系统/连接器"本身,随 CrewOS 一起开源发布;主人的记忆内容永不进 git。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# 默认指向 U 的真实位置;均可在 CONFIG(settings.yaml)里改,连接器本身不含任何私有数据。
DEFAULTS = {
    "hot_dir": "~/.openclaw/workspace/memory",   # = ~/.hermes/memories(symlink)
    "wiki_dir": "~/wiki",
    "gbrain_bin": "gbrain",
    "gbrain_path": "~/.bun/bin",   # gbrain 装在 bun 的 bin;子进程/cron 常缺这段 PATH
}


def _expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def config(settings: dict | None = None) -> dict:
    s = settings or {}
    return {
        "enabled": bool(s.get("u_memory_enabled", True)),
        "hot_dir": _expand(s.get("u_hot_dir") or DEFAULTS["hot_dir"]),
        "wiki_dir": _expand(s.get("u_wiki_dir") or DEFAULTS["wiki_dir"]),
        "gbrain_bin": (s.get("u_gbrain_bin") or DEFAULTS["gbrain_bin"]).strip(),
        "gbrain_path": s.get("u_gbrain_path") or DEFAULTS["gbrain_path"],
    }


def _env(c: dict) -> dict:
    env = dict(os.environ)
    pre = str(_expand(c["gbrain_path"]))
    env["PATH"] = f"{pre}:{env.get('PATH', '')}"
    return env


def _gbrain(args: list[str], c: dict, timeout: int = 25) -> str:
    """跑一条 gbrain 子命令,返回 stdout;任何异常都吞掉返回空串(记忆是锦上添花,绝不拖垮主流程)。"""
    try:
        cwd = str(c["wiki_dir"]) if c["wiki_dir"].is_dir() else None
        r = subprocess.run([c["gbrain_bin"], *args], env=_env(c), cwd=cwd,
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def gbrain_available(settings: dict | None = None) -> bool:
    return bool(_gbrain(["--version"], config(settings), timeout=8))


def status(settings: dict | None = None) -> dict:
    """给看板看的连接状态:HOT 库 / WARM wiki / gbrain 三项是否就绪。"""
    c = config(settings)
    memory_md = c["hot_dir"] / "MEMORY.md"
    hot_ok = memory_md.exists()
    wiki_ok = c["wiki_dir"].is_dir()
    gb_ok = gbrain_available(settings)
    return {
        "enabled": c["enabled"],
        "hot_dir": str(c["hot_dir"]), "hot_ok": hot_ok,
        "wiki_dir": str(c["wiki_dir"]), "wiki_ok": wiki_ok,
        "gbrain": c["gbrain_bin"], "gbrain_ok": gb_ok,
        # 至少 HOT 库在 + 启用 = 已连上 U(gbrain 可缺,缺了只是少了知识图召回)
        "connected": c["enabled"] and hot_ok,
    }


def recall(query: str, settings: dict | None = None, top: int = 5, max_chars: int = 1600) -> str:
    """派单前从 U 的永久知识图按需召回与任务相关的节点,拼成可注入的上下文块。
    用 `gbrain search`(关键词 tsvector,稳定可用);召回不到就返回空串(绝不注入噪音/无关 PII)。"""
    c = config(settings)
    if not c["enabled"] or not query.strip():
        return ""
    out = _gbrain(["search", query.strip()], c)
    if not out:
        return ""
    hits = [ln for ln in out.splitlines() if ln.strip().startswith("[")][:top]
    if not hits:
        return ""
    block = ("## 来自 U(主人第二大脑)的相关记忆 —— 优先参考\n"
             "(经 gbrain 从永久知识图按需召回,非常驻注入)\n" + "\n".join(hits))
    return block[:max_chars]


def remember(title: str, body: str, settings: dict | None = None, tag: str = "crewos") -> str:
    """把一条 CrewOS 编码经历蒸馏追加到 U 的 HOT 层当天 daily 文件 —— 升华单向 库→wiki 的入口。
    由 U 的每晚 distill 决定要不要升华进知识图(人在环棘轮)。返回写入路径;失败/未启用返回空串。
    绝不直写 wiki(铁律:wiki 只由升华管线写)。"""
    c = config(settings)
    if not c["enabled"] or not c["hot_dir"].is_dir():
        return ""
    day = time.strftime("%Y-%m-%d")
    stamp = time.strftime("%H:%M")
    f = c["hot_dir"] / f"{day}.md"
    entry = f"\n\n## [CrewOS·{tag}] {title.strip()} ({day} {stamp})\n{body.strip()}\n"
    try:
        existing = f.read_text(encoding="utf-8") if f.exists() else f"# {day}\n"
        f.write_text(existing.rstrip() + entry, encoding="utf-8")
        return str(f)
    except Exception:
        return ""

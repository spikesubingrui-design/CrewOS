"""角色记忆沉淀 — 错题本自动写入 + Memory Tree(跨项目永久记忆)。

错题本(agent 级):
1. CC 审阅打回(review_feedback)→ 自动记一笔"被打回的原因"
2. CC 复盘时显式调用 add_lesson MCP 工具 → 记"修正后的做法"
错题本会注入该 agent 的每次任务上下文(router.load_agent),越用越聪明。

Memory Tree(团队级,<工作区>/memory/):
Obsidian 兼容 vault,纯 Markdown。projects/ 放项目复盘,knowledge/ 放全局知识。
CC 接新任务先 vault_search 查同类经验,结案把复盘写进 projects/——
第二个项目从此吸取第一个项目的教训。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

LESSONS_HEADER = """# 错题本(失败 → 修正记录)

> 由 CC 在审阅/复盘节点写入。格式:日期 | 任务 | 教训。
> 本文件会注入到你的每次任务上下文,优先遵守。
"""


def append_lesson(agents_dir: str | Path, agent: str, lesson: str,
                  task_id: str = "", round: int = 0) -> Path:
    agent_dir = Path(agents_dir) / agent
    if not agent_dir.is_dir():
        raise FileNotFoundError(f"未知 agent: {agent}")
    f = agent_dir / "memory" / "lessons.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    text = f.read_text(encoding="utf-8") if f.exists() else LESSONS_HEADER
    text = text.replace("(暂无记录)", "").rstrip() + "\n"
    day = time.strftime("%Y-%m-%d")
    ref = f"{task_id} R{round}" if task_id else "—"
    f.write_text(f"{text}- {day} | {ref} | {lesson.strip()}\n", encoding="utf-8")
    return f


# ---------- Memory Tree(Obsidian vault) ----------

def _vault_path(root: str | Path, rel: str) -> Path:
    """vault 内路径安全解析:只许 memory/ 下的 .md,杜绝越权。"""
    vault = (Path(root) / "memory").resolve()
    if not re.fullmatch(r"[\w\-./一-鿿]+\.md", rel) or ".." in rel:
        raise ValueError(f"非法 vault 路径: {rel}(只允许 memory/ 下的 .md)")
    p = (vault / rel).resolve()
    if not str(p).startswith(str(vault)):
        raise ValueError(f"非法 vault 路径: {rel}")
    return p


def vault_write(root: str | Path, rel: str, content: str, mode: str = "append") -> Path:
    f = _vault_path(root, rel)
    f.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append" and f.exists():
        old = f.read_text(encoding="utf-8").rstrip()
        content = f"{old}\n\n{content.strip()}\n"
    f.write_text(content.strip() + "\n", encoding="utf-8")
    return f


def vault_read(root: str | Path, rel: str) -> str:
    f = _vault_path(root, rel)
    if not f.exists():
        raise FileNotFoundError(f"vault 中无此文件: {rel}")
    return f.read_text(encoding="utf-8")


def vault_search(root: str | Path, query: str, max_hits: int = 8) -> list[dict]:
    """关键词检索(空格分词,全部命中才算)。返回文件 + 命中行片段。"""
    vault = Path(root) / "memory"
    if not vault.exists():
        return []
    terms = [t.lower() for t in query.split() if t.strip()]
    hits = []
    for f in sorted(vault.rglob("*.md")):
        text = f.read_text(encoding="utf-8")
        low = text.lower()
        if not terms or not all(t in low for t in terms):
            continue
        lines = [l.strip() for l in text.splitlines()
                 if l.strip() and any(t in l.lower() for t in terms)]
        hits.append({"file": str(f.relative_to(vault)),
                     "matches": lines[:5]})
        if len(hits) >= max_hits:
            break
    return hits

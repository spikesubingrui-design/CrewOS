"""角色记忆沉淀 — 错题本自动写入。

两个入口都会调用 append_lesson:
1. CC 审阅打回(review_feedback)→ 自动记一笔"被打回的原因"
2. CC 复盘时显式调用 add_lesson MCP 工具 → 记"修正后的做法"

错题本会注入该 agent 的每次任务上下文(router.load_agent),越用越聪明。
"""
from __future__ import annotations

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

"""工作区管理 — 像 Hermes 一样,所有用户数据住在 ~/.crewos。

代码与数据彻底分离:pip 升级 crewos 不会碰你的 agent 档案/记忆/台账/key。
"""
from __future__ import annotations

import os
import re
import shutil
from importlib import resources
from pathlib import Path

DEFAULT_WS = Path(os.environ.get("CREWOS_HOME", Path.home() / ".crewos"))

KEY_SPECS = [
    ("OPENROUTER_KEY", "OpenRouter(一个 key 覆盖大部分模型,推荐必填)"),
    ("OPENAI_KEY", "OpenAI 直连(coder 备用通道)"),
    ("DASHSCOPE_KEY", "阿里百炼(writer 备用通道)"),
    ("MOONSHOT_KEY", "月之暗面(researcher 备用通道)"),
    ("DEEPSEEK_KEY", "DeepSeek 直连(analyst/runner 备用通道)"),
    ("ZHIPU_KEY", "智谱(builder 备用通道)"),
    ("ARK_KEY", "火山方舟(perceiver 视频理解,唯一通道)"),
]


def template_root() -> Path:
    return Path(str(resources.files("crewos"))) / "templates"


def web_root() -> Path:
    return Path(str(resources.files("crewos"))) / "web"


def ensure_workspace(ws: Path) -> list[str]:
    """初始化工作区(已存在的文件绝不覆盖)。返回新建条目列表。"""
    created = []
    ws.mkdir(parents=True, exist_ok=True)
    tpl = template_root()
    for rel in ("CrewOS.md",):
        if not (ws / rel).exists():
            shutil.copy(tpl / rel, ws / rel)
            created.append(rel)
    for sub in ("agents", "config"):
        src = tpl / sub
        for f in src.rglob("*"):
            if f.is_file():
                dst = ws / f.relative_to(tpl)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(f, dst)
                    created.append(str(f.relative_to(tpl)))
    (ws / "data").mkdir(exist_ok=True)
    return created


def load_env(ws: Path) -> int:
    """加载 <ws>/.env 到环境变量(不覆盖已有)。返回加载条数。"""
    f = ws / ".env"
    if not f.exists():
        return 0
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*["\']?([^"\']*)["\']?$', line)
        if m and m.group(2):
            os.environ.setdefault(m.group(1), m.group(2))
            n += 1
    return n


def write_env(ws: Path, keys: dict[str, str]):
    """写入 .env(0600 权限)。保留已有未提及的条目。"""
    f = ws / ".env"
    existing: dict[str, str] = {}
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r'(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*["\']?([^"\']*)["\']?$', line.strip())
            if m:
                existing[m.group(1)] = m.group(2)
    existing.update({k: v for k, v in keys.items() if v})
    body = "# CrewOS API keys — 此文件权限 600,绝不提交到任何仓库\n"
    body += "".join(f'{k}="{v}"\n' for k, v in existing.items())
    f.write_text(body, encoding="utf-8")
    os.chmod(f, 0o600)

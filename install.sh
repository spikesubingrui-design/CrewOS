#!/usr/bin/env bash
# CrewOS 一键安装(Hermes 式) — 在仓库目录执行: bash install.sh
set -e
echo ""
echo "   █▀▀ █▀█ █▀▀ █ █ █ █▀█ █▀▀"
echo "   █▄▄ █▀▄ ██▄ ▀▄▀▄▀ █▄█ ▄▄█   INSTALLER"
echo ""
command -v python3 >/dev/null || { echo "✗ 需要 Python 3.10+"; exit 1; }
PYV=$(python3 -c "import sys;print(sys.version_info>=(3,10))")
[ "$PYV" = "True" ] || { echo "✗ 需要 Python 3.10+,当前 $(python3 -V)"; exit 1; }

echo "▸ 安装 crewos 包及依赖…"
pip3 install . --quiet 2>/dev/null || pip3 install . --break-system-packages --quiet

command -v crewos >/dev/null || echo "⚠ crewos 不在 PATH,请把 pip 的 bin 目录加入 PATH 后重试"
echo ""
echo "✓ 安装完成。接下来:"
echo ""
echo "    crewos onboard     # 初始化工作区 (~/.crewos) + 录入 API keys"
echo "    crewos start       # 启动 Mission Control(自动打开浏览器)"
echo ""
echo "  让 Claude Code 上岗当总指挥:"
echo "    claude mcp add crewos -- crewos mcp"
echo ""

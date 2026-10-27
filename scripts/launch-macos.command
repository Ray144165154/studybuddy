#!/bin/bash
# studybuddy 启动脚本（macOS）
#
# 用法：
#   1) 在 Finder 里双击本文件，打开本地网页界面；或
#   2) 在终端里执行：bash scripts/launch-macos.command
#
# 如果双击没有反应，通常是没有可执行权限，先在终端执行一次：
#   chmod +x scripts/launch-macos.command
#
# 额外的参数会原样传给 `studybuddy ui`，例如换端口：
#   bash scripts/launch-macos.command --port 9000
#
# 首次使用请先运行 install-macos.command 完成安装。

set -euo pipefail

# 启动失败时不要闪退：打印中文提示并暂停，等用户按回车再关闭窗口。
pause_on_exit() {
    local code=$?
    if [ "$code" -ne 0 ]; then
        echo
        echo "启动失败（退出码 ${code}）。" >&2
        echo "请根据上面的提示处理后重试，或按回车键关闭窗口。" >&2
        if [ -t 0 ]; then
            read -r _ || true
        fi
    fi
    exit "$code"
}
trap pause_on_exit EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

# 没有安装过，或者虚拟环境被删掉/损坏时，明确告诉用户下一步做什么。
if [ ! -x "$VENV_PY" ]; then
    echo
    echo "还没有安装 studybuddy（找不到 .venv）。" >&2
    echo "请先运行安装脚本：" >&2
    echo "  bash \"$SCRIPT_DIR/install-macos.command\"" >&2
    echo "或者在 Finder 里双击 install-macos.command。" >&2
    exit 1
fi

# 激活虚拟环境，让 `studybuddy` 命令指向这个项目的安装。
# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate"

if ! command -v studybuddy >/dev/null 2>&1; then
    echo
    echo "虚拟环境里找不到 studybuddy 命令，安装可能没有完成。" >&2
    echo "请重新运行：" >&2
    echo "  bash \"$SCRIPT_DIR/install-macos.command\"" >&2
    exit 1
fi

echo
echo "正在启动 studybuddy 网页界面……"
echo "启动后终端会打印实际访问地址（默认 http://127.0.0.1:8765）。"
echo "按 Ctrl+C 可以退出；如果浏览器没有自动打开，手动访问上面的地址即可。"
echo

# 这里用 exec，让 `studybuddy ui` 直接接管当前进程：
# 关掉终端（或按 Ctrl+C）时服务随之退出，不会留下后台进程。
trap - EXIT
exec studybuddy ui "$@"

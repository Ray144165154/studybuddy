#!/bin/bash
# studybuddy 一键安装脚本（macOS）
#
# 用法：
#   1) 在 Finder 里双击本文件；或
#   2) 在终端里执行：bash scripts/install-macos.command
#
# 如果双击没有反应，通常是没有可执行权限，先在终端执行一次：
#   chmod +x scripts/install-macos.command scripts/launch-macos.command
#
# 可重复运行：已经存在的 .venv 会直接复用，不会重新创建。
#
# 可选参数：
#   --no-launch   安装完成后不询问、也不启动网页界面（方便脚本化调用）

set -euo pipefail

# 出错时不要闪退：打印中文提示并暂停，等用户按回车再关闭窗口。
pause_on_exit() {
    local code=$?
    if [ "$code" -ne 0 ]; then
        echo
        echo "安装没有完成（退出码 ${code}）。" >&2
        echo "请把上面的错误信息截图反馈，或按回车键关闭窗口。" >&2
        if [ -t 0 ]; then
            read -r _ || true
        fi
    fi
    exit "$code"
}
trap pause_on_exit EXIT

# 正常结束时也停一下，方便双击启动的用户看完“下一步”提示。
pause_if_interactive() {
    if [ -t 0 ]; then
        echo
        printf '按回车键关闭窗口……'
        read -r _ || true
        echo
    fi
}

# 定位脚本和项目根目录（scripts 的上一级），之后都在项目根目录里操作。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$PROJECT_ROOT"

echo
echo "studybuddy 一键安装（macOS）"
echo "项目目录：$PROJECT_ROOT"

# 没有可执行权限时给出提示：双击 .command 依赖这个权限。
if [ ! -x "${BASH_SOURCE[0]}" ]; then
    echo
    echo "提示：本脚本还没有可执行权限。如果要双击运行，请先在终端执行："
    echo "  chmod +x \"$SCRIPT_DIR/install-macos.command\" \"$SCRIPT_DIR/launch-macos.command\""
fi

if [ ! -f "$PROJECT_ROOT/pyproject.toml" ]; then
    echo
    echo "错误：在 $PROJECT_ROOT 没有找到 pyproject.toml。" >&2
    echo "请确认脚本位于项目的 scripts/ 目录里。" >&2
    exit 1
fi

# 寻找 Python 3.10+。优先用 README 里的 python3，其次尝试常见的带版本号命令。
find_python() {
    local candidate
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

echo
echo "==> 查找 Python（需要 3.10 或更高版本）"
if ! PYTHON="$(find_python)"; then
    echo
    echo "没有找到 Python 3.10 或更高版本。macOS 自带的 python3 可能太旧。" >&2
    echo "请先安装 Python（任选一种方式）：" >&2
    echo "  1) 官网安装包（推荐 3.12 或更新）：" >&2
    echo "       https://www.python.org/downloads/macos/" >&2
    echo "  2) Homebrew：" >&2
    echo "       brew install python@3.12" >&2
    echo
    echo "装好后关闭并重新打开终端，再重新运行本脚本。" >&2
    exit 1
fi
PYTHON_VERSION="$("$PYTHON" --version 2>&1)"
echo "    使用 ${PYTHON_VERSION}（${PYTHON}）"

VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

# 创建或复用虚拟环境。已存在且可用的 .venv 直接复用，保证幂等。
if [ -x "$VENV_PY" ]; then
    if ! "$VENV_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        echo
        echo "错误：现有的 .venv 使用的 Python 低于 3.10，或者虚拟环境已经损坏。" >&2
        echo "请先删除它，再重新运行本脚本：" >&2
        echo "  rm -rf \"$VENV_DIR\"" >&2
        exit 1
    fi
    echo
    echo "==> 已存在可用的虚拟环境，直接复用：$VENV_DIR"
else
    if [ -e "$VENV_DIR" ]; then
        echo
        echo "==> 发现不完整的 .venv（缺少 bin/python），将尝试重新创建"
    else
        echo
        echo "==> 创建虚拟环境：$VENV_DIR"
    fi
    "$PYTHON" -m venv "$VENV_DIR"
    if [ ! -x "$VENV_PY" ]; then
        echo "错误：创建虚拟环境后没有找到 ${VENV_PY}。" >&2
        exit 1
    fi
    echo "    虚拟环境已创建"
fi

echo
echo "==> 安装 studybuddy（pip install -e .）"
# 始终走 venv 里的 pip，避免污染系统 Python；重复运行会重新确认安装，不会报错。
"$VENV_PY" -m pip install --disable-pip-version-check --no-input -e "$PROJECT_ROOT"

# 轻量自检：确认命令行入口确实可用。
CLI_BIN="$VENV_DIR/bin/studybuddy"
if [ -x "$CLI_BIN" ]; then
    echo "    安装完成：$("$CLI_BIN" --version 2>/dev/null || echo studybuddy)"
else
    echo "    注意：没有找到 studybuddy 命令，仍可用 \"$VENV_PY -m studybuddy\" 运行。"
fi

# 与 README.md 里的安装、用法保持一致。
cat <<'EOF'

下一步（在终端里执行）：

  1. 激活虚拟环境（每次新开窗口都要做一次）：
       source .venv/bin/activate

  2. 导入教材：
       studybuddy add "高等数学.pdf" --course 高等数学

  3. 提问：
       studybuddy ask "导数的定义是什么？"

打开网页界面：
       studybuddy ui
     浏览器访问 http://127.0.0.1:8765

可选：把数据目录放到不会被清空的位置（下面只是示例，按需修改）：
       export STUDYBUDDY_ROOT="$HOME/Documents/studybuddy-data/library"

可选：配置模型（下面只是示例，本脚本不会写入任何密钥）：
       export STUDYBUDDY_API_KEY="sk-你的密钥"
       export STUDYBUDDY_BASE_URL="https://api.deepseek.com/v1"
       export STUDYBUDDY_MODEL="deepseek-chat"

EOF

# 完成后询问是否立刻启动界面。默认回车视为 y。
if [ "${1:-}" = "--no-launch" ]; then
    echo "（--no-launch）已跳过启动网页界面。"
    exit 0
fi

if [ -t 0 ]; then
    printf '现在启动网页界面吗？[Y/n] '
    answer=""
    IFS= read -r -n 1 answer || true
    echo
    case "${answer:-y}" in
        [nN])
            echo "已跳过。以后可以双击 scripts/launch-macos.command 启动。"
            pause_if_interactive
            ;;
        *)
            echo "正在启动网页界面……"
            trap - EXIT
            exec bash "$SCRIPT_DIR/launch-macos.command"
            ;;
    esac
else
    # 非交互环境（例如被其他脚本调用）不自动启动，避免阻塞调用方。
    echo "当前不是交互终端，已跳过自动启动。需要界面时请运行："
    echo "  bash \"$SCRIPT_DIR/launch-macos.command\""
fi

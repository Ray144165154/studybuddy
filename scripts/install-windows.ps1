#Requires -Version 5.1
<#
.SYNOPSIS
    一键安装 studybuddy（Windows）。

.DESCRIPTION
    自动寻找 Python 3.10+，在项目根目录创建 .venv，并用虚拟环境里的 pip
    以可编辑模式安装本项目（pip install -e .）。

    脚本可以重复运行：已经存在的 .venv 会直接复用，不会重新创建。
    不需要管理员权限，也不会下载或执行任何远端脚本。

.PARAMETER Launch
    安装完成后立刻启动本地网页界面（studybuddy ui）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1 -Launch
#>
[CmdletBinding()]
param(
    [switch]$Launch
)

# 任何未处理的错误都停下来，交给最外层的 try/catch 打印中文提示。
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    [完成] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    [注意] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host ""
    Write-Host "[错误] $Message" -ForegroundColor Red
}

# 出错时不要让窗口直接消失，等用户看完报错再退出。
function Pause-Exit {
    param([int]$Code = 1)
    Write-Host ""
    if ($Host.Name -eq "ConsoleHost") {
        try {
            [void](Read-Host "按回车键关闭窗口")
        }
        catch {
            # 没有交互终端时（例如被其他程序调用）直接退出即可。
        }
    }
    exit $Code
}

# 检查一个候选解释器是否可用、版本是否满足要求。
# 成功时返回对象：Exe（命令）、Args（前置参数）、Version、Major、Minor。
function Get-PythonInfo {
    param(
        [string]$Exe,
        [string[]]$PrefixArgs
    )

    $probeArgs = @()
    if ($PrefixArgs) {
        $probeArgs += $PrefixArgs
    }
    $probeArgs += @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")

    try {
        $output = & $Exe @probeArgs 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
    }
    catch {
        return $null
    }

    $firstLine = @($output)[0]
    if ($null -eq $firstLine) {
        return $null
    }

    $version = ([string]$firstLine).Trim()
    if ($version -notmatch '^(\d+)\.(\d+)$') {
        return $null
    }

    return [pscustomobject]@{
        Exe     = $Exe
        Args    = $PrefixArgs
        Version = $version
        Major   = [int]$Matches[1]
        Minor   = [int]$Matches[2]
    }
}

# 按优先级寻找 Python：py -3 → python → python3。
function Find-Python {
    $candidates = New-Object System.Collections.ArrayList

    if (Get-Command py -ErrorAction SilentlyContinue) {
        [void]$candidates.Add([pscustomobject]@{ Exe = "py"; Args = @("-3") })
    }
    foreach ($name in @("python", "python3")) {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            [void]$candidates.Add([pscustomobject]@{ Exe = $name; Args = @() })
        }
    }

    $tooOld = @()
    foreach ($candidate in $candidates) {
        $info = Get-PythonInfo -Exe $candidate.Exe -PrefixArgs $candidate.Args
        if ($null -eq $info) {
            continue
        }
        if ($info.Major -eq 3 -and $info.Minor -ge 10) {
            return $info
        }
        $tooOld += "$($candidate.Exe)（Python $($info.Version)）"
    }

    # 找不到可用解释器时，给出中文安装指引。
    if ($tooOld.Count -gt 0) {
        Write-Err ("找到了 Python，但版本太低：{0}。本项目需要 Python 3.10 或更高版本。" -f ($tooOld -join "、"))
    }
    else {
        Write-Err "没有找到 Python。本项目需要 Python 3.10 或更高版本。"
    }

    Write-Host @'

请先安装 Python（任选一种方式）：

  方式一（推荐，使用 winget）：
      winget install --id Python.Python.3.12 -e

  方式二（官网安装包）：
      https://www.python.org/downloads/windows/
      安装时务必勾选 "Add python.exe to PATH"。

安装完成后，关闭并重新打开 PowerShell，再运行本脚本。
如果不确定是否装好，可以先在 PowerShell 里执行：
      py -3 --version
'@

    Pause-Exit 1
}

function Install-StudyBuddy {
    # 脚本位于 scripts\ 下，项目根目录是它的上一级。
    $ScriptDir = Split-Path -Parent $PSCommandPath
    $ProjectRoot = Split-Path -Parent $ScriptDir

    if (-not (Test-Path -LiteralPath $ProjectRoot)) {
        throw "找不到项目目录：$ProjectRoot"
    }
    Set-Location -LiteralPath $ProjectRoot

    Write-Host ""
    Write-Host "studybuddy 一键安装（Windows）" -ForegroundColor Cyan
    Write-Host "项目目录：$ProjectRoot"

    $pyproject = Join-Path $ProjectRoot "pyproject.toml"
    if (-not (Test-Path -LiteralPath $pyproject)) {
        throw "在 $ProjectRoot 没有找到 pyproject.toml，请确认脚本放在项目的 scripts\ 目录里。"
    }

    Write-Step "查找 Python（需要 3.10 或更高版本）"
    $python = Find-Python
    Write-Ok "使用 Python $($python.Version)（$($python.Exe) $($python.Args -join ' ')）"

    $venvDir = Join-Path $ProjectRoot ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if (Test-Path -LiteralPath $venvPython) {
        Write-Ok "已存在虚拟环境，直接复用：$venvDir"
    }
    else {
        if (Test-Path -LiteralPath $venvDir) {
            Write-Warn "发现不完整的 .venv（缺少 Scripts\python.exe），将尝试重新创建。"
        }
        Write-Step "创建虚拟环境：$venvDir"

        $venvArgs = @()
        if ($python.Args) {
            $venvArgs += $python.Args
        }
        $venvArgs += @("-m", "venv", $venvDir)

        & $python.Exe @venvArgs
        if ($LASTEXITCODE -ne 0) {
            throw "创建虚拟环境失败（退出码 $LASTEXITCODE）。如果 .venv 已损坏，请手动删除它后重试。"
        }
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw "创建虚拟环境后没有找到 $venvPython，请检查 Python 安装是否完整。"
        }
        Write-Ok "虚拟环境已创建"
    }

    Write-Step "安装 studybuddy（pip install -e .）"
    & $venvPython -m pip install --disable-pip-version-check --no-input -e $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "pip 安装失败（退出码 $LASTEXITCODE）。请检查网络或代理后重试。"
    }

    # 安装完成后做一次轻量自检，确认命令行入口确实可用。
    $cliExe = Join-Path $venvDir "Scripts\studybuddy.exe"
    if (Test-Path -LiteralPath $cliExe) {
        $installedVersion = ""
        try {
            $installedVersion = (& $cliExe --version 2>$null | Select-Object -First 1)
        }
        catch {
            $installedVersion = ""
        }
        if ($installedVersion) {
            Write-Ok "安装完成：$installedVersion"
        }
        else {
            Write-Ok "安装完成"
        }
    }
    else {
        Write-Warn "没有找到 studybuddy.exe，仍然可以用 python -m studybuddy 运行。"
    }

    Write-Host @'

下一步（在 PowerShell 中执行）：

  1. 激活虚拟环境（每次新开窗口都要做一次）：
       .\.venv\Scripts\Activate.ps1

  2. 导入教材：
       studybuddy add "高等数学.pdf" --course 高等数学

  3. 提问：
       studybuddy ask "导数的定义是什么？"

打开网页界面：
       studybuddy ui
     浏览器访问 http://127.0.0.1:8765

可选：把数据目录放到不会被清空的位置（下面只是示例，按需修改）：
       $env:STUDYBUDDY_ROOT = "$HOME\Documents\studybuddy-data\library"

可选：配置模型（下面只是示例，本脚本不会写入任何密钥）：
       $env:STUDYBUDDY_API_KEY = "sk-你的密钥"
       $env:STUDYBUDDY_BASE_URL = "https://api.deepseek.com/v1"
       $env:STUDYBUDDY_MODEL = "deepseek-chat"

提示：如果 PowerShell 提示“禁止运行脚本”，可以只对当前窗口临时放行：
       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

'@

    if ($Launch) {
        Write-Step "启动本地网页界面（按 Ctrl+C 退出）"
        & $venvPython -m studybuddy ui
        if ($LASTEXITCODE -ne 0) {
            throw "启动网页界面失败（退出码 $LASTEXITCODE）。"
        }
    }
    else {
        Write-Host "安装完成。以后随时可以运行：studybuddy ui" -ForegroundColor Green
    }
}

try {
    Install-StudyBuddy
}
catch {
    Write-Err $_.Exception.Message
    Write-Host ""
    Write-Host "安装没有完成。请根据上面的提示处理后重新运行本脚本。" -ForegroundColor Yellow
    Pause-Exit 1
}

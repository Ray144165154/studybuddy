# scripts

辅助工具，不属于运行时依赖。

## 一键安装

| 文件 | 平台 | 作用 |
| --- | --- | --- |
| `install-windows.ps1` | Windows | 找 Python 3.10+、建 `.venv`、`pip install -e .`；加 `-Launch` 装完直接开界面 |
| `install-macos.command` | macOS | 同上，双击即可；装完会问要不要立刻启动界面 |
| `launch-macos.command` | macOS | 日常启动器：双击打开网页界面（可传 `--port 9000` 等参数） |

三个脚本都是幂等的（重复运行安全）、不需要管理员权限、不会下载或执行远端脚本。

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1 -Launch
```

```bash
# macOS
bash scripts/install-macos.command
```

> 如果 PowerShell 报"禁止运行脚本"：
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

## ocr_macos.swift —— 扫描版 PDF 的 OCR

本项目不内置 OCR 引擎：扫描件会被 `read_pdf` 明确拒绝，并提示先做 OCR。
macOS 上可以用系统自带的 Vision 框架（对中文识别质量很好，无需安装 tesseract）：

```bash
# 1. 编译（约 30 秒）
swiftc -O ocr_macos.swift -o /tmp/ocr_macos \
  -framework PDFKit -framework Vision -framework AppKit

# 2. 识别整本 PDF，输出 JSONL（每行一页）
/tmp/ocr_macos 概率论基础.pdf pages.jsonl 1 422 150

# 3. 导入书库
studybuddy add-text pages.jsonl -t "概率论基础" -c 概率论
```

参数：`<输入.pdf> <输出.jsonl> [起始页] [结束页] [DPI]`。
DPI 建议 150–200：150 约 0.35 秒/页，200 更准但慢一倍。

> 注意：Vision 在沙箱受限的环境里可能报 `unknownError`，需要在正常用户会话下运行。

### Windows / Linux

用 [ocrmypdf](https://ocrmypdf.readthedocs.io/) 生成带文字层的 PDF，再直接导入：

```powershell
# Windows
winget install --id UB-Mannheim.TesseractOCR
pip install ocrmypdf
ocrmypdf -l chi_sim 概率论基础.pdf 概率论基础-ocr.pdf
studybuddy add 概率论基础-ocr.pdf -c 概率论
```

```bash
# Linux
sudo apt install tesseract-ocr tesseract-ocr-chi-sim
pip install ocrmypdf
ocrmypdf -l chi_sim 概率论基础.pdf 概率论基础-ocr.pdf
studybuddy add 概率论基础-ocr.pdf -c 概率论
```

> 注：本项目只提供 macOS 的 OCR 脚本；Windows/Linux 不内置 OCR 引擎，用上面这个
> 成熟工具即可。缺 OCR 时 `studybuddy add` 会明确报错并提示，不会导入空书。

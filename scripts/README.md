# scripts

辅助工具，不属于运行时依赖。

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

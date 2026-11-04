"""Built-in OCR integration for scanned PDFs.

Two local backends are supported, chosen automatically by :func:`detect_backend`:

* ``vision`` -- macOS only. The bundled Swift helper (``tools/ocr_macos.swift``) is
  compiled once with ``swiftc`` and cached by source hash, then run over the PDF.
  It writes one JSON object per page (``{"page": n, "text": "..."}``), which is
  exactly what ``studybuddy add-text`` imports.
* ``ocrmypdf`` -- cross platform. ``ocrmypdf --skip-text`` adds a text layer and
  writes a normal searchable PDF.

Nothing here touches the network, and every subprocess call is injected so tests
(and embedders) can run without Swift or Tesseract installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .errors import OcrError

__all__ = [
    "OcrBackend",
    "VISION_SOURCE",
    "backend_hint",
    "compile_command",
    "detect_backend",
    "ocr_pdf",
    "ocrmypdf_command",
    "output_kind",
    "validate_jsonl",
    "vision_command",
]

#: Path to the Swift Vision helper shipped inside the wheel.
VISION_SOURCE: Final = Path(__file__).with_name("tools") / "ocr_macos.swift"

Runner = Callable[..., Any]
Which = Callable[[str], str | None]
ProgressCallback = Callable[[int, int], None]

_VALID_BACKENDS: Final = ("auto", "vision", "ocrmypdf")
_VISION_FRAMEWORKS: Final = ("PDFKit", "Vision", "AppKit")
_OCR_EXECUTABLES: Final = ("ocrmypdf", "ocrmypdf.exe")
_DEFAULT_DPI: Final = 150

_MAC_COMPILE_HINT: Final = (
    "需要 Xcode Command Line Tools 提供 swiftc（xcode-select --install）；"
    "若已安装仍然失败，检查 TMPDIR / clang 模块缓存是否可写，或在本机正常会话（非受限沙箱）下重试。"
)
_MAC_RUN_HINT: Final = "Vision 在沙箱受限的环境里可能报 unknownError，请在正常用户会话下运行。"
_OCRMYPDF_HINT: Final = "请确认已安装 Tesseract 及 chi_sim / eng 语言包；它还需要 Ghostscript。"


@dataclass(slots=True)
class OcrBackend:
    """A local OCR engine.

    ``detail`` is the executable path when the backend is available, and an
    actionable install/fix suggestion when it is not.
    """

    name: str
    available: bool
    detail: str


def _normalize_platform(platform: str | None) -> str:
    value = platform or sys.platform
    if value.startswith("win"):
        return "win32"
    if value in ("macos", "osx"):
        return "darwin"
    return value


def backend_hint(platform: str | None = None) -> str:
    """Return a short install guide for the platform's preferred OCR backend."""
    target = _normalize_platform(platform)
    if target == "darwin":
        return (
            "macOS 可以使用系统自带的 Vision 引擎（中文识别质量好，无需安装 Tesseract）：\n"
            "  1. 安装 Xcode Command Line Tools 以获取 swiftc：xcode-select --install\n"
            "  2. 重新运行：studybuddy ocr <文件.pdf>\n"
            "也可以改用 ocrmypdf：pip install ocrmypdf（还需要 Tesseract 与中文语言包）。"
        )
    if target == "win32":
        return (
            "Windows 需要 ocrmypdf + Tesseract：\n"
            "  1. 安装 Tesseract 中文版：winget install --id UB-Mannheim.TesseractOCR\n"
            "  2. 安装 ocrmypdf：pip install ocrmypdf\n"
            "  3. 重新运行：studybuddy ocr <文件.pdf>"
        )
    if target == "linux":
        return (
            "Linux 需要 ocrmypdf + Tesseract（含中文语言包）：\n"
            "  1. sudo apt install tesseract-ocr tesseract-ocr-chi-sim\n"
            "  2. pip install ocrmypdf\n"
            "  3. 重新运行：studybuddy ocr <文件.pdf>"
        )
    return (
        f"未识别的平台 {target}。请安装 ocrmypdf 与 Tesseract（含 chi_sim 中文语言包）：\n"
        "  pip install ocrmypdf"
    )


def _detect_vision(platform: str, which: Which) -> OcrBackend:
    if platform != "darwin":
        return OcrBackend(
            "vision",
            False,
            f"内置 Vision 后端只支持 macOS（当前平台：{platform}）。\n{backend_hint(platform)}",
        )
    if not VISION_SOURCE.is_file():
        return OcrBackend(
            "vision",
            False,
            "安装包缺少 Swift OCR 源文件（src/studybuddy/tools/ocr_macos.swift）。请重新安装 studybuddy。",
        )
    swiftc = which("swiftc")
    if swiftc is None:
        return OcrBackend("vision", False, backend_hint("darwin"))
    return OcrBackend("vision", True, swiftc)


def _detect_ocrmypdf(platform: str, which: Which) -> OcrBackend:
    # Windows installs expose ``ocrmypdf.exe``; checking both names also covers
    # shells whose PATHEXT lookup is not visible to ``shutil.which``.
    for name in _OCR_EXECUTABLES:
        executable = which(name)
        if executable:
            return OcrBackend("ocrmypdf", True, executable)
    return OcrBackend("ocrmypdf", False, backend_hint(platform))


def detect_backend(
    preferred: str = "auto",
    *,
    platform: str | None = None,
    which: Which = shutil.which,
) -> OcrBackend | None:
    """Pick a local OCR backend.

    ``auto`` prefers Vision on macOS (when ``swiftc`` is available), otherwise
    falls back to ``ocrmypdf``, and returns ``None`` when neither exists. An
    explicitly requested backend is always returned so the caller can show its
    ``detail`` suggestion.
    """
    if preferred not in _VALID_BACKENDS:
        raise OcrError(f"未知的 OCR 后端：{preferred}。可选值：auto、vision、ocrmypdf。")
    target = _normalize_platform(platform)
    if preferred == "auto":
        vision = _detect_vision(target, which)
        if vision.available:
            return vision
        ocrmypdf = _detect_ocrmypdf(target, which)
        return ocrmypdf if ocrmypdf.available else None
    if preferred == "vision":
        return _detect_vision(target, which)
    return _detect_ocrmypdf(target, which)


def output_kind(backend: OcrBackend) -> str:
    """Return ``"jsonl"`` for Vision output and ``"pdf"`` for ocrmypdf output."""
    if backend.name == "vision":
        return "jsonl"
    if backend.name == "ocrmypdf":
        return "pdf"
    raise OcrError(f"不支持的 OCR 后端：{backend.name}")


def compile_command(swiftc: str, source: Path, binary: Path) -> list[str]:
    """Build the ``swiftc`` invocation that compiles the Vision helper."""
    command = [swiftc, "-O", str(source), "-o", str(binary)]
    for framework in _VISION_FRAMEWORKS:
        command.extend(["-framework", framework])
    return command


def vision_command(
    binary: Path, pdf: Path, output: Path, total_pages: int, *, dpi: int = _DEFAULT_DPI
) -> list[str]:
    """Build the helper invocation: ``<bin> in.pdf out.jsonl 1 <pages> <dpi>``."""
    return [str(binary), str(pdf), str(output), "1", str(total_pages), str(dpi)]


def ocrmypdf_command(
    executable: str, pdf: Path, output: Path, *, languages: str = "chi_sim+eng"
) -> list[str]:
    """Build the ocrmypdf invocation; ``--skip-text`` keeps existing text layers."""
    return [executable, "--skip-text", "-l", languages, str(pdf), str(output)]


def _read_jsonl_pages(path: Path, total_pages: int | None) -> list[tuple[int, str]]:
    if not path.is_file():
        raise OcrError(f"OCR 输出文件不存在：{path}")
    pages: list[tuple[int, str]] = []
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OcrError(f"无法读取 OCR 输出：{path}（{exc}）") from exc
    with handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise OcrError(f"OCR 输出不是合法的 JSONL（第 {number} 行）：{exc}") from exc
            if not isinstance(payload, dict):
                raise OcrError(f"OCR 输出第 {number} 行不是 JSON 对象")
            page = payload.get("page")
            text = payload.get("text")
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise OcrError(f"OCR 输出第 {number} 行的 page 字段无效：{page!r}")
            if not isinstance(text, str):
                raise OcrError(f"OCR 输出第 {number} 行缺少字符串 text 字段")
            if total_pages is not None and page > total_pages:
                raise OcrError(f"OCR 输出第 {number} 行声明了第 {page} 页，但 PDF 只有 {total_pages} 页")
            pages.append((page, text))
    return pages


def validate_jsonl(path: Path, *, total_pages: int | None = None) -> int:
    """Validate a Vision JSONL file and return how many pages it contains.

    Every non-blank line must be a JSON object with an integer ``page`` (1-based)
    and a string ``text``. Malformed output raises :class:`OcrError` with the line
    number so a bad conversion is never silently imported as an empty book.
    """
    pages = _read_jsonl_pages(Path(path), total_pages)
    if not pages:
        raise OcrError(f"OCR 没有识别出任何页面文字：{path}。请确认 PDF 是扫描件且页面可读。")
    return len(pages)


def _tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _run(runner: Runner, command: list[str], *, what: str, hint: str = "") -> Any:
    try:
        result = runner(command, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        message = f"{what}无法启动：{exc}"
        raise OcrError(f"{message}\n{hint}" if hint else message) from exc
    if result.returncode != 0:
        detail = _tail(str(result.stderr or result.stdout or ""))
        message = f"{what}失败（退出码 {result.returncode}）"
        if detail:
            message = f"{message}：{detail}"
        raise OcrError(f"{message}\n{hint}" if hint else message)
    return result


def _total_pages(pdf: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise OcrError("pypdf 是必需依赖：pip install pypdf") from exc
    try:
        reader = PdfReader(str(pdf))
        total = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types
        raise OcrError(f"无法读取 PDF 页数：{pdf.name}（{exc}）。文件可能已损坏。") from exc
    if total < 1:
        raise OcrError(f"PDF 没有页面：{pdf.name}")
    return total


def _vision_binary(cache_dir: Path | None) -> Path:
    if not VISION_SOURCE.is_file():
        raise OcrError(
            "安装包缺少 Swift OCR 源文件（src/studybuddy/tools/ocr_macos.swift）。请重新安装 studybuddy。"
        )
    root = Path(cache_dir) if cache_dir is not None else Path(tempfile.gettempdir()) / "studybuddy-ocr"
    try:
        digest = hashlib.sha256(VISION_SOURCE.read_bytes()).hexdigest()[:12]
    except OSError as exc:
        raise OcrError(f"无法读取 Swift OCR 源文件：{VISION_SOURCE}（{exc}）") from exc
    return root / f"ocr_macos-{digest}"


def _notify(progress: ProgressCallback | None, done: int, total: int) -> None:
    if progress is not None:
        progress(done, total)


def _replace_output(output: Path) -> None:
    """Remove a previous artifact so a shorter rerun cannot leave stale lines."""
    if not output.exists():
        return
    try:
        output.unlink()
    except OSError as exc:
        raise OcrError(f"无法覆盖输出文件：{output}（{exc}）") from exc


def _ocr_with_vision(
    pdf: Path,
    output: Path,
    backend: OcrBackend,
    runner: Runner,
    progress: ProgressCallback | None,
    cache_dir: Path | None,
    dpi: int,
) -> Path:
    total = _total_pages(pdf)
    _notify(progress, 0, total)

    swiftc = backend.detail or shutil.which("swiftc")
    if not swiftc:
        raise OcrError(f"找不到 swiftc。\n{backend_hint('darwin')}")
    binary = _vision_binary(cache_dir)
    if not binary.is_file():
        binary.parent.mkdir(parents=True, exist_ok=True)
        _run(
            runner,
            compile_command(swiftc, VISION_SOURCE, binary),
            what="编译 Swift OCR 助手（swiftc）",
            hint=_MAC_COMPILE_HINT,
        )
        if not binary.is_file():
            raise OcrError(f"swiftc 已成功退出，但没有生成可执行文件：{binary}")

    _run(
        runner,
        vision_command(binary, pdf, output, total, dpi=dpi),
        what="运行 macOS Vision OCR",
        hint=_MAC_RUN_HINT,
    )
    if not output.is_file():
        raise OcrError(f"OCR 没有生成输出文件：{output}")
    pages = _read_jsonl_pages(output, total)
    if not pages:
        raise OcrError(f"OCR 没有识别出任何页面文字：{output}。请确认 PDF 是扫描件且页面可读。")
    for page, _text in pages:
        _notify(progress, page, total)
    return output


def _ocr_with_ocrmypdf(
    pdf: Path,
    output: Path,
    backend: OcrBackend,
    runner: Runner,
    progress: ProgressCallback | None,
) -> Path:
    executable = backend.detail or "ocrmypdf"
    _run(
        runner,
        ocrmypdf_command(executable, pdf, output),
        what="运行 ocrmypdf",
        hint=f"{_OCRMYPDF_HINT}\n{backend_hint()}",
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise OcrError(f"ocrmypdf 已成功退出，但没有生成 PDF：{output}")
    # ocrmypdf does not stream per-page progress; report one completion tick.
    _notify(progress, 1, 1)
    return output


def ocr_pdf(
    pdf: Path,
    output: Path,
    *,
    backend: OcrBackend | None = None,
    runner: Runner = subprocess.run,
    progress: ProgressCallback | None = None,
    cache_dir: Path | None = None,
    dpi: int = _DEFAULT_DPI,
) -> Path:
    """OCR ``pdf`` into ``output`` and return the artifact that was written.

    With the Vision backend the artifact is a JSONL file (one ``{"page", "text"}``
    object per page); with ocrmypdf it is a searchable PDF. ``backend=None``
    auto-detects, ``cache_dir`` overrides where the compiled Swift helper lives,
    and ``progress(done, total)`` is called as pages are completed.

    Raises
    ------
    OcrError
        No backend is available, the input is missing/corrupt, the tool failed,
        or the produced output is missing or malformed.
    """
    source = Path(pdf)
    target = Path(output)
    if not source.is_file():
        raise OcrError(f"找不到 PDF 文件：{source}")
    try:
        source_size = source.stat().st_size
    except OSError as exc:
        raise OcrError(f"无法读取 PDF 文件：{source}（{exc}）") from exc
    if source_size == 0:
        raise OcrError(f"PDF 文件为空：{source}")
    if source.resolve() == target.resolve():
        raise OcrError(f"输入和输出不能是同一个文件：{source}")

    selected = backend if backend is not None else detect_backend()
    if selected is None:
        raise OcrError(f"没有找到可用的 OCR 后端。\n{backend_hint()}")
    if not selected.available:
        raise OcrError(selected.detail or backend_hint())

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OcrError(f"无法创建输出目录：{target.parent}（{exc}）") from exc

    kind = output_kind(selected)
    _replace_output(target)
    if kind == "jsonl":
        return _ocr_with_vision(source, target, selected, runner, progress, cache_dir, dpi)
    return _ocr_with_ocrmypdf(source, target, selected, runner, progress)

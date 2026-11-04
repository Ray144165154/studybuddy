"""Built-in OCR: backend detection, command construction, and mocked runs.

Nothing here compiles Swift or invokes ``ocrmypdf``: every process call goes through
an injected runner, so the suite stays offline and deterministic on any platform.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from studybuddy import ocr
from studybuddy.errors import AgentError, OcrError, PDFError
from studybuddy.pdf import read_pdf
from support import make_pdf

SWIFTC = "/usr/bin/swiftc"
OCRMYPDF = "/usr/local/bin/ocrmypdf"
VISION_JSONL = (
    json.dumps({"page": 1, "text": "第一章 极限"}, ensure_ascii=False)
    + "\n"
    + json.dumps({"page": 2, "text": "second page"}, ensure_ascii=False)
    + "\n"
)


def completed(argv: list[str], code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout="", stderr=stderr)


def fake_which(available: dict[str, str]) -> Any:
    def which(name: str) -> str | None:
        return available.get(name)

    return which


def make_backend(name: str = "vision", *, available: bool = True, detail: str = SWIFTC) -> ocr.OcrBackend:
    return ocr.OcrBackend(name=name, available=available, detail=detail)


class FakeRunner:
    """Record every command and let the test script what happens to the filesystem."""

    def __init__(self, handler: Any = None) -> None:
        self.commands: list[list[str]] = []
        self._handler = handler

    def __call__(self, command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(part) for part in command]
        self.commands.append(argv)
        if self._handler is not None:
            return self._handler(argv)
        return completed(argv)


class TestOcrError(unittest.TestCase):
    def test_ocr_error_is_an_agent_error(self) -> None:
        self.assertTrue(issubclass(OcrError, AgentError))


class TestDetectBackend(unittest.TestCase):
    def test_auto_prefers_vision_on_macos(self) -> None:
        result = ocr.detect_backend(
            platform="darwin", which=fake_which({"swiftc": SWIFTC, "ocrmypdf": OCRMYPDF})
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.name, "vision")
        self.assertTrue(result.available)
        self.assertEqual(result.detail, SWIFTC)

    def test_auto_prefers_ocrmypdf_on_linux_even_with_swiftc(self) -> None:
        result = ocr.detect_backend(
            platform="linux", which=fake_which({"swiftc": SWIFTC, "ocrmypdf": OCRMYPDF})
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.name, "ocrmypdf")
        self.assertTrue(result.available)

    def test_auto_falls_back_to_ocrmypdf_when_swiftc_is_missing(self) -> None:
        result = ocr.detect_backend(platform="darwin", which=fake_which({"ocrmypdf": OCRMYPDF}))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual((result.name, result.available), ("ocrmypdf", True))

    def test_auto_returns_none_when_nothing_is_available(self) -> None:
        self.assertIsNone(ocr.detect_backend(platform="darwin", which=fake_which({})))
        self.assertIsNone(ocr.detect_backend(platform="linux", which=fake_which({})))

    def test_explicit_vision_on_linux_explains_the_platform_gap(self) -> None:
        result = ocr.detect_backend("vision", platform="linux", which=fake_which({"swiftc": SWIFTC}))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.available)
        self.assertIn("macOS", result.detail)
        self.assertIn("ocrmypdf", result.detail.lower())

    def test_explicit_vision_without_swiftc_points_at_command_line_tools(self) -> None:
        result = ocr.detect_backend("vision", platform="darwin", which=fake_which({}))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.available)
        self.assertIn("xcode-select --install", result.detail)

    def test_explicit_vision_without_bundled_source_is_unavailable(self) -> None:
        with patch.object(ocr, "VISION_SOURCE", Path("/nonexistent/ocr_macos.swift")):
            result = ocr.detect_backend("vision", platform="darwin", which=fake_which({"swiftc": SWIFTC}))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.available)
        self.assertIn("重新安装", result.detail)

    def test_auto_skips_vision_when_bundled_source_is_missing(self) -> None:
        with patch.object(ocr, "VISION_SOURCE", Path("/nonexistent/ocr_macos.swift")):
            result = ocr.detect_backend(
                platform="darwin", which=fake_which({"swiftc": SWIFTC, "ocrmypdf": OCRMYPDF})
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.name, "ocrmypdf")

    def test_explicit_ocrmypdf_missing_suggests_install_commands(self) -> None:
        result = ocr.detect_backend("ocrmypdf", platform="linux", which=fake_which({}))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.available)
        self.assertIn("pip install ocrmypdf", result.detail)
        self.assertIn("tesseract", result.detail.lower())

    def test_ocrmypdf_detection_accepts_the_windows_exe_suffix(self) -> None:
        exe = "C:/Tools/ocrmypdf.exe"
        result = ocr.detect_backend("ocrmypdf", platform="win32", which=fake_which({"ocrmypdf.exe": exe}))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.available)
        self.assertEqual(result.detail, exe)

    def test_unknown_preferred_backend_is_an_actionable_error(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.detect_backend("tesseract", platform="darwin", which=fake_which({}))
        message = str(ctx.exception)
        self.assertIn("tesseract", message)
        self.assertIn("auto", message)


class TestBackendHint(unittest.TestCase):
    def test_macos_hint_mentions_vision_and_swiftc(self) -> None:
        hint = ocr.backend_hint("darwin")
        self.assertIn("swiftc", hint)
        self.assertIn("xcode-select --install", hint)
        self.assertIn("Vision", hint)
        self.assertIn("pip install ocrmypdf", hint)

    def test_windows_hint_mentions_winget_and_tesseract(self) -> None:
        hint = ocr.backend_hint("win32")
        self.assertIn("winget", hint)
        self.assertIn("Tesseract", hint)
        self.assertIn("pip install ocrmypdf", hint)

    def test_linux_hint_mentions_apt_and_chinese_language_pack(self) -> None:
        hint = ocr.backend_hint("linux")
        self.assertIn("apt install", hint)
        self.assertIn("tesseract-ocr-chi-sim", hint)
        self.assertIn("pip install ocrmypdf", hint)

    def test_hint_uses_the_current_platform_by_default(self) -> None:
        with patch.object(ocr.sys, "platform", "linux"):
            self.assertIn("apt install", ocr.backend_hint())


class TestOutputKind(unittest.TestCase):
    def test_vision_writes_jsonl(self) -> None:
        self.assertEqual(ocr.output_kind(make_backend("vision")), "jsonl")

    def test_ocrmypdf_writes_pdf(self) -> None:
        self.assertEqual(ocr.output_kind(make_backend("ocrmypdf", detail=OCRMYPDF)), "pdf")

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(OcrError):
            ocr.output_kind(make_backend("tesseract"))


class TestCommandBuilding(unittest.TestCase):
    def test_compile_command_uses_the_frameworks_the_script_needs(self) -> None:
        command = ocr.compile_command(SWIFTC, Path("/pkg/ocr_macos.swift"), Path("/tmp/studybuddy-ocr/bin"))
        self.assertEqual(
            command,
            [
                SWIFTC,
                "-O",
                "/pkg/ocr_macos.swift",
                "-o",
                "/tmp/studybuddy-ocr/bin",
                "-framework",
                "PDFKit",
                "-framework",
                "Vision",
                "-framework",
                "AppKit",
            ],
        )

    def test_vision_command_matches_the_documented_argument_order(self) -> None:
        command = ocr.vision_command(Path("/tmp/bin"), Path("/tmp/in.pdf"), Path("/tmp/pages.jsonl"), 422)
        self.assertEqual(command, ["/tmp/bin", "/tmp/in.pdf", "/tmp/pages.jsonl", "1", "422", "150"])

    def test_vision_command_honours_a_custom_dpi(self) -> None:
        command = ocr.vision_command(
            Path("/tmp/bin"), Path("/tmp/in.pdf"), Path("/tmp/pages.jsonl"), 7, dpi=200
        )
        self.assertEqual(command[-1], "200")

    def test_ocrmypdf_command_skips_existing_text_layers(self) -> None:
        command = ocr.ocrmypdf_command(OCRMYPDF, Path("/tmp/in.pdf"), Path("/tmp/in-ocr.pdf"))
        self.assertEqual(
            command,
            [
                OCRMYPDF,
                "--skip-text",
                "-l",
                "chi_sim+eng",
                "/tmp/in.pdf",
                "/tmp/in-ocr.pdf",
            ],
        )

    def test_ocrmypdf_command_accepts_a_custom_language_set(self) -> None:
        command = ocr.ocrmypdf_command(OCRMYPDF, Path("/tmp/in.pdf"), Path("/tmp/out.pdf"), languages="eng")
        self.assertEqual(command[3], "eng")

    def test_ocrmypdf_command_keeps_a_windows_exe_executable(self) -> None:
        exe = "C:/Tools/ocrmypdf.exe"
        command = ocr.ocrmypdf_command(exe, Path("C:/in.pdf"), Path("C:/out.pdf"))
        self.assertEqual(command[0], exe)


class TestValidateJsonl(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, text: str) -> Path:
        path = self.tmp / "pages.jsonl"
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_output_reports_the_page_count(self) -> None:
        self.assertEqual(ocr.validate_jsonl(self.write(VISION_JSONL), total_pages=2), 2)

    def test_blank_lines_are_ignored(self) -> None:
        text = "\n" + VISION_JSONL.replace("\n", "\n\n", 1) + "\n"
        self.assertEqual(ocr.validate_jsonl(self.write(text)), 2)

    def test_invalid_json_mentions_the_line_number(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.validate_jsonl(self.write("not json\n"))
        self.assertIn("第 1 行", str(ctx.exception))

    def test_non_object_line_is_rejected(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.validate_jsonl(self.write("[]\n"))
        self.assertIn("JSON", str(ctx.exception))

    def test_missing_text_field_is_rejected(self) -> None:
        text = json.dumps({"page": 1}) + "\n"
        with self.assertRaises(OcrError) as ctx:
            ocr.validate_jsonl(self.write(text))
        self.assertIn("text", str(ctx.exception))

    def test_non_string_text_field_is_rejected(self) -> None:
        text = json.dumps({"page": 1, "text": 3}) + "\n"
        with self.assertRaises(OcrError):
            ocr.validate_jsonl(self.write(text))

    def test_non_integer_page_is_rejected(self) -> None:
        for page in ("1", True, 0):
            with self.subTest(page=page):
                text = json.dumps({"page": page, "text": "x"}) + "\n"
                with self.assertRaises(OcrError):
                    ocr.validate_jsonl(self.write(text))

    def test_page_beyond_the_document_is_rejected(self) -> None:
        text = json.dumps({"page": 3, "text": "x"}) + "\n"
        with self.assertRaises(OcrError) as ctx:
            ocr.validate_jsonl(self.write(text), total_pages=2)
        self.assertIn("只有 2 页", str(ctx.exception))

    def test_empty_output_is_rejected(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.validate_jsonl(self.write("\n\n"))
        self.assertIn("没有识别出", str(ctx.exception))

    def test_missing_output_file_is_rejected(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.validate_jsonl(self.tmp / "nope.jsonl")
        self.assertIn("nope.jsonl", str(ctx.exception))


class TestVisionRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pdf = make_pdf(self.tmp / "book.pdf", [["Page one"], ["Page two"]])

    def runner(self, output_text: str = VISION_JSONL, *, compile_code: int = 0) -> FakeRunner:
        def handler(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if "-framework" in argv:  # compile step
                binary = Path(argv[argv.index("-o") + 1])
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("fake binary", encoding="utf-8")
                return completed(argv, compile_code, "error: unable to find utility")
            Path(argv[2]).write_text(output_text, encoding="utf-8")
            return completed(argv)

        return FakeRunner(handler)

    def test_compiles_then_runs_with_page_progress(self) -> None:
        output = self.tmp / "pages.jsonl"
        progress: list[tuple[int, int]] = []
        runner = self.runner()
        result = ocr.ocr_pdf(
            self.pdf,
            output,
            backend=make_backend(),
            runner=runner,
            progress=lambda done, total: progress.append((done, total)),
            cache_dir=self.tmp / "cache",
        )

        self.assertEqual(result, output)
        self.assertEqual(len(runner.commands), 2)
        compile_command, run_command = runner.commands

        self.assertEqual(compile_command[0], SWIFTC)
        self.assertEqual(compile_command[1], "-O")
        self.assertEqual(compile_command[2], str(ocr.VISION_SOURCE))
        self.assertEqual(compile_command[3], "-o")
        for framework in ("PDFKit", "Vision", "AppKit"):
            self.assertIn(framework, compile_command)

        self.assertEqual(run_command[0], compile_command[4])
        self.assertEqual(run_command[1], str(self.pdf))
        self.assertEqual(run_command[2], str(output))
        self.assertEqual(run_command[3:], ["1", "2", "150"])
        self.assertEqual(progress, [(0, 2), (1, 2), (2, 2)])
        self.assertTrue(output.read_text(encoding="utf-8").strip())

    def test_cached_binary_is_not_recompiled(self) -> None:
        runner = self.runner()
        cache = self.tmp / "cache"
        ocr.ocr_pdf(
            self.pdf,
            self.tmp / "a.jsonl",
            backend=make_backend(),
            runner=runner,
            cache_dir=cache,
        )
        ocr.ocr_pdf(
            self.pdf,
            self.tmp / "b.jsonl",
            backend=make_backend(),
            runner=runner,
            cache_dir=cache,
        )
        compiles = [command for command in runner.commands if "-framework" in command]
        runs = [command for command in runner.commands if "-framework" not in command]
        self.assertEqual(len(compiles), 1)
        self.assertEqual(len(runs), 2)

    def test_compile_failure_explains_how_to_get_swiftc(self) -> None:
        runner = self.runner(compile_code=1)
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "pages.jsonl",
                backend=make_backend(),
                runner=runner,
                cache_dir=self.tmp / "cache",
            )
        message = str(ctx.exception)
        self.assertIn("swiftc", message)
        self.assertIn("xcode-select --install", message)
        self.assertIn("unable to find utility", message)
        self.assertEqual(len(runner.commands), 1)

    def test_run_failure_includes_the_exit_code_and_stderr(self) -> None:
        def handler(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if "-framework" in argv:
                Path(argv[argv.index("-o") + 1]).write_text("fake", encoding="utf-8")
                return completed(argv)
            return completed(argv, 1, "error: unknownError")

        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "pages.jsonl",
                backend=make_backend(),
                runner=FakeRunner(handler),
                cache_dir=self.tmp / "cache",
            )
        message = str(ctx.exception)
        self.assertIn("退出码 1", message)
        self.assertIn("unknownError", message)

    def test_malformed_jsonl_is_reported_after_the_run(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "pages.jsonl",
                backend=make_backend(),
                runner=self.runner("not json\n"),
                cache_dir=self.tmp / "cache",
            )
        self.assertIn("第 1 行", str(ctx.exception))

    def test_empty_jsonl_is_reported_as_no_text(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "pages.jsonl",
                backend=make_backend(),
                runner=self.runner(""),
                cache_dir=self.tmp / "cache",
            )
        self.assertIn("没有识别出", str(ctx.exception))

    def test_output_directory_is_created(self) -> None:
        output = self.tmp / "nested" / "pages.jsonl"
        ocr.ocr_pdf(
            self.pdf,
            output,
            backend=make_backend(),
            runner=self.runner(),
            cache_dir=self.tmp / "cache",
        )
        self.assertTrue(output.is_file())

    def test_existing_output_is_replaced_not_appended(self) -> None:
        output = self.tmp / "pages.jsonl"
        output.write_text(json.dumps({"page": 1, "text": "stale"}) + "\n", encoding="utf-8")
        ocr.ocr_pdf(
            self.pdf,
            output,
            backend=make_backend(),
            runner=self.runner(),
            cache_dir=self.tmp / "cache",
        )
        content = output.read_text(encoding="utf-8")
        self.assertNotIn("stale", content)
        self.assertIn("第一章", content)

    def test_input_and_output_must_differ(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.pdf,
                backend=make_backend(),
                runner=self.runner(),
                cache_dir=self.tmp / "cache",
            )
        self.assertIn("同一个文件", str(ctx.exception))

    def test_missing_input_pdf_is_an_ocr_error(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.tmp / "missing.pdf",
                self.tmp / "pages.jsonl",
                backend=make_backend(),
                runner=self.runner(),
                cache_dir=self.tmp / "cache",
            )
        self.assertIn("missing.pdf", str(ctx.exception))

    def test_unavailable_backend_reports_its_detail(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "pages.jsonl",
                backend=make_backend(
                    available=False, detail="请先安装 Xcode Command Line Tools：xcode-select --install"
                ),
                runner=self.runner(),
                cache_dir=self.tmp / "cache",
            )
        self.assertIn("xcode-select --install", str(ctx.exception))

    def test_unknown_backend_name_is_rejected(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "out",
                backend=make_backend("tesseract", detail="x"),
                runner=self.runner(),
                cache_dir=self.tmp / "cache",
            )
        self.assertIn("tesseract", str(ctx.exception))

    def test_no_detected_backend_surfaces_the_platform_hint(self) -> None:
        with (
            patch.object(ocr, "detect_backend", return_value=None),
            self.assertRaises(OcrError) as ctx,
        ):
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "pages.jsonl",
                runner=self.runner(),
                cache_dir=self.tmp / "cache",
            )
        self.assertIn("ocrmypdf", str(ctx.exception).lower())


class TestOcrmypdfRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pdf = make_pdf(self.tmp / "scanned.pdf", [["Page one"]])

    def runner(self, *, code: int = 0, stderr: str = "", write: bool = True) -> FakeRunner:
        def handler(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if write:
                Path(argv[-1]).write_bytes(b"%PDF-1.4\n% ocr text layer\n")
            return completed(argv, code, stderr)

        return FakeRunner(handler)

    def test_runs_ocrmypdf_and_returns_the_searchable_pdf(self) -> None:
        output = self.tmp / "scanned-ocr.pdf"
        progress: list[tuple[int, int]] = []
        runner = self.runner()
        result = ocr.ocr_pdf(
            self.pdf,
            output,
            backend=make_backend("ocrmypdf", detail=OCRMYPDF),
            runner=runner,
            progress=lambda done, total: progress.append((done, total)),
        )
        self.assertEqual(result, output)
        self.assertEqual(runner.commands[0], ocr.ocrmypdf_command(OCRMYPDF, self.pdf, output))
        self.assertEqual(progress, [(1, 1)])
        self.assertTrue(output.is_file())

    def test_uses_the_backend_detected_when_none_is_given(self) -> None:
        output = self.tmp / "scanned-ocr.pdf"
        runner = self.runner()
        with patch.object(ocr, "detect_backend", return_value=make_backend("ocrmypdf", detail=OCRMYPDF)):
            ocr.ocr_pdf(self.pdf, output, runner=runner)
        self.assertEqual(runner.commands[0][0], OCRMYPDF)

    def test_windows_executable_name_is_used_verbatim(self) -> None:
        exe = "C:/Tools/ocrmypdf.exe"
        output = self.tmp / "scanned-ocr.pdf"
        runner = self.runner()
        ocr.ocr_pdf(self.pdf, output, backend=make_backend("ocrmypdf", detail=exe), runner=runner)
        self.assertEqual(runner.commands[0][0], exe)

    def test_failure_mentions_tesseract_and_the_stderr(self) -> None:
        runner = self.runner(code=2, stderr="Could not find language data for chi_sim")
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "scanned-ocr.pdf",
                backend=make_backend("ocrmypdf", detail=OCRMYPDF),
                runner=runner,
            )
        message = str(ctx.exception)
        self.assertIn("chi_sim", message)
        self.assertIn("Tesseract", message)

    def test_missing_artifact_is_an_error(self) -> None:
        with self.assertRaises(OcrError) as ctx:
            ocr.ocr_pdf(
                self.pdf,
                self.tmp / "scanned-ocr.pdf",
                backend=make_backend("ocrmypdf", detail=OCRMYPDF),
                runner=self.runner(write=False),
            )
        self.assertIn("没有生成", str(ctx.exception))


class TestScannedPdfMessage(unittest.TestCase):
    def test_scanned_pdf_points_at_the_builtin_ocr_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blank = make_pdf(Path(directory) / "blank.pdf", [[]])
            with self.assertRaises(PDFError) as ctx:
                read_pdf(blank)
        message = str(ctx.exception)
        self.assertIn("OCR", message)
        self.assertIn("studybuddy ocr", message)


class TestBundledVisionSource(unittest.TestCase):
    def test_swift_source_ships_inside_the_package(self) -> None:
        self.assertTrue(ocr.VISION_SOURCE.is_file())
        source = ocr.VISION_SOURCE.read_text(encoding="utf-8")
        self.assertIn("import Vision", source)
        self.assertIn("VNRecognizeTextRequest", source)
        self.assertIn("PDFKit", source)


if __name__ == "__main__":
    unittest.main()

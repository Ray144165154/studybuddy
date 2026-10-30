"""PDF reading: page extraction, cleanup, and honest failures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studybuddy import read_pdf
from studybuddy.errors import PDFError
from support import make_pdf, make_textbook


class TestReadPdf(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_pages_and_text(self) -> None:
        document = read_pdf(make_textbook(self.tmp / "book.pdf"))
        self.assertEqual(document.page_count, 2)
        self.assertEqual(document.non_empty_pages, 2)
        self.assertIn("derivative", document.pages[0].text.lower())
        self.assertIn("chain rule", document.pages[1].text.lower())

    def test_bare_page_number_is_removed(self) -> None:
        document = read_pdf(make_textbook(self.tmp / "book.pdf"))
        lines = [line.strip() for line in document.pages[0].text.split("\n")]
        self.assertNotIn("42", lines)

    def test_ocr_style_page_number_is_removed(self) -> None:
        # Scanned books often OCR the folio as "•30・", "·30·" or "- 30 -".
        # The PDF fixture writer is latin-1 only, so the cleaner is tested directly.
        from studybuddy.pdf import clean_text

        for folio in ("•30・", "·30·", "- 30 -", "第 30 页", "30"):
            cleaned = clean_text(f"{folio}\nChapter 1\nbody text")
            self.assertNotIn("30", cleaned, folio)
            self.assertIn("body text", cleaned)

    def test_page_numbers_are_one_based(self) -> None:
        document = read_pdf(make_textbook(self.tmp / "book.pdf"))
        self.assertEqual([page.number for page in document.pages], [1, 2])

    def test_full_text_joins_pages(self) -> None:
        document = read_pdf(make_textbook(self.tmp / "book.pdf"))
        self.assertIn("Chapter 3", document.full_text())

    def test_missing_file(self) -> None:
        with self.assertRaises(PDFError) as ctx:
            read_pdf(self.tmp / "nope.pdf")
        self.assertIn("not found", str(ctx.exception))

    def test_empty_file(self) -> None:
        empty = self.tmp / "empty.pdf"
        empty.write_bytes(b"")
        with self.assertRaises(PDFError):
            read_pdf(empty)

    def test_not_a_pdf(self) -> None:
        junk = self.tmp / "junk.pdf"
        junk.write_bytes(b"this is definitely not a pdf")
        with self.assertRaises(PDFError):
            read_pdf(junk)

    def test_pdf_without_text_says_ocr(self) -> None:
        blank = make_pdf(self.tmp / "blank.pdf", [[]])
        with self.assertRaises(PDFError) as ctx:
            read_pdf(blank)
        self.assertIn("OCR", str(ctx.exception))

    def test_lone_surrogate_is_repaired(self) -> None:
        # Regression: pypdf hands back lone surrogates for italic maths glyphs
        # (𝑥 U+1D465 = "\ud835\udc65"), which used to abort the import with
        # "UnicodeEncodeError: surrogates not allowed" when the index was written.
        from studybuddy.pdf import clean_text

        paired = clean_text("设 f(\ud835\udc65) = x²")
        self.assertIn("f(\U0001d465)", paired)
        paired.encode("utf-8")  # must not raise

        unpaired = clean_text("残破的 \ud835 符号")
        unpaired.encode("utf-8")  # must not raise either
        self.assertIn("\ufffd", unpaired)

    def test_clean_text_is_utf8_safe_for_every_input(self) -> None:
        from studybuddy.pdf import clean_text

        for text in ("\ud835", "\udc65", "a\ud835b", "\ud835\udc65\ud835"):
            clean_text(text).encode("utf-8")


if __name__ == "__main__":
    unittest.main()

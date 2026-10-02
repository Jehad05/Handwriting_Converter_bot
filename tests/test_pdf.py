from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from converter import create_pdf


class PdfTests(unittest.TestCase):
    def test_arabic_english_and_mixed_text_build_a_readable_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "arabic-mixed.pdf"
            text = (
                "# دفتر رسائلي — 2026-10-01\n\n"
                "هذه فقرة عربية تحتوي على رقم 12 واسم Alice ونسبة 30%.\n\n"
                "English paragraph with Arabic: مرحبًا, version 2.1 and range 6-8."
            )
            create_pdf(text, output)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1_000)
            reader = PdfReader(str(output))
            self.assertGreaterEqual(len(reader.pages), 1)
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("Alice", extracted)
            self.assertIn("English paragraph", extracted)
            self.assertIn("12", extracted)

    def test_long_arabic_paragraph_splits_across_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "long-arabic.pdf"
            create_pdf("هذه فقرة عربية طويلة لاختبار فواصل الصفحات. " * 240, output)
            reader = PdfReader(str(output))
            self.assertGreaterEqual(len(reader.pages), 2)


if __name__ == "__main__":
    unittest.main()

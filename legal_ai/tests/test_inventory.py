from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

from legal_ai.domain import BookConfig, LegalAIError
from legal_ai.inventory import (
    file_sha256,
    inspect_book,
    load_books,
    resolve_book_path,
)


def sample_book(file_path: str, expected_pages: int = 1) -> BookConfig:
    return BookConfig.from_dict(
        {
            "book_id": "synthetic-book",
            "title": "合成法律文本",
            "subtitle": None,
            "authors": [],
            "translators": [],
            "edition": None,
            "volume": None,
            "language": "zh-CN",
            "jurisdictions": ["测试"],
            "subjects": ["测试"],
            "source_type": "学理来源",
            "authority_tier": "secondary",
            "file_path": file_path,
            "expected_pdf_pages": expected_pages,
            "extract_mode": "embedded_text",
            "enabled": True,
        }
    )


class InventoryTests(unittest.TestCase):
    def test_legacy_embedded_config_maps_to_v2_policies(self):
        book = sample_book("book/sample.pdf")
        self.assertEqual(book.extract_mode, "embedded_text")
        self.assertEqual(book.text_layer_policy, "embedded_only")
        self.assertEqual(book.ocr_policy, "off")
        self.assertIsNone(book.ocr_profile)

    def test_v2_ocr_config_does_not_need_legacy_extract_mode(self):
        value = {
            "book_id": "ocr-book",
            "title": "合成 OCR 书籍",
            "authors": [],
            "translators": [],
            "language": "zh-CN",
            "jurisdictions": ["测试"],
            "subjects": ["测试"],
            "source_type": "学理来源",
            "authority_tier": "secondary",
            "file_path": "book/ocr.pdf",
            "expected_pdf_pages": 3,
            "text_layer_policy": "auto",
            "ocr_policy": "low_quality_pages",
            "ocr_profile": "zh_book_cpu_v1",
            "enabled": False,
        }
        book = BookConfig.from_dict(value)
        self.assertIsNone(book.extract_mode)
        self.assertEqual(book.text_layer_policy, "auto")
        self.assertEqual(book.ocr_policy, "low_quality_pages")

    def test_file_sha256_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.bin"
            path.write_bytes(b"legal-ai")
            first = file_sha256(path)
            second = file_sha256(path)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)

    def test_rejects_path_outside_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(LegalAIError):
                resolve_book_path(root, "../outside.pdf")

    def test_page_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "book" / "sample.pdf"
            pdf_path.parent.mkdir()
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with pdf_path.open("wb") as handle:
                writer.write(handle)
            with self.assertRaisesRegex(LegalAIError, "页数不一致"):
                inspect_book(sample_book("book/sample.pdf", 2), root)

    def test_duplicate_book_id_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "books.json"
            item = {
                "book_id": "duplicate",
                "title": "测试",
                "authors": [],
                "translators": [],
                "language": "zh-CN",
                "jurisdictions": [],
                "subjects": [],
                "source_type": "学理来源",
                "authority_tier": "secondary",
                "file_path": "book/test.pdf",
                "expected_pdf_pages": 1,
                "extract_mode": "embedded_text",
                "enabled": True,
            }
            config.write_text(
                json.dumps([item, item], ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LegalAIError, "book_id 重复"):
                load_books(config)


if __name__ == "__main__":
    unittest.main()

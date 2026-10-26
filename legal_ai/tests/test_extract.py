from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

from legal_ai.domain import InventoryRecord
from legal_ai.extract import extract_book
from legal_ai.inventory import file_sha256
from legal_ai.tests.test_inventory import sample_book


class ExtractionTests(unittest.TestCase):
    def test_blank_pdf_produces_one_record_per_page_and_stable_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "book" / "sample.pdf"
            pdf_path.parent.mkdir()
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_blank_page(width=100, height=100)
            with pdf_path.open("wb") as handle:
                writer.write(handle)
            book = sample_book("book/sample.pdf", 2)
            record = InventoryRecord(
                book=book,
                absolute_path=pdf_path,
                sha256=file_sha256(pdf_path),
                actual_pdf_pages=2,
            )
            project_root = root / "legal_ai"

            first_report = extract_book(record, project_root)
            output = (
                project_root
                / "data"
                / "raw"
                / "pages"
                / "synthetic-book.jsonl"
            )
            first_rows = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            second_report = extract_book(record, project_root)
            second_rows = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(len(first_rows), 2)
            self.assertEqual(first_rows, second_rows)
            self.assertEqual(
                [row["page_id"] for row in first_rows],
                ["synthetic-book:p0001", "synthetic-book:p0002"],
            )
            self.assertTrue(first_report["source_pdf_unchanged"])
            self.assertEqual(first_report, second_report)
            self.assertEqual(first_report["empty_page_count"], 2)

    def test_valid_checkpoint_is_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "book" / "sample.pdf"
            pdf_path.parent.mkdir()
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_blank_page(width=100, height=100)
            with pdf_path.open("wb") as handle:
                writer.write(handle)
            source_hash = file_sha256(pdf_path)
            book = sample_book("book/sample.pdf", 2)
            record = InventoryRecord(
                book=book,
                absolute_path=pdf_path,
                sha256=source_hash,
                actual_pdf_pages=2,
            )
            project_root = root / "legal_ai"
            partial = (
                project_root
                / "data"
                / "raw"
                / "pages"
                / "synthetic-book.jsonl.partial"
            )
            partial.parent.mkdir(parents=True)
            checkpoint = {
                "page_id": "synthetic-book:p0001",
                "book_id": "synthetic-book",
                "pdf_page": 1,
                "raw_text": "",
                "raw_text_sha256": (
                    "e3b0c44298fc1c149afbf4c8996fb924"
                    "27ae41e4649b934ca495991b7852b855"
                ),
                "extraction_error": None,
                "quality_flags": ["empty"],
                "pipeline_version": "p2-ocr-v1",
                "source_pdf_sha256": source_hash,
            }
            partial.write_text(
                json.dumps(checkpoint, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            messages: list[str] = []
            report = extract_book(
                record, project_root, progress=messages.append
            )

            self.assertEqual(report["page_record_count"], 2)
            self.assertTrue(any("从断点续跑" in item for item in messages))
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()

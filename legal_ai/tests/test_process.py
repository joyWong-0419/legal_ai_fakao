from __future__ import annotations

import json
import tempfile
import unittest
import hashlib
from pathlib import Path

from legal_ai.domain import BookConfig, InventoryRecord
from legal_ai.inventory import file_sha256
from legal_ai.ocr import write_ocr_pages
from legal_ai.process import build_chapter_paths, process_book, select_canonical_page
from legal_ai.tests.test_inventory import sample_book


class ProcessTests(unittest.TestCase):
    def test_registered_ocr_exclusion_overrides_an_existing_generated_ocr_page(self):
        record = InventoryRecord(
            book=sample_book("book/sample.pdf", 1),
            absolute_path=Path("sample.pdf"),
            sha256="a" * 64,
            actual_pdf_pages=1,
        )
        canonical = select_canonical_page(
            record=record,
            raw_page={
                "page_id": "synthetic-book:p0001",
                "raw_text": "embedded text that must not become canonical",
            },
            ocr_page={
                "ocr_status": "generated",
                "ocr_text": "OCR audit text that must not become canonical",
                "ocr_engine": "synthetic",
                "ocr_avg_confidence": 0.99,
                "ocr_text_sha256": "b" * 64,
            },
            page_exclusion={"disposition": "frontmatter", "reason": "synthetic frontmatter"},
        )

        self.assertEqual(canonical["text_source"], "empty")
        self.assertEqual(canonical["ocr_status"], "skipped")
        self.assertEqual(canonical["ocr_skip_reason"], "synthetic frontmatter")
        self.assertIsNone(canonical["ocr_engine"])

    def test_chapter_paths_follow_outline_levels(self):
        outline = {
            "entries": [
                {"level": 1, "title": "第一章", "pdf_page": 2, "status": "valid"},
                {"level": 2, "title": "第一节", "pdf_page": 3, "status": "valid"},
                {"level": 1, "title": "第二章", "pdf_page": 5, "status": "valid"},
                {"level": 1, "title": "坏书签", "pdf_page": None, "status": "invalid_page"},
            ]
        }

        paths = build_chapter_paths(outline, 5)

        self.assertEqual(paths[1], [])
        self.assertEqual(paths[2], ["第一章"])
        self.assertEqual(paths[4], ["第一章", "第一节"])
        self.assertEqual(paths[5], ["第二章"])

    def test_process_book_writes_stable_sentences_and_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "legal_ai"
            pdf_path = root / "book" / "sample.pdf"
            pdf_path.parent.mkdir()
            pdf_path.write_bytes(b"%PDF synthetic placeholder")

            raw_pages = project_root / "data" / "raw" / "pages"
            raw_outlines = project_root / "data" / "raw" / "outlines"
            raw_pages.mkdir(parents=True)
            raw_outlines.mkdir(parents=True)
            pages = [
                {
                    "page_id": "synthetic-book:p0001",
                    "book_id": "synthetic-book",
                    "pdf_page": 1,
                    "raw_text": "第一章 总则\n规则一。规则二。",
                    "raw_text_sha256": "x1",
                    "extraction_error": None,
                    "quality_flags": [],
                    "pipeline_version": "p1-v1",
                    "source_pdf_sha256": "source",
                },
                {
                    "page_id": "synthetic-book:p0002",
                    "book_id": "synthetic-book",
                    "pdf_page": 2,
                    "raw_text": "第二页文本：\n他说：“原则有效。”",
                    "raw_text_sha256": "x2",
                    "extraction_error": None,
                    "quality_flags": [],
                    "pipeline_version": "p1-v1",
                    "source_pdf_sha256": "source",
                },
            ]
            (raw_pages / "synthetic-book.jsonl").write_text(
                "".join(
                    json.dumps(page, ensure_ascii=False) + "\n"
                    for page in pages
                ),
                encoding="utf-8",
            )
            (raw_outlines / "synthetic-book.json").write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "level": 1,
                                "title": "第一章",
                                "pdf_page": 1,
                                "status": "valid",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            book = sample_book("book/sample.pdf", 2)
            record = InventoryRecord(
                book=book,
                absolute_path=pdf_path,
                sha256=file_sha256(pdf_path),
                actual_pdf_pages=2,
            )

            first = process_book(record, project_root)
            sentence_path = (
                project_root
                / "data"
                / "processed"
                / "sentences"
                / "synthetic-book.jsonl"
            )
            chunk_path = (
                project_root
                / "data"
                / "processed"
                / "chunks"
                / "synthetic-book.jsonl"
            )
            page_path = (
                project_root
                / "data"
                / "processed"
                / "pages"
                / "synthetic-book.jsonl"
            )
            first_sentences = [
                json.loads(line)
                for line in sentence_path.read_text(encoding="utf-8").splitlines()
            ]
            first_chunks = [
                json.loads(line)
                for line in chunk_path.read_text(encoding="utf-8").splitlines()
            ]
            processed_pages = [
                json.loads(line)
                for line in page_path.read_text(encoding="utf-8").splitlines()
            ]
            second = process_book(record, project_root)
            second_sentences = [
                json.loads(line)
                for line in sentence_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(first, second)
            self.assertEqual(first_sentences, second_sentences)
            self.assertGreaterEqual(first["sentence_count"], 4)
            self.assertTrue(
                all(
                    page["printed_page"] is None
                    and page["printed_page_status"] == "unknown"
                    for page in processed_pages
                )
            )
            sentence_ids = {item["sentence_id"] for item in first_sentences}
            for sentence in first_sentences:
                page = processed_pages[sentence["pdf_page"] - 1]
                text = page["normalized_text"]
                self.assertEqual(
                    text[sentence["start_offset"] : sentence["end_offset"]],
                    sentence["normalized_text"],
                )
            for chunk in first_chunks:
                self.assertEqual(chunk["book_id"], "synthetic-book")
                self.assertIn(chunk["first_sentence_id"], sentence_ids)
                self.assertIn(chunk["last_sentence_id"], sentence_ids)
                self.assertEqual(
                    len({sid.split(":s")[0] for sid in chunk["sentence_ids"]}),
                    1,
                )

    def test_low_quality_embedded_page_uses_validated_ocr_as_canonical_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "legal_ai"
            pdf_path = root / "book" / "sample.pdf"
            pdf_path.parent.mkdir()
            pdf_path.write_bytes(b"%PDF synthetic placeholder")
            source_hash = "a" * 64
            raw_pages = project_root / "data" / "raw" / "pages"
            raw_outlines = project_root / "data" / "raw" / "outlines"
            raw_pages.mkdir(parents=True)
            raw_outlines.mkdir(parents=True)
            raw_page = {
                "page_id": "synthetic-ocr:p0001",
                "book_id": "synthetic-ocr",
                "pdf_page": 1,
                "raw_text": "",
                "raw_text_sha256": hashlib.sha256(b"").hexdigest(),
                "extraction_error": None,
                "quality_flags": ["empty"],
                "pipeline_version": "p1-v1",
                "source_pdf_sha256": source_hash,
            }
            (raw_pages / "synthetic-ocr.jsonl").write_text(
                json.dumps(raw_page, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            (raw_outlines / "synthetic-ocr.json").write_text(
                json.dumps({"entries": []}, ensure_ascii=False), encoding="utf-8"
            )
            ocr_text = "合成标题\n合同成立应当区分要约与承诺。"
            write_ocr_pages(
                project_root / "data" / "raw" / "ocr" / "pages" / "synthetic-ocr.jsonl",
                [
                    {
                        "schema_version": "ocr-page-v1",
                        "page_id": "synthetic-ocr:p0001",
                        "book_id": "synthetic-ocr",
                        "pdf_page": 1,
                        "source_pdf_sha256": source_hash,
                        "ocr_status": "generated",
                        "ocr_engine": "fixture-ocr",
                        "ocr_engine_version": "0.0.0",
                        "ocr_model_id": "synthetic-zh-v1",
                        "render_dpi": 300,
                        "preprocess_profile": "zh_book_cpu_v1",
                        "ocr_text": ocr_text,
                        "ocr_text_sha256": hashlib.sha256(
                            ocr_text.encode("utf-8")
                        ).hexdigest(),
                        "ocr_avg_confidence": 0.95,
                        "blocks": [
                            {
                                "block_id": "synthetic-ocr:p0001:b0001",
                                "text": "合成标题",
                                "bbox": [1, 1, 100, 20],
                                "confidence": 0.99,
                                "reading_order": 1,
                                "role": "title",
                            },
                            {
                                "block_id": "synthetic-ocr:p0001:b0002",
                                "text": "合同成立应当区分要约与承诺。",
                                "bbox": [1, 30, 300, 55],
                                "confidence": 0.91,
                                "reading_order": 2,
                                "role": "body",
                            },
                        ],
                        "error": None,
                        "skip_reason": None,
                    }
                ],
                book_id="synthetic-ocr",
                source_pdf_sha256=source_hash,
                pdf_page_count=1,
            )
            book = BookConfig.from_dict(
                {
                    "book_id": "synthetic-ocr",
                    "title": "合成 OCR 书籍",
                    "authors": [],
                    "translators": [],
                    "language": "zh-CN",
                    "jurisdictions": ["测试"],
                    "subjects": ["测试"],
                    "source_type": "学理来源",
                    "authority_tier": "secondary",
                    "file_path": "book/sample.pdf",
                    "expected_pdf_pages": 1,
                    "text_layer_policy": "auto",
                    "ocr_policy": "low_quality_pages",
                    "ocr_profile": "zh_book_cpu_v1",
                    "enabled": True,
                }
            )
            record = InventoryRecord(
                book=book,
                absolute_path=pdf_path,
                sha256=source_hash,
                actual_pdf_pages=1,
            )

            report = process_book(record, project_root)
            page = json.loads(
                (project_root / "data" / "processed" / "pages" / "synthetic-ocr.jsonl")
                .read_text(encoding="utf-8")
                .strip()
            )

            self.assertEqual(report["text_source_counts"]["ocr"], 1)
            self.assertEqual(page["text_source"], "ocr")
            self.assertEqual(page["text_verification_status"], "ocr_unverified")
            self.assertEqual(page["ocr_status"], "generated")
            self.assertEqual(page["normalized_text"], ocr_text)


if __name__ == "__main__":
    unittest.main()

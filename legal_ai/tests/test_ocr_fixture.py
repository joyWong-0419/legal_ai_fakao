from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from legal_ai.domain import LegalAIError
from legal_ai.ocr import load_ocr_pages, write_ocr_pages

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ocr"
    / "contract-law-8e-pilot.jsonl"
)
SOURCE_SHA256 = "a" * 64

COMMON_FIELDS = {
    "schema_version",
    "page_id",
    "book_id",
    "pdf_page",
    "source_pdf_sha256",
    "ocr_status",
    "ocr_engine",
    "ocr_engine_version",
    "ocr_model_id",
    "render_dpi",
    "preprocess_profile",
    "ocr_text",
    "ocr_text_sha256",
    "ocr_avg_confidence",
    "blocks",
    "error",
    "skip_reason",
}
BLOCK_FIELDS = {
    "block_id",
    "text",
    "bbox",
    "confidence",
    "reading_order",
    "role",
}


class OCRFixtureTests(unittest.TestCase):
    def _rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_fixture_locks_the_v2_ocr_page_contract(self) -> None:
        rows = self._rows()

        self.assertEqual([row["pdf_page"] for row in rows], [12, 13, 14])
        self.assertEqual(
            [row["ocr_status"] for row in rows],
            ["generated", "failed", "skipped"],
        )
        for row in rows:
            self.assertTrue(COMMON_FIELDS.issubset(row))
            self.assertEqual(row["schema_version"], "ocr-page-v1")
            self.assertEqual(row["book_id"], "contract-law-8e")
            self.assertEqual(
                row["page_id"],
                f"contract-law-8e:p{int(row['pdf_page']):04d}",
            )
            self.assertEqual(row["source_pdf_sha256"], SOURCE_SHA256)
            self.assertRegex(str(row["source_pdf_sha256"]), r"^[0-9a-f]{64}$")
            self.assertEqual(row["render_dpi"], 300)
            self.assertEqual(row["preprocess_profile"], "zh_book_cpu_v1")

    def test_fixture_is_accepted_by_the_production_contract_reader(self) -> None:
        rows = load_ocr_pages(
            FIXTURE_PATH,
            book_id="contract-law-8e",
            source_pdf_sha256=SOURCE_SHA256,
            pdf_page_count=20,
        )
        self.assertEqual(sorted(rows), [12, 13, 14])
        self.assertEqual(rows[12]["ocr_status"], "generated")

    def test_writer_rejects_a_source_hash_mismatch_without_creating_output(self) -> None:
        row = self._rows()[0]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ocr.jsonl"
            with self.assertRaisesRegex(LegalAIError, "SHA-256 不一致"):
                write_ocr_pages(
                    target,
                    [row],
                    book_id="contract-law-8e",
                    source_pdf_sha256="b" * 64,
                    pdf_page_count=20,
                )
            self.assertFalse(target.exists())

    def test_generated_page_has_auditable_blocks_and_hash(self) -> None:
        row = self._rows()[0]
        blocks = row["blocks"]
        self.assertIsInstance(blocks, list)
        self.assertEqual(
            "\n".join(str(block["text"]) for block in blocks),
            row["ocr_text"],
        )
        self.assertEqual(
            [block["reading_order"] for block in blocks], [1, 2]
        )
        self.assertEqual(
            hashlib.sha256(str(row["ocr_text"]).encode("utf-8")).hexdigest(),
            row["ocr_text_sha256"],
        )
        self.assertGreater(float(row["ocr_avg_confidence"]), 0.0)
        self.assertLessEqual(float(row["ocr_avg_confidence"]), 1.0)
        for position, block in enumerate(blocks, start=1):
            self.assertTrue(BLOCK_FIELDS.issubset(block))
            self.assertEqual(
                block["block_id"],
                f"contract-law-8e:p0012:b{position:04d}",
            )
            bbox = block["bbox"]
            self.assertEqual(len(bbox), 4)
            self.assertLess(int(bbox[0]), int(bbox[2]))
            self.assertLess(int(bbox[1]), int(bbox[3]))
            self.assertGreater(float(block["confidence"]), 0.0)
            self.assertLessEqual(float(block["confidence"]), 1.0)

    def test_failed_and_skipped_pages_cannot_look_like_empty_successes(self) -> None:
        failed, skipped = self._rows()[1:]
        for row in (failed, skipped):
            self.assertIsNone(row["ocr_text"])
            self.assertIsNone(row["ocr_text_sha256"])
            self.assertIsNone(row["ocr_avg_confidence"])
            self.assertEqual(row["blocks"], [])
        self.assertEqual(failed["error"], {"code": "render_timeout", "message": "synthetic fixture failure"})
        self.assertIsNone(failed["skip_reason"])
        self.assertIsNone(skipped["error"])
        self.assertEqual(skipped["skip_reason"], "frontmatter")
        self.assertIsNone(skipped["ocr_engine"])


if __name__ == "__main__":
    unittest.main()

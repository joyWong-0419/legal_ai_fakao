from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from legal_ai.domain import LegalAIError
from legal_ai.cli import run_ocr_pilot_command
from legal_ai.ocr_pilot import (
    _generated_row,
    order_blocks,
    render_cache_key,
    select_random_pages,
)
from legal_ai.domain import BookConfig, InventoryRecord


RUNTIME = {
    "renderer": "pymupdf",
    "renderer_version": "1.0.0",
    "ocr_engine": "rapidocr",
    "ocr_engine_version": "3.0.0",
    "ocr_model_id": "rapidocr-default-small",
    "onnxruntime_version": "1.0.0",
}


class OCRPilotTests(unittest.TestCase):
    def _record(self) -> InventoryRecord:
        return InventoryRecord(
            book=BookConfig(
                book_id="contract-law-8e",
                title="合同法",
                subtitle=None,
                authors=("崔建远",),
                translators=(),
                edition="第八版",
                volume=None,
                language="zh-CN",
                jurisdictions=("中国",),
                subjects=("民法",),
                source_type="academic",
                authority_tier="secondary",
                file_path="book/contract.pdf",
                expected_pdf_pages=1,
                text_layer_policy="auto",
                ocr_policy="low_quality_pages",
                ocr_profile="zh_book_cpu_v1",
                extract_mode=None,
                enabled=False,
            ),
            absolute_path=Path("contract.pdf"),
            sha256="a" * 64,
            actual_pdf_pages=1,
        )

    def test_random_selection_is_deterministic_and_in_range(self) -> None:
        first = select_random_pages(pdf_page_count=518, count=3, seed=20260811)
        second = select_random_pages(pdf_page_count=518, count=3, seed=20260811)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertEqual(first, sorted(first))
        self.assertTrue(all(1 <= page <= 518 for page in first))

    def test_random_selection_rejects_an_impossible_sample(self) -> None:
        with self.assertRaises(LegalAIError):
            select_random_pages(pdf_page_count=2, count=3, seed=1)

    def test_cache_key_changes_when_any_material_input_changes(self) -> None:
        base = render_cache_key(
            source_pdf_sha256="a" * 64,
            pdf_page=12,
            runtime=RUNTIME,
            render_dpi=300,
            preprocess_profile="zh_book_cpu_v1",
        )
        changed = render_cache_key(
            source_pdf_sha256="a" * 64,
            pdf_page=13,
            runtime=RUNTIME,
            render_dpi=300,
            preprocess_profile="zh_book_cpu_v1",
        )
        self.assertNotEqual(base, changed)

    def test_order_blocks_uses_top_then_left_rule_and_discards_invalid_scores(self) -> None:
        blocks = order_blocks(
            [
                {"bbox": [[120, 50], [180, 50], [180, 70], [120, 70]], "text": "right", "confidence": 0.9},
                {"bbox": [[10, 10], [80, 10], [80, 30], [10, 30]], "text": "top", "confidence": 0.8},
                {"bbox": [[20, 50], [90, 50], [90, 70], [20, 70]], "text": "left", "confidence": 0.7},
                {"bbox": [[1, 80], [2, 80], [2, 90], [1, 90]], "text": "drop", "confidence": 0.0},
            ],
            page_id="contract-law-8e:p0012",
        )

        self.assertEqual([block["text"] for block in blocks], ["top", "left", "right"])
        self.assertEqual([block["reading_order"] for block in blocks], [1, 2, 3])

    def test_encoding_replacement_character_is_not_accepted_as_generated_ocr(self) -> None:
        row = _generated_row(
            record=self._record(),
            pdf_page=1,
            runtime=RUNTIME,
            dpi=300,
            profile="zh_book_cpu_v1",
            attempts=1,
            cache_key="b" * 64,
            image_sha256="c" * 64,
            raw_blocks=[
                {
                    "bbox": [[10, 10], [40, 10], [40, 30], [10, 30]],
                    "text": "合同\ufffd",
                    "confidence": 0.9,
                }
            ],
        )
        self.assertEqual(row["ocr_status"], "failed")
        self.assertEqual(row["error"]["code"], "ocr_text_encoding_error")

    def test_report_path_outside_the_reports_directory_is_rejected(self) -> None:
        fake_report = {
            "book": {"source_pdf_unchanged": True},
            "summary": {},
            "raw_ocr_jsonl": "data/raw/ocr/pages/contract-law-8e.jsonl",
        }
        with patch("legal_ai.cli.load_books", return_value=[self._record().book]), patch(
            "legal_ai.inventory.inspect_book", return_value=self._record()
        ), patch("legal_ai.cli.run_ocr_pilot", return_value=fake_report), self.assertRaisesRegex(
            LegalAIError, "只能写入"
        ):
            run_ocr_pilot_command(
                book_id="contract-law-8e",
                pages=[1],
                random_count=None,
                seed=20260811,
                dpi=300,
                timeout_seconds=90,
                max_attempts=2,
                force=False,
                report_path=Path("outside.json"),
                config_path=Path("ignored.json"),
            )


if __name__ == "__main__":
    unittest.main()

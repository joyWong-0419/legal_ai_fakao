from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from legal_ai.domain import BookConfig, InventoryRecord
from legal_ai.inventory import file_sha256
from legal_ai.ocr import load_ocr_pages
from legal_ai.ocr_import import import_pp_ocrv6_json


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ocr"
    / "pp-ocrv6-external-synthetic.json"
)


class ExternalOCRImportTests(unittest.TestCase):
    def _record(self) -> InventoryRecord:
        return InventoryRecord(
            book=BookConfig(
                book_id="yang-civil-general-2022",
                title="民法总论",
                subtitle=None,
                authors=("杨代雄",),
                translators=(),
                edition=None,
                volume=None,
                language="zh-CN",
                jurisdictions=("中国",),
                subjects=("民法", "民法总论"),
                source_type="学理来源",
                authority_tier="secondary",
                file_path="book/civil-general.pdf",
                expected_pdf_pages=3,
                text_layer_policy="ignore",
                ocr_policy="all_pages",
                ocr_profile="external_pp_ocrv6_json_v1",
                extract_mode=None,
                enabled=True,
            ),
            absolute_path=FIXTURE_PATH,
            sha256=file_sha256(FIXTURE_PATH),
            actual_pdf_pages=3,
        )

    def test_import_maps_generated_empty_and_invalid_external_pages(self) -> None:
        record = self._record()
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            report = import_pp_ocrv6_json(
                record=record,
                project_root=project_root,
                input_path=FIXTURE_PATH,
            )
            rows = load_ocr_pages(
                project_root / "data" / "raw" / "ocr" / "pages" / "yang-civil-general-2022.jsonl",
                book_id=record.book.book_id,
                source_pdf_sha256=record.sha256,
                pdf_page_count=3,
            )

        self.assertEqual([rows[page]["ocr_status"] for page in sorted(rows)], ["generated", "skipped", "failed"])
        self.assertIsNone(rows[1]["render_dpi"])
        self.assertEqual(rows[1]["ocr_engine"], "paddleocr")
        self.assertEqual(rows[2]["skip_reason"], "external_ocr_empty_page")
        self.assertEqual(rows[3]["error"]["code"], "external_ocr_no_valid_blocks")
        self.assertEqual(report["summary"]["discarded_invalid_block_count"], 1)
        self.assertEqual(report["summary"]["empty_source_page_count"], 1)
        self.assertTrue(report["book"]["source_pdf_unchanged"])

    def test_registered_exclusion_overrides_an_invalid_external_page(self) -> None:
        record = self._record()
        with tempfile.TemporaryDirectory() as directory:
            report = import_pp_ocrv6_json(
                record=record,
                project_root=Path(directory),
                input_path=FIXTURE_PATH,
                exclusions={3: {"disposition": "blank", "reason": "synthetic blank"}},
            )

        self.assertEqual(report["summary"]["failed_pages"], 0)
        self.assertEqual(report["registered_excluded_pages"], [3])


if __name__ == "__main__":
    unittest.main()

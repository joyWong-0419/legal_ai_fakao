from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from legal_ai.cli import main


class CliTests(unittest.TestCase):
    def test_inventory_returns_nonzero_for_missing_book(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "books.json"
            config.write_text(
                json.dumps(
                    [
                        {
                            "book_id": "missing-book",
                            "title": "不存在的测试书",
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
                            "file_path": "book/definitely-missing.pdf",
                            "expected_pdf_pages": 1,
                            "extract_mode": "embedded_text",
                            "enabled": True,
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    ["inventory", "--config", str(config)]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("文件不存在", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

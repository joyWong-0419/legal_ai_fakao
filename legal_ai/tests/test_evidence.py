from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from legal_ai.cli import main
from legal_ai.domain import LegalAIError
from legal_ai.evidence import (
    MAX_QUOTE_CHARS,
    build_evidence_package,
    render_evidence_markdown,
)
from legal_ai.index import build_index
from legal_ai.tests.test_index_search import add_book_files, synthetic_book


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_root = self.root / "legal_ai"
        self.book = synthetic_book(
            book_id="criminal-book",
            title="合成刑法",
            jurisdiction="中国",
            subject="刑法",
        )
        self.record = add_book_files(
            self.project_root,
            self.book,
            [
                "正当防卫保护合法权益。",
                "正当防卫的必要限度需要具体判断。",
                "共同责任应当具有法律根据。",
            ],
            printed_page="10",
            printed_status="detected",
        )
        maps_path = self.project_root / "config" / "page_maps.json"
        maps_path.parent.mkdir(parents=True, exist_ok=True)
        maps_path.write_text(
            json.dumps(
                [
                    {
                        "book_id": "criminal-book",
                        "segments": [
                            {
                                "pdf_start": 1,
                                "pdf_end": 1,
                                "printed_start": 10,
                                "label_type": "arabic",
                                "status": "detected",
                            }
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.database_path = (
            self.project_root / "data" / "index" / "test.sqlite"
        )
        build_index(
            records=[self.record],
            project_root=self.project_root,
            database_path=self.database_path,
            rebuild=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _package(self, query: str = "正当防卫", **kwargs):
        return build_evidence_package(
            database_path=self.database_path,
            project_root=self.project_root,
            query=query,
            limit=2,
            **kwargs,
        )

    def test_quote_and_location_are_requeried_from_sentence_records(self) -> None:
        package = self._package()
        self.assertEqual(package["evidence_count"], 2)
        item = package["evidence"][0]
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT normalized_text FROM sentences "
                f"WHERE sentence_id IN ({','.join('?' for _ in item['sentence_ids'])}) "
                "ORDER BY seq",
                item["sentence_ids"],
            ).fetchall()
        finally:
            connection.close()
        expected_quote = "\n".join(row[0] for row in rows)
        self.assertEqual(item["quote"], expected_quote)
        self.assertEqual(
            item["quote_sha256"],
            hashlib.sha256(expected_quote.encode("utf-8")).hexdigest(),
        )
        self.assertLessEqual(item["quote_char_count"], MAX_QUOTE_CHARS)
        self.assertEqual(item["pdf_page"], 1)
        self.assertEqual(item["printed_page"], "10")
        self.assertEqual(item["printed_page_status"], "detected")
        self.assertTrue(
            self.project_root
            .joinpath(*Path(package["saved_package"]).parts[1:])
            .exists()
        )

    def test_search_display_fields_cannot_forge_evidence_source(self) -> None:
        fake = {
            "query": "正当防卫",
            "query_terms": ["正当防卫"],
            "run_id": "fake-run",
            "filters": {"book_ids": [], "jurisdictions": [], "subjects": []},
            "corpus_scope": ["criminal-book"],
            "coverage_notice": "测试覆盖声明",
            "results": [
                {
                    "chunk_id": "criminal-book:p0001:c001",
                    "title": "伪造书名",
                    "pdf_page": 999,
                    "printed_page": "999",
                    "scores": {
                        "fts": 1.0,
                        "exact": 1.0,
                        "chapter": 0.0,
                        "metadata": 0.0,
                        "final": 1.0,
                    },
                }
            ],
        }
        with patch("legal_ai.evidence.search_index", return_value=fake):
            package = self._package(persist=False)
        item = package["evidence"][0]
        self.assertEqual(item["title"], "合成刑法")
        self.assertEqual(item["pdf_page"], 1)
        self.assertEqual(item["printed_page"], "10")
        self.assertNotIn("伪造", item["quote"])

    def test_metadata_only_hit_does_not_become_a_direct_quote(self) -> None:
        fake = {
            "query": "只在元数据中的词",
            "query_terms": ["只在元数据中的词"],
            "run_id": "metadata-run",
            "filters": {"book_ids": [], "jurisdictions": [], "subjects": []},
            "corpus_scope": ["criminal-book"],
            "coverage_notice": "测试覆盖声明",
            "results": [
                {
                    "chunk_id": "criminal-book:p0001:c001",
                    "scores": {
                        "fts": 0.0,
                        "exact": 0.0,
                        "chapter": 0.0,
                        "metadata": 1.0,
                        "final": 0.1,
                    },
                }
            ],
        }
        with patch("legal_ai.evidence.search_index", return_value=fake):
            package = self._package(query=fake["query"], persist=False)
        self.assertEqual(package["evidence"], [])
        self.assertTrue(any("元数据" in gap for gap in package["gaps"]))

    def test_complete_sentence_is_not_truncated_to_fit_quote_limit(self) -> None:
        long_text = "正当防卫" + "甲" * MAX_QUOTE_CHARS
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE sentences SET normalized_text = ? WHERE sentence_id = ?",
                (long_text, "criminal-book:p0001:s001"),
            )
            connection.commit()
        finally:
            connection.close()
        fake = {
            "query": "正当防卫",
            "query_terms": ["正当防卫"],
            "run_id": "long-run",
            "filters": {"book_ids": [], "jurisdictions": [], "subjects": []},
            "corpus_scope": ["criminal-book"],
            "coverage_notice": "测试覆盖声明",
            "results": [
                {
                    "chunk_id": "criminal-book:p0001:c001",
                    "scores": {
                        "fts": 1.0,
                        "exact": 1.0,
                        "chapter": 0.0,
                        "metadata": 0.0,
                        "final": 1.0,
                    },
                }
            ],
        }
        with patch("legal_ai.evidence.search_index", return_value=fake):
            package = self._package(persist=False)
        self.assertEqual(package["evidence"], [])
        self.assertTrue(any("长句" in gap for gap in package["gaps"]))

    def test_markdown_labels_detected_page_as_unverified(self) -> None:
        markdown = render_evidence_markdown(self._package())
        self.assertIn("印刷页候选 10（未核验）", markdown)
        self.assertNotIn("印刷页 10（已核验）", markdown)
        self.assertIn("学理来源", markdown)

    def test_unverified_ocr_quote_is_explicitly_labeled(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                UPDATE pages
                SET text_source = 'ocr',
                    text_verification_status = 'ocr_unverified',
                    ocr_status = 'generated',
                    ocr_engine = 'fixture-ocr',
                    ocr_avg_confidence = 0.91,
                    ocr_text_sha256 = ?
                """,
                ("a" * 64,),
            )
            connection.commit()
        finally:
            connection.close()
        package = self._package(persist=False)
        item = package["evidence"][0]
        self.assertEqual(item["inference_level"], "ocr_unverified_quote")
        self.assertEqual(item["text_source"], "ocr")
        self.assertIn("OCR 转写", "\n".join(package["gaps"]))
        self.assertIn("尚未人工对照", render_evidence_markdown(package))

    def test_stale_verified_index_is_blocked_after_page_map_demotion(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE pages SET printed_page_status = 'verified'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(LegalAIError, "page_maps.json 不一致"):
            self._package(persist=False)

    def test_invalid_page_status_value_pair_is_blocked(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("UPDATE pages SET printed_page = NULL")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(LegalAIError, "四态状态不相容"):
            self._package(persist=False)

    def test_context_cli_json_is_machine_readable(self) -> None:
        stdout = io.StringIO()
        with patch("legal_ai.cli.PROJECT_ROOT", self.project_root):
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "context",
                        "正当防卫",
                        "--limit",
                        "1",
                        "--format",
                        "json",
                        "--db",
                        str(self.database_path),
                    ]
                )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["evidence_count"], 1)
        self.assertEqual(payload["evidence"][0]["inference_level"], "direct_quote")

    def test_context_api_does_not_accept_caller_supplied_quote_or_page(self) -> None:
        parameters = inspect.signature(build_evidence_package).parameters
        self.assertNotIn("quote", parameters)
        self.assertNotIn("pdf_page", parameters)
        self.assertNotIn("sentence_ids", parameters)

    def test_failed_pre_persist_verification_leaves_no_package(self) -> None:
        evidence_dir = self.project_root / "data" / "evidence"
        before = set(evidence_dir.glob("*.json")) if evidence_dir.exists() else set()
        with patch(
            "legal_ai.verify.verify_evidence_package",
            return_value=[{"code": "forced_failure", "entity": "test"}],
        ):
            with self.assertRaisesRegex(LegalAIError, "落盘前自检未通过"):
                self._package()
        after = set(evidence_dir.glob("*.json")) if evidence_dir.exists() else set()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

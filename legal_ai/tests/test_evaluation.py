from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from legal_ai.cli import main
from legal_ai.domain import LegalAIError
from legal_ai.evaluation import (
    evaluate_dataset,
    validate_golden_dataset,
)
from legal_ai.index import build_index
from legal_ai.tests.test_index_search import add_book_files, synthetic_book


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_root = self.root / "legal_ai"
        book = synthetic_book(
            book_id="criminal-book",
            title="合成刑法",
            jurisdiction="中国",
            subject="刑法",
        )
        record = add_book_files(
            self.project_root,
            book,
            ["正当防卫保护合法权益。", "必要限度需要结合危险判断。"],
        )
        self.database_path = (
            self.project_root / "data" / "index" / "test.sqlite"
        )
        build_index(
            records=[record],
            project_root=self.project_root,
            database_path=self.database_path,
            rebuild=True,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE books SET sha256 = ?", ("a" * 64,)
            )
            connection.commit()
            sentence = connection.execute(
                "SELECT sentence_id, text_sha256 FROM sentences ORDER BY seq LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.dataset = {
            "schema_version": "golden-v1",
            "dataset_id": "synthetic-gold",
            "frozen_at": "2026-07-11T00:00:00+00:00",
            "ranking_version": "rank-v1",
            "questions": [
                {
                    "question_id": "synthetic-001",
                    "question": "正当防卫的基本含义是什么？",
                    "search_query": "正当防卫",
                    "category": "single_book",
                    "coverage_tags": ["synthetic_retrieval"],
                    "filters": {
                        "book_ids": ["criminal-book"],
                        "jurisdictions": [],
                        "subjects": [],
                    },
                    "expected_book_ids": ["criminal-book"],
                    "candidate_scopes": [
                        {
                            "book_id": "criminal-book",
                            "pdf_page_range": [1, 1],
                            "chapter_path": ["第一章 合成规则"],
                            "basis": ["source_inspection"],
                        }
                    ],
                    "judgments": [
                        {
                            "page_id": "criminal-book:p0001",
                            "book_id": "criminal-book",
                            "relevance": 2,
                            "supporting_sentence_ids": [sentence[0]],
                            "supporting_sentence_sha256": [sentence[1]],
                            "label_method": "source_inspection",
                            "review_status": "verified",
                            "reviewed_at": "2026-07-11T00:00:00+00:00",
                            "reason_code": "direct_support",
                        }
                    ],
                }
            ],
        }
        self.golden_path = self.project_root / "config" / "golden.json"
        self.golden_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_dataset(self.dataset)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_dataset(self, dataset) -> None:
        self.golden_path.write_text(
            json.dumps(dataset, ensure_ascii=False), encoding="utf-8"
        )

    def test_page_dedup_and_no_independent_pool_stays_incomplete(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            before = connection.execute(
                "SELECT COUNT(*) FROM retrieval_runs"
            ).fetchone()[0]
        finally:
            connection.close()
        report = evaluate_dataset(
            database_path=self.database_path, golden_path=self.golden_path
        )
        connection = sqlite3.connect(self.database_path)
        try:
            after = connection.execute(
                "SELECT COUNT(*) FROM retrieval_runs"
            ).fetchone()[0]
        finally:
            connection.close()
        question = report["questions"][0]
        self.assertFalse(report["complete"])
        self.assertFalse(report["passed"])
        self.assertEqual(question["unique_page_result_count"], 1)
        self.assertIsNone(question["metrics"]["recall_at_10"])
        self.assertIsNone(question["metrics"]["precision_at_5"])
        self.assertEqual(
            question["metrics"]["known_positive_precision_lower_bound"], 0.2
        )
        self.assertIn(
            "no_source_scoped_positive_qrel_outside_top_five",
            question["incomplete_reasons"],
        )
        self.assertEqual(before, after)
        self.assertNotIn(
            "正当防卫保护合法权益",
            json.dumps(report, ensure_ascii=False),
        )

    def test_unjudged_top_page_makes_metrics_incomplete(self) -> None:
        dataset = json.loads(json.dumps(self.dataset, ensure_ascii=False))
        dataset["questions"][0]["judgments"] = []
        report = evaluate_dataset(
            database_path=self.database_path, dataset=dataset
        )
        self.assertFalse(report["complete"])
        self.assertIsNone(report["metrics"]["macro"]["recall_at_10"])
        self.assertIn(
            "unjudged_top_five_pages",
            report["questions"][0]["incomplete_reasons"],
        )

    def test_tampered_supporting_sentence_hash_is_rejected(self) -> None:
        dataset = json.loads(json.dumps(self.dataset, ensure_ascii=False))
        dataset["questions"][0]["judgments"][0][
            "supporting_sentence_sha256"
        ] = ["0" * 64]
        with self.assertRaisesRegex(LegalAIError, "SHA-256"):
            validate_golden_dataset(dataset, self.database_path)

    def test_pool_scope_duplicate_query_and_rank_origin_contracts(self) -> None:
        empty_scope = json.loads(json.dumps(self.dataset, ensure_ascii=False))
        empty_scope["questions"][0]["candidate_scopes"] = []
        with self.assertRaisesRegex(LegalAIError, "非空数组"):
            validate_golden_dataset(empty_scope, self.database_path)

        duplicate = json.loads(json.dumps(self.dataset, ensure_ascii=False))
        second = json.loads(
            json.dumps(duplicate["questions"][0], ensure_ascii=False)
        )
        second["question_id"] = "synthetic-002"
        second["question"] = "另一个表述"
        duplicate["questions"].append(second)
        with self.assertRaisesRegex(LegalAIError, "search_query 必须全局唯一"):
            validate_golden_dataset(duplicate, self.database_path)

        other_rank = json.loads(json.dumps(self.dataset, ensure_ascii=False))
        other_rank["ranking_version"] = "rank-v0-pool-origin"
        normalized = validate_golden_dataset(other_rank, self.database_path)
        self.assertEqual(normalized["ranking_version"], "rank-v0-pool-origin")

    def test_evaluate_cli_json_and_incomplete_exit_code(self) -> None:
        stdout = io.StringIO()
        with patch("legal_ai.cli.PROJECT_ROOT", self.project_root):
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "evaluate",
                        "--format",
                        "json",
                        "--golden",
                        str(self.golden_path),
                        "--db",
                        str(self.database_path),
                    ]
                )
        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["complete"])
        self.assertFalse(payload["passed"])
        self.assertTrue(
            (self.project_root / "reports" / "retrieval_evaluation.json").is_file()
        )


if __name__ == "__main__":
    unittest.main()

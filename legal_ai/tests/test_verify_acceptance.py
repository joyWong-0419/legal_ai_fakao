from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from legal_ai.acceptance import (
    build_citation_acceptance,
    render_citation_acceptance_markdown,
)
from legal_ai.cli import main
from legal_ai.domain import LegalAIError
from legal_ai.evidence import build_evidence_package
from legal_ai.index import build_index
from legal_ai.inventory import file_sha256
from legal_ai.tests.test_index_search import (
    add_book_files,
    synthetic_book,
    write_jsonl,
)
from legal_ai.verify import verify_corpus, verify_evidence_package


class VerifyAcceptanceTests(unittest.TestCase):
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

        pdf_path = self.root / self.book.file_path
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with pdf_path.open("wb") as handle:
            writer.write(handle)

        record = add_book_files(
            self.project_root,
            self.book,
            ["正当防卫保护合法权益。", "共同责任应当具有法律根据。"],
            printed_page="10",
            printed_status="detected",
        )
        digest = file_sha256(pdf_path)
        raw_path = (
            self.project_root / "data" / "raw" / "pages" / "criminal-book.jsonl"
        )
        raw_row = json.loads(raw_path.read_text(encoding="utf-8").strip())
        raw_row["source_pdf_sha256"] = digest
        write_jsonl(raw_path, [raw_row])
        self.record = replace(
            record, absolute_path=pdf_path, sha256=digest, actual_pdf_pages=1
        )

        config_value = asdict(self.book)
        for field in ("authors", "translators", "jurisdictions", "subjects"):
            config_value[field] = list(config_value[field])
        self.config_path = self.project_root / "config" / "books.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps([config_value], ensure_ascii=False), encoding="utf-8"
        )

        self.maps_path = self.project_root / "config" / "page_maps.json"
        self.maps_path.write_text(
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
        self.audit_path = self.project_root / "reports" / "page_map_audit.json"
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.write_text(
            json.dumps(
                {
                    "book_count": 1,
                    "sample_policy": {
                        "per_book": 1,
                        "seed": 20260710,
                        "deterministic": True,
                    },
                    "books": [
                        {
                            "book_id": "criminal-book",
                            "acceptance_samples": [
                                {
                                    "pdf_page": 1,
                                    "candidate_printed_page": "10",
                                    "status": "detected",
                                    "obsidian_pdf_link": "[[forged#page=999]]",
                                    "snapshot_path": (
                                        "legal_ai/reports/private/"
                                        "page_map_acceptance/criminal-book/pdf-0001.png"
                                    ),
                                    "snapshot_exists": False,
                                }
                            ],
                        }
                    ],
                },
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

    def _verify(self):
        return verify_corpus(
            database_path=self.database_path,
            project_root=self.project_root,
            config_path=self.config_path,
            maps_path=self.maps_path,
        )

    def test_clean_full_verification_and_json_cli(self) -> None:
        report = self._verify()
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["checks"]["sentences_checked"], 2)
        self.assertEqual(report["checks"]["chunks_checked"], 2)
        self.assertEqual(report["checks"]["pdf_files_checked"], 1)

        stdout = io.StringIO()
        with patch("legal_ai.cli.PROJECT_ROOT", self.project_root):
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "verify",
                        "--format",
                        "json",
                        "--config",
                        str(self.config_path),
                        "--maps",
                        str(self.maps_path),
                        "--db",
                        str(self.database_path),
                    ]
                )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(
            (self.project_root / "reports" / "verification.json").is_file()
        )

    def test_tampered_quote_is_detected_without_echoing_text(self) -> None:
        package = build_evidence_package(
            database_path=self.database_path,
            project_root=self.project_root,
            query="正当防卫",
            limit=1,
        )
        package["evidence"][0]["quote"] = "伪造的长篇证据正文绝不能进入报告"
        package["evidence"][0]["quote_sha256"] = hashlib.sha256(
            package["evidence"][0]["quote"].encode("utf-8")
        ).hexdigest()
        connection = sqlite3.connect(self.database_path)
        try:
            issues = verify_evidence_package(connection, package)
        finally:
            connection.close()
        codes = {issue["code"] for issue in issues}
        self.assertIn("evidence_quote_mismatch", codes)
        serialized = json.dumps(issues, ensure_ascii=False)
        self.assertNotIn("伪造的长篇证据正文", serialized)
        self.assertNotIn("正当防卫保护合法权益", serialized)

    def test_sentence_hash_corruption_fails_full_verification(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("UPDATE sentences SET text_sha256 = '0'")
            connection.commit()
        finally:
            connection.close()
        report = self._verify()
        self.assertFalse(report["ok"])
        self.assertIn(
            "sentence_text_hash_mismatch",
            {issue["code"] for issue in report["issues"]},
        )

    def test_historical_detected_package_survives_safe_promotion_only(self) -> None:
        package = build_evidence_package(
            database_path=self.database_path,
            project_root=self.project_root,
            query="正当防卫",
            limit=1,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE pages SET printed_page_status = 'verified'"
            )
            connection.commit()
            self.assertEqual(verify_evidence_package(connection, package), [])

            unsafe = json.loads(json.dumps(package, ensure_ascii=False))
            unsafe["evidence"][0]["printed_page_status"] = "verified"
            connection.execute(
                "UPDATE pages SET printed_page_status = 'detected'"
            )
            connection.commit()
            codes = {
                issue["code"]
                for issue in verify_evidence_package(connection, unsafe)
            }
        finally:
            connection.close()
        self.assertIn("evidence_database_field_mismatch", codes)

    def test_acceptance_uses_db_link_hash_and_rejects_stale_audit(self) -> None:
        acceptance = build_citation_acceptance(
            database_path=self.database_path, audit_path=self.audit_path
        )
        sample = acceptance["books"][0]["samples"][0]
        self.assertEqual(acceptance["sample_count"], 1)
        self.assertIn("book/criminal-book.pdf#page=1", sample["obsidian_pdf_link"])
        self.assertNotIn("forged", sample["obsidian_pdf_link"])
        self.assertEqual(
            sample["quote_sha256"],
            hashlib.sha256(sample["quote"].encode("utf-8")).hexdigest(),
        )
        markdown = render_citation_acceptance_markdown(acceptance)
        self.assertIn("印刷页候选 10（未核验）", markdown)

        audit = json.loads(self.audit_path.read_text(encoding="utf-8"))
        audit["books"][0]["acceptance_samples"][0][
            "candidate_printed_page"
        ] = "999"
        self.audit_path.write_text(
            json.dumps(audit, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(LegalAIError, "审计报告已过期"):
            build_citation_acceptance(
                database_path=self.database_path, audit_path=self.audit_path
            )


if __name__ == "__main__":
    unittest.main()

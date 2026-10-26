from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from legal_ai.cli import main
from legal_ai.domain import BookConfig, InventoryRecord, LegalAIError
from legal_ai.index import build_index
from legal_ai.search import search_index


def synthetic_book(
    *,
    book_id: str,
    title: str,
    jurisdiction: str,
    subject: str,
) -> BookConfig:
    return BookConfig.from_dict(
        {
            "book_id": book_id,
            "title": title,
            "subtitle": None,
            "authors": ["测试作者"],
            "translators": [],
            "edition": None,
            "volume": None,
            "language": "zh-CN",
            "jurisdictions": [jurisdiction],
            "subjects": [subject],
            "source_type": "学理来源",
            "authority_tier": "secondary",
            "file_path": f"book/{book_id}.pdf",
            "expected_pdf_pages": 1,
            "extract_mode": "embedded_text",
            "enabled": True,
        }
    )


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def add_book_files(
    project_root: Path,
    book: BookConfig,
    chunks: list[str],
    *,
    printed_page: str | None = None,
    printed_status: str = "unknown",
) -> InventoryRecord:
    page_id = f"{book.book_id}:p0001"
    normalized_page = "\n".join(chunks)
    source_hash = f"sha-{book.book_id}"
    raw_hash = hashlib.sha256(normalized_page.encode("utf-8")).hexdigest()
    write_jsonl(
        project_root / "data" / "raw" / "pages" / f"{book.book_id}.jsonl",
        [
            {
                "page_id": page_id,
                "book_id": book.book_id,
                "pdf_page": 1,
                "raw_text": normalized_page,
                "raw_text_sha256": raw_hash,
                "quality_flags": [],
                "pipeline_version": "p1-v1",
                "source_pdf_sha256": source_hash,
            }
        ],
    )
    write_jsonl(
        project_root
        / "data"
        / "processed"
        / "pages"
        / f"{book.book_id}.jsonl",
        [
            {
                "page_id": page_id,
                "book_id": book.book_id,
                "pdf_page": 1,
                "printed_page": printed_page,
                "printed_page_status": printed_status,
                "chapter_path": ["第一章 合成规则"],
                "normalized_text": normalized_page,
                "char_count": len(normalized_page),
                "quality_flags": [],
                "normalization_version": "norm-v1",
                "text_source": "embedded_text",
                "text_verification_status": "embedded_text",
                "ocr_status": "not_required",
                "ocr_engine": None,
                "ocr_avg_confidence": None,
                "ocr_text_sha256": None,
                "pipeline_version": "p2-ocr-v1",
            }
        ],
    )
    sentences: list[dict] = []
    chunk_rows: list[dict] = []
    offset = 0
    for sequence, text in enumerate(chunks, start=1):
        sentence_id = f"{page_id}:s{sequence:03d}"
        chunk_id = f"{page_id}:c{sequence:03d}"
        sentences.append(
            {
                "sentence_id": sentence_id,
                "page_id": page_id,
                "book_id": book.book_id,
                "pdf_page": 1,
                "seq": sequence,
                "raw_text": text,
                "normalized_text": text,
                "start_offset": offset,
                "end_offset": offset + len(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "normalization_version": "norm-v1",
                "segmentation_version": "seg-v1",
            }
        )
        chunk_rows.append(
            {
                "chunk_id": chunk_id,
                "page_id": page_id,
                "book_id": book.book_id,
                "pdf_page": 1,
                "seq": sequence,
                "first_sentence_id": sentence_id,
                "last_sentence_id": sentence_id,
                "sentence_ids": [sentence_id],
                "text": text,
                "char_count": len(text),
                "chapter_path": ["第一章 合成规则"],
                "segmentation_version": "seg-v1",
            }
        )
        offset += len(text) + 1
    write_jsonl(
        project_root
        / "data"
        / "processed"
        / "sentences"
        / f"{book.book_id}.jsonl",
        sentences,
    )
    write_jsonl(
        project_root
        / "data"
        / "processed"
        / "chunks"
        / f"{book.book_id}.jsonl",
        chunk_rows,
    )
    return InventoryRecord(
        book=book,
        absolute_path=project_root.parent / book.file_path,
        sha256=source_hash,
        actual_pdf_pages=1,
    )


class IndexSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_root = self.root / "legal_ai"
        criminal = synthetic_book(
            book_id="criminal-book",
            title="合成刑法",
            jurisdiction="中国",
            subject="刑法",
        )
        civil = synthetic_book(
            book_id="civil-book",
            title="合成民法",
            jurisdiction="德国",
            subject="民法",
        )
        self.records = [
            add_book_files(
                self.project_root,
                criminal,
                [
                    "正当防卫保护合法权益。共同责任需要法律根据。",
                    "正当防卫的必要限度需要具体判断。共同责任应当区分。",
                    "正当防卫与共同责任属于不同问题。",
                ],
                printed_page="10",
                printed_status="detected",
            ),
            add_book_files(
                self.project_root,
                civil,
                ["合同自由受到诚信原则与强制规范的限制。"],
            ),
        ]
        self.database_path = self.project_root / "data" / "index" / "test.sqlite"
        self.report = build_index(
            records=self.records,
            project_root=self.project_root,
            database_path=self.database_path,
            rebuild=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_core_tables_and_fts(self):
        self.assertEqual(self.report["book_count"], 2)
        self.assertEqual(self.report["page_count"], 2)
        self.assertEqual(self.report["sentence_count"], 4)
        self.assertEqual(self.report["chunk_count"], 4)
        self.assertEqual(self.report["fts_row_count"], 4)
        connection = sqlite3.connect(self.database_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            for required in (
                "books",
                "pages",
                "sentences",
                "chunks",
                "retrieval_runs",
                "chunks_fts",
            ):
                self.assertIn(required, tables)
        finally:
            connection.close()

    def test_trigram_and_two_character_fallback(self):
        trigram = search_index(
            database_path=self.database_path,
            query="正当防卫",
            record_run=False,
        )
        short = search_index(
            database_path=self.database_path,
            query="合同",
            record_run=False,
        )
        self.assertGreater(trigram["result_count"], 0)
        self.assertEqual(trigram["results"][0]["book_id"], "criminal-book")
        self.assertGreater(short["result_count"], 0)
        self.assertEqual(short["results"][0]["book_id"], "civil-book")

    def test_filters_multiword_merge_and_page_cap(self):
        filtered = search_index(
            database_path=self.database_path,
            query="合同",
            jurisdictions=["德国"],
            subjects=["民法"],
            record_run=False,
        )
        book_filtered = search_index(
            database_path=self.database_path,
            query="正当防卫",
            book_ids=["criminal-book"],
            record_run=False,
        )
        merged = search_index(
            database_path=self.database_path,
            query="正当防卫 合同",
            limit=10,
            record_run=False,
        )
        same_page = search_index(
            database_path=self.database_path,
            query="正当防卫",
            limit=10,
            record_run=False,
        )
        self.assertTrue(filtered["results"])
        self.assertTrue(all(item["book_id"] == "civil-book" for item in filtered["results"]))
        self.assertTrue(book_filtered["results"])
        self.assertTrue(
            all(
                item["book_id"] == "criminal-book"
                for item in book_filtered["results"]
            )
        )
        self.assertEqual(
            {item["book_id"] for item in merged["results"]},
            {"criminal-book", "civil-book"},
        )
        page_counts: dict[str, int] = {}
        for item in same_page["results"]:
            page_counts[item["page_id"]] = page_counts.get(item["page_id"], 0) + 1
        self.assertLessEqual(max(page_counts.values()), 2)

    def test_rebuild_rejects_chunk_with_cross_page_sentence(self):
        chunk_path = (
            self.project_root
            / "data"
            / "processed"
            / "chunks"
            / "criminal-book.jsonl"
        )
        rows = [
            json.loads(line)
            for line in chunk_path.read_text(encoding="utf-8").splitlines()
        ]
        rows[0]["sentence_ids"] = ["civil-book:p0001:s001"]
        rows[0]["first_sentence_id"] = "civil-book:p0001:s001"
        rows[0]["last_sentence_id"] = "civil-book:p0001:s001"
        write_jsonl(chunk_path, rows)
        with self.assertRaisesRegex(LegalAIError, "不存在或跨 PDF 页"):
            build_index(
                records=self.records,
                project_root=self.project_root,
                database_path=self.database_path,
                rebuild=True,
            )
        preserved = search_index(
            database_path=self.database_path,
            query="正当防卫",
            record_run=False,
        )
        self.assertGreater(preserved["result_count"], 0)
        self.assertFalse(
            list(self.database_path.parent.glob(f".{self.database_path.name}.*.tmp"))
        )

    def test_rebuild_rejects_sentence_identity_and_offset_corruption(self):
        sentence_path = (
            self.project_root
            / "data"
            / "processed"
            / "sentences"
            / "criminal-book.jsonl"
        )
        rows = [
            json.loads(line)
            for line in sentence_path.read_text(encoding="utf-8").splitlines()
        ]
        rows[0]["book_id"] = "wrong-book"
        rows[0]["end_offset"] = 99999
        write_jsonl(sentence_path, rows)
        with self.assertRaisesRegex(
            LegalAIError, "book_id/pdf_page 不一致|offset"
        ):
            build_index(
                records=self.records,
                project_root=self.project_root,
                database_path=self.database_path,
                rebuild=True,
            )

    def test_rebuild_cannot_overwrite_pdf_or_path_outside_index_directory(self):
        protected = self.root / "book" / "protected.pdf"
        protected.parent.mkdir(parents=True, exist_ok=True)
        protected.write_bytes(b"original-pdf-bytes")
        with self.assertRaisesRegex(LegalAIError, "必须位于"):
            build_index(
                records=self.records,
                project_root=self.project_root,
                database_path=protected,
                rebuild=True,
            )
        self.assertEqual(protected.read_bytes(), b"original-pdf-bytes")

        bad_suffix = self.project_root / "data" / "index" / "notes.md"
        with self.assertRaisesRegex(LegalAIError, "扩展名"):
            build_index(
                records=self.records,
                project_root=self.project_root,
                database_path=bad_suffix,
                rebuild=True,
            )

    def test_special_characters_empty_and_long_queries_are_safe(self):
        for query in ("'", '"', "%", "_", "*", "(OR)", "a-b", "' OR 1=1 --"):
            response = search_index(
                database_path=self.database_path,
                query=query,
                record_run=False,
            )
            self.assertIn("results", response)
        with self.assertRaisesRegex(LegalAIError, "不能为空"):
            search_index(
                database_path=self.database_path,
                query="   ",
                record_run=False,
            )
        with self.assertRaisesRegex(LegalAIError, "过长"):
            search_index(
                database_path=self.database_path,
                query="法" * 201,
                record_run=False,
            )

    def test_rebuild_and_result_order_are_stable(self):
        before = search_index(
            database_path=self.database_path,
            query="正当防卫",
            record_run=False,
        )
        second_report = build_index(
            records=self.records,
            project_root=self.project_root,
            database_path=self.database_path,
            rebuild=True,
        )
        after = search_index(
            database_path=self.database_path,
            query="正当防卫",
            record_run=False,
        )
        self.assertEqual(
            (self.report["book_count"], self.report["chunk_count"]),
            (second_report["book_count"], second_report["chunk_count"]),
        )
        self.assertEqual(
            [item["chunk_id"] for item in before["results"]],
            [item["chunk_id"] for item in after["results"]],
        )

    def test_rebuild_preserves_retrieval_run_history(self):
        search_index(
            database_path=self.database_path,
            query="合同",
            record_run=True,
        )
        report = build_index(
            records=self.records,
            project_root=self.project_root,
            database_path=self.database_path,
            rebuild=True,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM retrieval_runs"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)
        self.assertEqual(report["retrieval_runs_preserved"], 1)

    def test_json_cli_stdout_is_parseable_and_run_is_recorded(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "search",
                    "合同",
                    "--format",
                    "json",
                    "--db",
                    str(self.database_path),
                ]
            )
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(parsed["query"], "合同")
        connection = sqlite3.connect(self.database_path)
        try:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM retrieval_runs"
            ).fetchone()[0]
            run_parameters = json.loads(
                connection.execute(
                    "SELECT filters_json FROM retrieval_runs"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        self.assertEqual(run_count, 1)
        self.assertEqual(run_parameters["limit"], 10)
        self.assertEqual(run_parameters["ranking_version"], "rank-v1")
        self.assertEqual(run_parameters["query_terms"], ["合同"])

    def test_stale_pipeline_and_missing_fts_are_rejected(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE index_metadata SET value='stale' "
                "WHERE key='pipeline_version'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(LegalAIError, "版本不匹配"):
            search_index(
                database_path=self.database_path,
                query="正当防卫",
                record_run=False,
            )

        build_index(
            records=self.records,
            project_root=self.project_root,
            database_path=self.database_path,
            rebuild=True,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DROP TABLE chunks_fts")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(LegalAIError, "SQLite 检索失败"):
            search_index(
                database_path=self.database_path,
                query="正当防卫",
                record_run=False,
            )

    def test_no_hit_still_reports_filtered_corpus_scope(self):
        response = search_index(
            database_path=self.database_path,
            query="绝不存在的合成术语",
            subjects=["民法"],
            record_run=False,
        )
        self.assertEqual(response["result_count"], 0)
        self.assertEqual(response["corpus_scope"], ["civil-book"])


if __name__ == "__main__":
    unittest.main()

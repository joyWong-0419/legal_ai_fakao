from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from legal_ai.cli import main
from legal_ai.domain import LegalAIError
from legal_ai.evidence import build_evidence_package
from legal_ai.index import build_index
from legal_ai.obsidian_export import export_query_to_obsidian, write_obsidian_note
from legal_ai.tests.test_index_search import add_book_files, synthetic_book


class ObsidianExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.temporary.name)
        self.project_root = self.vault_root / "legal_ai"
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
            records=[record],
            project_root=self.project_root,
            database_path=self.database_path,
            rebuild=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _export(self, query: str = "正当防卫", **kwargs):
        return export_query_to_obsidian(
            database_path=self.database_path,
            output_dir=Path("证据笔记"),
            query=query,
            project_root=self.project_root,
            vault_root=self.vault_root,
            limit=2,
            **kwargs,
        )

    def test_exports_required_sections_and_traceable_evidence(self) -> None:
        result = self._export()
        path = Path(result["output_path"])
        markdown = path.read_text(encoding="utf-8")
        self.assertTrue(path.is_file())
        self.assertFalse(result["current_law_verified"])
        for heading in (
            "## 问题",
            "## 定义",
            "## 证据",
            "## 引用索引",
            "## 覆盖缺口",
            "## 待核验现行法",
        ):
            self.assertIn(heading, markdown)
        self.assertIn("current_law_verified: false", markdown)
        self.assertIn("印刷页候选 10（未核验）", markdown)
        self.assertIn("sentence", markdown.casefold())
        self.assertIn("SHA-256", markdown)
        self.assertIn(
            "[[book/criminal-book.pdf#page=1|打开 PDF 第 1 页]]",
            markdown,
        )
        self.assertNotIn("#page=1\\|", markdown)

    def test_existing_personal_note_is_never_overwritten(self) -> None:
        first = self._export(filename="个人笔记.md")
        first_path = Path(first["output_path"])
        before = hashlib.sha256(first_path.read_bytes()).hexdigest()
        second = self._export(filename="个人笔记.md")
        second_path = Path(second["output_path"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(second_path.name, "个人笔记-2.md")
        self.assertEqual(
            hashlib.sha256(first_path.read_bytes()).hexdigest(), before
        )

    def test_unsafe_destination_is_rejected_before_retrieval_write(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            before_runs = connection.execute(
                "SELECT COUNT(*) FROM retrieval_runs"
            ).fetchone()[0]
        finally:
            connection.close()
        for output_dir in (
            Path("..") / "outside",
            Path(".obsidian") / "notes",
            Path("book") / "notes",
            Path("legal_ai") / "data" / "notes",
        ):
            with self.subTest(output_dir=output_dir):
                with self.assertRaises(LegalAIError):
                    export_query_to_obsidian(
                        database_path=self.database_path,
                        output_dir=output_dir,
                        query="正当防卫",
                        project_root=self.project_root,
                        vault_root=self.vault_root,
                    )
        connection = sqlite3.connect(self.database_path)
        try:
            after_runs = connection.execute(
                "SELECT COUNT(*) FROM retrieval_runs"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(after_runs, before_runs)

    def test_empty_evidence_still_creates_honest_draft(self) -> None:
        result = self._export(query="绝对不存在的术语组合")
        markdown = Path(result["output_path"]).read_text(encoding="utf-8")
        self.assertEqual(result["evidence_count"], 0)
        self.assertIn("未找到可由数据库句子记录直接支持的证据", markdown)
        self.assertIn("当前状态：未核验", markdown)

    def test_public_writer_rejects_self_hashed_forged_package(self) -> None:
        package = build_evidence_package(
            database_path=self.database_path,
            project_root=self.project_root,
            query="正当防卫",
            limit=1,
        )
        forged = "调用方伪造的文字"
        package["evidence"][0]["quote"] = forged
        package["evidence"][0]["quote_sha256"] = hashlib.sha256(
            forged.encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(LegalAIError, "证据包校验失败"):
            write_obsidian_note(
                package=package,
                database_path=self.database_path,
                output_dir=Path("证据笔记"),
                vault_root=self.vault_root,
            )

    def test_cli_stdout_is_json(self) -> None:
        stdout = io.StringIO()
        with patch("legal_ai.cli.REPO_ROOT", self.vault_root):
            with patch("legal_ai.cli.PROJECT_ROOT", self.project_root):
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "obsidian-export",
                            "正当防卫",
                            "--limit",
                            "1",
                            "--output-dir",
                            "命令行笔记",
                            "--db",
                            str(self.database_path),
                        ]
                    )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["evidence_count"], 1)
        self.assertTrue(Path(payload["output_path"]).is_file())


if __name__ == "__main__":
    unittest.main()

"""read_context 的边界与返回契约测试（需要本地语料库）。"""

from __future__ import annotations

import unittest

from legal_ai.mcp_server import CORPUS_DB, read_context

BOOK = "hart-concept-of-law-3e-zh"


@unittest.skipUnless(CORPUS_DB.exists(), "需要本地语料库（data/ 不入库）")
class ReadContextTests(unittest.TestCase):
    def test_reads_two_continuous_pages(self) -> None:
        result = read_context(BOOK, 96, 97)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["quotable"])
        self.assertEqual([page["pdf_page"] for page in result["pages"]], [96, 97])
        self.assertGreater(len(result["text"]), 500)
        self.assertIn("PDF 第 96 页", result["text"])

    def test_single_page_default(self) -> None:
        result = read_context(BOOK, 96)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["pages"]), 1)

    def test_rejects_more_than_two_pages(self) -> None:
        result = read_context(BOOK, 1, 9)
        self.assertEqual(result["status"], "error")
        self.assertIn("最多读 2 页", result["message"])

    def test_rejects_reversed_range(self) -> None:
        result = read_context(BOOK, 9, 1)
        self.assertEqual(result["status"], "error")

    def test_unknown_book(self) -> None:
        result = read_context("no-such-book-id", 1, 1)
        self.assertEqual(result["status"], "not_covered")


if __name__ == "__main__":
    unittest.main()

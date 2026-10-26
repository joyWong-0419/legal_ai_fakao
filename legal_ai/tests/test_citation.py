"""引用渲染层测试：格式、内部字段隔离、receipt。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from legal_ai.citation import (
    assert_public_text,
    book_citation,
    build_receipt,
    classic_citation,
    format_article_label,
    int_to_chinese,
    law_citation,
    public_item,
    write_receipt,
)


class ChineseNumeralTests(unittest.TestCase):
    def test_int_to_chinese(self) -> None:
        cases = {
            1: "一",
            9: "九",
            10: "十",
            11: "十一",
            19: "十九",
            20: "二十",
            21: "二十一",
            100: "一百",
            101: "一百零一",
            110: "一百一十",
            111: "一百一十一",
            1000: "一千",
            1001: "一千零一",
            1010: "一千零一十",
            1260: "一千二百六十",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(int_to_chinese(value), expected)

    def test_format_article_label(self) -> None:
        self.assertEqual(format_article_label("20"), "第二十条")
        self.assertEqual(format_article_label("17-1"), "第十七条之一")
        self.assertEqual(format_article_label("1260"), "第一千二百六十条")
        with self.assertRaises(ValueError):
            format_article_label("")


class BookCitationTests(unittest.TestCase):
    def test_unknown_printed_page_uses_pdf_page_only(self) -> None:
        citation = book_citation(
            file_path="books/天下·法律的概念.pdf",
            title="法律的概念",
            edition="第三版",
            pdf_page=74,
        )
        self.assertEqual(
            citation,
            "——[[books/天下·法律的概念.pdf#page=74|《法律的概念》第三版p.74]]",
        )

    def test_verified_printed_page_shows_both(self) -> None:
        citation = book_citation(
            file_path="books/张明楷刑法6上.pdf",
            title="刑法学",
            edition="第六版（上）",
            pdf_page=307,
            printed_page="278",
            printed_page_status="verified",
        )
        self.assertEqual(
            citation,
            "——[[books/张明楷刑法6上.pdf#page=307|《刑法学》第六版（上）p.278（PDF 307）]]",
        )

    def test_detected_printed_page_is_not_presented_as_verified(self) -> None:
        citation = book_citation(
            file_path="books/法律帝国.pdf",
            title="法律帝国",
            edition=None,
            pdf_page=162,
            printed_page="150",
            printed_page_status="detected",
        )
        self.assertTrue(citation.endswith("|《法律帝国》p.162]]"))


class LawCitationTests(unittest.TestCase):
    def test_with_version_label(self) -> None:
        self.assertEqual(
            law_citation(
                title="中华人民共和国刑法",
                article_no="20",
                version_label="刑法修正案（十二）",
                as_of="2024-03-01",
            ),
            "《中华人民共和国刑法》第二十条（刑法修正案（十二），截至 2024-03-01）",
        )

    def test_without_version_label(self) -> None:
        self.assertEqual(
            law_citation(
                title="中华人民共和国民法典",
                article_no="545",
                version_label=None,
                as_of="2021-01-01",
            ),
            "《中华人民共和国民法典》第五百四十五条（截至 2021-01-01）",
        )


class ClassicCitationTests(unittest.TestCase):
    def test_chapter_only(self) -> None:
        self.assertEqual(
            classic_citation(book="庄子", chapter="骈拇"), "——《庄子·骈拇》"
        )

    def test_book_brackets_are_stripped(self) -> None:
        self.assertEqual(
            classic_citation(book="《庄子》", chapter="骈拇"), "——《庄子·骈拇》"
        )

    def test_empty_chapter_rejected(self) -> None:
        with self.assertRaises(ValueError):
            classic_citation(book="庄子", chapter="  ")


class InternalIsolationTests(unittest.TestCase):
    def test_public_item_drops_internal_fields(self) -> None:
        payload = {
            "quote": "……",
            "citation": "——[[books/x.pdf#page=1|《X》 p.1]]",
            "source": {"kind": "book", "title": "X"},
            "run_id": "20261025T000000+0000-abc",
            "sentence_ids": ["x:p0001:s001"],
            "chunk_id": "x:p0001:c001",
            "quote_sha256": "deadbeef",
            "file_path": "books/x.pdf",
        }
        item = public_item(payload, ref="ev_001")
        self.assertEqual(item["ref"], "ev_001")
        self.assertEqual(item["quote"], "……")
        for field in ("run_id", "sentence_ids", "chunk_id", "quote_sha256", "file_path"):
            self.assertNotIn(field, item)

    def test_assert_public_text_rejects_ids(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_text("证据 ID：x:p0001:s001（sentence_id）")
        assert_public_text("《刑法学》p.278 —— 正当防卫的成立条件")

    def test_receipt_keeps_internal_fields(self) -> None:
        receipt = build_receipt(
            tool="evidence.search",
            query="正当防卫",
            run_id="20261025T000000+0000-abc",
            items=[
                {
                    "sentence_ids": ["a:p1:s1"],
                    "chunk_id": "a:p1:c1",
                    "quote_sha256": "abc",
                    "file_path": "books/a.pdf",
                    "quote": "不应进入 receipt 的引文",
                }
            ],
        )
        self.assertEqual(receipt["run_id"], "20261025T000000+0000-abc")
        self.assertEqual(receipt["evidence"][0]["sentence_ids"], ["a:p1:s1"])
        self.assertNotIn("quote", receipt["evidence"][0])

    def test_write_receipt_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            receipt = build_receipt(
                tool="law.search",
                query="押金",
                run_id="20261025T000000+0000-abc",
                items=[],
            )
            path = write_receipt(directory, receipt)
            self.assertTrue(path.exists())
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["tool"], "law.search"
            )
            with self.assertRaises(FileExistsError):
                write_receipt(directory, receipt)


if __name__ == "__main__":
    unittest.main()

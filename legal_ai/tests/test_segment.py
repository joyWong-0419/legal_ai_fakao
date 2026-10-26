from __future__ import annotations

import unittest

from legal_ai.segment import build_chunk_records, split_sentences


class SegmentTests(unittest.TestCase):
    def test_quotes_brackets_footnotes_and_abbreviations(self):
        text = (
            "他说：“法律不是命令。”然后补充：\n"
            "（括号内部有句号。仍不单独切开）外部结束。"
            "Art. 1 Abs. 2 bleibt。[12]原则继续。"
        )
        sentences = split_sentences(text)

        self.assertEqual(
            [item.text for item in sentences],
            [
                "他说：“法律不是命令。”",
                "然后补充：",
                "（括号内部有句号。仍不单独切开）外部结束。",
                "Art. 1 Abs. 2 bleibt。[12]",
                "原则继续。",
            ],
        )
        for sentence in sentences:
            self.assertEqual(
                text[sentence.start_offset : sentence.end_offset],
                sentence.text,
            )

    def test_chunks_do_not_cross_page_and_reference_existing_sentences(self):
        sentences = [
            {
                "sentence_id": "synthetic:p0001:s001",
                "normalized_text": "第一句。",
            },
            {
                "sentence_id": "synthetic:p0001:s002",
                "normalized_text": "第二句。",
            },
        ]

        chunks = build_chunk_records(
            book_id="synthetic",
            page_id="synthetic:p0001",
            pdf_page=1,
            sentences=sentences,
            chapter_path=["第一章"],
            min_chars=1,
            max_chars=20,
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["page_id"], "synthetic:p0001")
        self.assertEqual(chunks[0]["first_sentence_id"], sentences[0]["sentence_id"])
        self.assertEqual(chunks[0]["last_sentence_id"], sentences[-1]["sentence_id"])


if __name__ == "__main__":
    unittest.main()

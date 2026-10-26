from __future__ import annotations

import unittest

from legal_ai.normalize import NORMALIZATION_VERSION, normalize_page_text


class NormalizeTests(unittest.TestCase):
    def test_minimal_normalization_and_raw_mapping(self):
        raw = "\r\n  第一\u3000条\t  规则。\r\n\r\n\r\n第二条。  "
        normalized = normalize_page_text(raw)

        self.assertEqual(normalized.version, NORMALIZATION_VERSION)
        self.assertEqual(normalized.text, "第一 条 规则。\n\n第二条。")
        self.assertEqual(
            normalized.raw_slice_for_normalized_span(raw, 0, 7).strip(),
            "第一\u3000条\t  规则",
        )

    def test_none_normalizes_to_empty_page(self):
        normalized = normalize_page_text(None)

        self.assertEqual(normalized.text, "")
        self.assertEqual(normalized.raw_offsets, ())


if __name__ == "__main__":
    unittest.main()

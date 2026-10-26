from __future__ import annotations

import unittest

from legal_ai.quality import (
    quality_flags,
    sample_page_numbers,
    suspicious_character_count,
    text_sha256,
)


class QualityTests(unittest.TestCase):
    def test_error_is_not_reported_as_empty(self):
        self.assertEqual(
            quality_flags(None, "PdfReadError: synthetic"),
            ["extraction_error"],
        )

    def test_replacement_character_is_visible(self):
        flags = quality_flags("有效文本\ufffd" * 4, None)
        self.assertIn("replacement_character", flags)
        self.assertGreater(suspicious_character_count("a\ufffdb"), 0)

    def test_samples_cover_start_middle_and_end(self):
        samples = sample_page_numbers(100)
        self.assertEqual(samples[:3], [1, 2, 3])
        self.assertIn(50, samples)
        self.assertEqual(samples[-3:], [98, 99, 100])

    def test_text_hash_changes_with_text(self):
        self.assertNotEqual(text_sha256("规则"), text_sha256("原则"))


if __name__ == "__main__":
    unittest.main()


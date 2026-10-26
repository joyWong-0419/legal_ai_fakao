"""书签修复映射的测试：只改内存副本、三项精确匹配、失效可告警。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from legal_ai.domain import LegalAIError
from legal_ai.process import apply_bookmark_overrides, load_bookmark_overrides

ROOT = Path(__file__).resolve().parents[1]
BOOK_ID = "hart-concept-of-law-3e-zh"

OUTLINE = {
    "entries": [
        {"level": 1, "title": "第乜ॲ䡒䶊", "pdf_page": 8, "status": "valid"},
        {"level": 2, "title": "四、法律和道德", "pdf_page": 19, "status": "valid"},
    ]
}


class ApplyBookmarkOverridesTests(unittest.TestCase):
    def test_replaces_matching_entry(self) -> None:
        patched, stats = apply_bookmark_overrides(
            OUTLINE,
            [
                {
                    "pdf_page": 8,
                    "level": 1,
                    "garbled": "第乜ॲ䡒䶊",
                    "title": "第三版前言",
                }
            ],
        )
        self.assertEqual(patched["entries"][0]["title"], "第三版前言")
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(stats["unused"], 0)

    def test_original_outline_is_not_mutated(self) -> None:
        apply_bookmark_overrides(
            OUTLINE,
            [{"pdf_page": 8, "level": 1, "garbled": "第乜ॲ䡒䶊", "title": "第三版前言"}],
        )
        self.assertEqual(OUTLINE["entries"][0]["title"], "第乜ॲ䡒䶊")

    def test_unmatched_entry_is_reported_not_applied(self) -> None:
        patched, stats = apply_bookmark_overrides(
            OUTLINE,
            [{"pdf_page": 8, "level": 1, "garbled": "书签已经变了", "title": "第三版前言"}],
        )
        self.assertEqual(patched["entries"][0]["title"], "第乜ॲ䡒䶊")
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(stats["unused"], 1)
        self.assertEqual(stats["unused_titles"], ["书签已经变了"])

    def test_level_mismatch_does_not_apply(self) -> None:
        _, stats = apply_bookmark_overrides(
            OUTLINE,
            [{"pdf_page": 8, "level": 2, "garbled": "第乜ॲ䡒䶊", "title": "第三版前言"}],
        )
        self.assertEqual(stats["applied"], 0)

    def test_incomplete_override_raises(self) -> None:
        with self.assertRaises(LegalAIError):
            apply_bookmark_overrides(OUTLINE, [{"pdf_page": 8}])

    def test_empty_overrides_is_noop(self) -> None:
        patched, stats = apply_bookmark_overrides(OUTLINE, [])
        self.assertEqual(patched["entries"], OUTLINE["entries"])
        self.assertEqual(stats["applied"], 0)


class RealOverrideConfigTests(unittest.TestCase):
    @unittest.skipUnless(
        (ROOT / "data" / "raw" / "outlines" / f"{BOOK_ID}.json").exists(),
        "需要本地语料（data/ 不入库）",
    )
    def test_all_22_entries_still_match(self) -> None:
        overrides = load_bookmark_overrides(ROOT, BOOK_ID)
        self.assertEqual(len(overrides), 22)
        outline = json.loads(
            (ROOT / "data" / "raw" / "outlines" / f"{BOOK_ID}.json").read_text(
                encoding="utf-8"
            )
        )
        patched, stats = apply_bookmark_overrides(outline, overrides)
        self.assertEqual(stats["applied"], 22)
        self.assertEqual(stats["unused"], 0)
        garbled_left = [
            entry
            for entry in patched["entries"]
            if "乜" in str(entry.get("title") or "")
        ]
        self.assertEqual(garbled_left, [])


if __name__ == "__main__":
    unittest.main()

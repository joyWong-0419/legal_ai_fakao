from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from legal_ai.domain import LegalAIError
from legal_ai.page_map import (
    build_page_map_audit,
    confirm_page_sample,
    format_printed_page,
    load_page_maps,
    page_map_by_book,
    resolve_page,
)
from legal_ai.tests.test_inventory import sample_book


def write_maps(path: Path, segments: list[dict]) -> None:
    path.write_text(
        json.dumps(
            [{"book_id": "synthetic-book", "segments": segments}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_audit(path: Path, pages: list[tuple[int, str]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "books": [
                    {
                        "book_id": "synthetic-book",
                        "acceptance_samples": [
                            {
                                "pdf_page": pdf_page,
                                "candidate_printed_page": printed_page,
                            }
                            for pdf_page, printed_page in pages
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class PageMapTests(unittest.TestCase):
    def test_resolves_arabic_mapping_and_unknown_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.json"
            write_maps(
                path,
                [
                    {
                        "pdf_start": 3,
                        "pdf_end": 8,
                        "printed_start": 1,
                        "label_type": "arabic",
                        "status": "detected",
                        "verified_samples": [],
                        "inspection_samples": [3, 5, 8],
                    }
                ],
            )
            book_map = page_map_by_book(
                load_page_maps(path, {"synthetic-book": 10})
            )["synthetic-book"]

            self.assertEqual(resolve_page(book_map, 3).printed_page, "1")
            self.assertEqual(resolve_page(book_map, 8).printed_page, "6")
            self.assertEqual(resolve_page(book_map, 2).status, "unknown")
            self.assertIsNone(resolve_page(book_map, 2).printed_page)

    def test_supports_roman_labels(self):
        self.assertEqual(format_printed_page(12, "roman_lower"), "xii")
        self.assertEqual(format_printed_page(12, "roman_upper"), "XII")

    def test_rejects_overlapping_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.json"
            write_maps(
                path,
                [
                    {
                        "pdf_start": 1,
                        "pdf_end": 5,
                        "printed_start": None,
                        "label_type": None,
                        "status": "unknown",
                    },
                    {
                        "pdf_start": 5,
                        "pdf_end": 10,
                        "printed_start": None,
                        "label_type": None,
                        "status": "unknown",
                    },
                ],
            )
            with self.assertRaisesRegex(LegalAIError, "重叠"):
                load_page_maps(path, {"synthetic-book": 10})

    def test_verified_segment_requires_start_middle_and_end(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.json"
            write_maps(
                path,
                [
                    {
                        "pdf_start": 2,
                        "pdf_end": 8,
                        "printed_start": 10,
                        "label_type": "arabic",
                        "status": "verified",
                        "verified_samples": [2, 8],
                    }
                ],
            )
            with self.assertRaisesRegex(LegalAIError, "至少需要 3"):
                load_page_maps(path, {"synthetic-book": 10})

    def test_all_planned_user_confirmations_are_required_for_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps_path = root / "maps.json"
            audit_path = root / "audit.json"
            confirmation_path = root / "private" / "confirmations.json"
            write_maps(
                maps_path,
                [
                    {
                        "pdf_start": 2,
                        "pdf_end": 8,
                        "printed_start": 10,
                        "label_type": "arabic",
                        "status": "detected",
                        "verified_samples": [],
                        "inspection_samples": [2, 5, 8],
                    }
                ],
            )
            write_audit(audit_path, [(4, "12"), (6, "14")])

            first = confirm_page_sample(
                maps_path=maps_path,
                confirmation_path=confirmation_path,
                audit_path=audit_path,
                page_counts={"synthetic-book": 10},
                book_id="synthetic-book",
                pdf_page=4,
                observed_printed_page="12",
                no_visible_page=False,
            )
            first_map = page_map_by_book(
                load_page_maps(maps_path, {"synthetic-book": 10})
            )["synthetic-book"]
            self.assertFalse(first["segment_promoted_to_verified"])
            self.assertEqual(resolve_page(first_map, 4).status, "detected")

            result = confirm_page_sample(
                maps_path=maps_path,
                confirmation_path=confirmation_path,
                audit_path=audit_path,
                page_counts={"synthetic-book": 10},
                book_id="synthetic-book",
                pdf_page=6,
                observed_printed_page="14",
                no_visible_page=False,
            )

            self.assertTrue(result["segment_promoted_to_verified"])
            book_map = page_map_by_book(
                load_page_maps(maps_path, {"synthetic-book": 10})
            )["synthetic-book"]
            self.assertEqual(resolve_page(book_map, 6).status, "verified")
            self.assertEqual(
                book_map.segments[0].verified_samples, (2, 4, 5, 6, 8)
            )
            self.assertTrue(confirmation_path.exists())

    def test_mismatch_does_not_modify_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps_path = root / "maps.json"
            write_maps(
                maps_path,
                [
                    {
                        "pdf_start": 2,
                        "pdf_end": 8,
                        "printed_start": 10,
                        "label_type": "arabic",
                        "status": "detected",
                        "verified_samples": [],
                        "inspection_samples": [2, 5, 8],
                    }
                ],
            )
            before = maps_path.read_bytes()
            with self.assertRaisesRegex(LegalAIError, "核验不一致"):
                confirm_page_sample(
                    maps_path=maps_path,
                    confirmation_path=root / "confirmations.json",
                    page_counts={"synthetic-book": 10},
                    book_id="synthetic-book",
                    pdf_page=6,
                    observed_printed_page="99",
                    no_visible_page=False,
                )
            self.assertEqual(before, maps_path.read_bytes())
            confirmation = json.loads(
                (root / "confirmations.json").read_text(encoding="utf-8")
            )["confirmations"][0]
            self.assertEqual(confirmation["result"], "mismatch")

    def test_contradiction_demotes_verified_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps_path = root / "maps.json"
            audit_path = root / "audit.json"
            confirmation_path = root / "confirmations.json"
            write_maps(
                maps_path,
                [
                    {
                        "pdf_start": 2,
                        "pdf_end": 8,
                        "printed_start": 10,
                        "label_type": "arabic",
                        "status": "detected",
                        "verified_samples": [],
                        "inspection_samples": [2, 5, 8],
                    }
                ],
            )
            write_audit(audit_path, [(4, "12"), (6, "14")])
            for pdf_page, printed_page in ((4, "12"), (6, "14")):
                confirm_page_sample(
                    maps_path=maps_path,
                    confirmation_path=confirmation_path,
                    audit_path=audit_path,
                    page_counts={"synthetic-book": 10},
                    book_id="synthetic-book",
                    pdf_page=pdf_page,
                    observed_printed_page=printed_page,
                    no_visible_page=False,
                )

            with self.assertRaisesRegex(LegalAIError, "核验不一致"):
                confirm_page_sample(
                    maps_path=maps_path,
                    confirmation_path=confirmation_path,
                    audit_path=audit_path,
                    page_counts={"synthetic-book": 10},
                    book_id="synthetic-book",
                    pdf_page=7,
                    observed_printed_page="99",
                    no_visible_page=False,
                )

            book_map = page_map_by_book(
                load_page_maps(maps_path, {"synthetic-book": 10})
            )["synthetic-book"]
            self.assertEqual(resolve_page(book_map, 7).status, "detected")
            self.assertEqual(book_map.segments[0].verified_samples, ())

    def test_corrupt_confirmation_log_blocks_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps_path = root / "maps.json"
            confirmation_path = root / "confirmations.json"
            write_maps(
                maps_path,
                [
                    {
                        "pdf_start": 2,
                        "pdf_end": 8,
                        "printed_start": 10,
                        "label_type": "arabic",
                        "status": "detected",
                        "inspection_samples": [2, 5, 8],
                    }
                ],
            )
            confirmation_path.write_text("{broken", encoding="utf-8")
            before = maps_path.read_bytes()
            with self.assertRaisesRegex(LegalAIError, "不是合法 JSON"):
                confirm_page_sample(
                    maps_path=maps_path,
                    confirmation_path=confirmation_path,
                    page_counts={"synthetic-book": 10},
                    book_id="synthetic-book",
                    pdf_page=4,
                    observed_printed_page="12",
                    no_visible_page=False,
                )
            self.assertEqual(before, maps_path.read_bytes())

    def test_audit_sampling_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps_path = root / "maps.json"
            write_maps(
                maps_path,
                [
                    {
                        "pdf_start": 2,
                        "pdf_end": 9,
                        "printed_start": 10,
                        "label_type": "arabic",
                        "status": "detected",
                        "verified_samples": [],
                        "inspection_samples": [2, 5, 9],
                    }
                ],
            )
            book = sample_book("book/sample.pdf", 10)
            page_maps = load_page_maps(
                maps_path, {"synthetic-book": 10}
            )
            kwargs = {
                "books": [book],
                "page_maps": page_maps,
                "page_counts": {"synthetic-book": 10},
                "project_root": root,
                "per_book": 2,
                "seed": 7,
            }

            first = build_page_map_audit(**kwargs)
            second = build_page_map_audit(**kwargs)

            self.assertEqual(first, second)
            self.assertEqual(
                len(first["books"][0]["acceptance_samples"]), 2
            )


if __name__ == "__main__":
    unittest.main()

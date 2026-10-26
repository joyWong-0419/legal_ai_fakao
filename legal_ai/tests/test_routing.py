"""路由与状态判定测试（纯函数，不依赖语料库）。"""

from __future__ import annotations

import unittest

from legal_ai.routing import (
    classify_status,
    load_layer_map,
    resolve_routing,
    subjects_for_layer,
)


class ResolveRoutingTests(unittest.TestCase):
    def test_book_ids_win(self) -> None:
        routing = resolve_routing(
            book_ids=["book-a"], subjects=["刑法"], layer="value"
        )
        self.assertEqual(routing.book_ids, ("book-a",))
        self.assertEqual(routing.source, "model")

    def test_subjects_beat_layer(self) -> None:
        routing = resolve_routing(subjects=["民法"], layer="value")
        self.assertEqual(routing.subjects, ("民法",))
        self.assertEqual(routing.layer, "value")
        self.assertEqual(routing.book_ids, ())

    def test_layer_maps_to_subjects(self) -> None:
        routing = resolve_routing(layer="value", layer_map={"value": ["法哲学"]})
        self.assertEqual(routing.subjects, ("法哲学",))
        self.assertEqual(routing.source, "model")

    def test_dictionary_only_when_explicit(self) -> None:
        routing = resolve_routing(auto_subjects=["刑法"])
        self.assertEqual(routing.source, "dictionary")
        self.assertEqual(routing.subjects, ("刑法",))

    def test_no_filter_by_default(self) -> None:
        routing = resolve_routing()
        self.assertEqual(routing.source, "none")
        self.assertEqual(routing.subjects, ())
        self.assertEqual(routing.book_ids, ())

    def test_unknown_layer_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_routing(layer="不存在的层", layer_map={"value": ["法哲学"]})

    def test_duplicates_removed(self) -> None:
        routing = resolve_routing(subjects=["民法", "民法", "刑法"])
        self.assertEqual(routing.subjects, ("民法", "刑法"))


class ClassifyStatusTests(unittest.TestCase):
    def test_ok(self) -> None:
        self.assertEqual(classify_status(2, ["book-a"]), "ok")

    def test_no_hit_when_books_exist(self) -> None:
        self.assertEqual(classify_status(0, ["book-a"]), "no_hit")

    def test_not_covered_when_no_books(self) -> None:
        self.assertEqual(classify_status(0, []), "not_covered")


class LayerMapTests(unittest.TestCase):
    def test_real_config_has_expected_layers(self) -> None:
        mapping = load_layer_map()
        for layer in ("doctrine", "value", "genealogy", "norm"):
            with self.subTest(layer=layer):
                self.assertIn(layer, mapping)

    def test_value_layer_covers_philosophy_books(self) -> None:
        subjects = subjects_for_layer("value")
        self.assertIn("法哲学", subjects)
        self.assertIn("法理学", subjects)

    def test_norm_layer_has_no_book_subjects(self) -> None:
        self.assertEqual(subjects_for_layer("norm"), ())


if __name__ == "__main__":
    unittest.main()

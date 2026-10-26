"""作用域解析测试：部门法归属、平局不限定、放开与回显。"""

from __future__ import annotations

import unittest

from legal_ai.scope import Scope, load_terms, resolve_scope


class ResolveScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.terms = load_terms()

    def test_criminal_defense_query(self) -> None:
        scope = resolve_scope("正当防卫 必要限度", terms=self.terms)
        self.assertEqual(scope.domains, ("刑法",))
        self.assertIn("刑法", scope.subjects)
        self.assertIn("criminal-law-cn", scope.laws)
        self.assertFalse(scope.expanded)
        self.assertTrue(scope.filtered)

    def test_civil_lease_query(self) -> None:
        scope = resolve_scope("房东不退押金 租赁合同", terms=self.terms)
        self.assertEqual(scope.domains, ("民法",))
        self.assertIn("合同法", scope.subjects)
        self.assertIn("civil-code-cn", scope.laws)

    def test_company_query(self) -> None:
        scope = resolve_scope("公司 股东 出资", terms=self.terms)
        self.assertEqual(scope.domains, ("商法",))
        self.assertIn("company-law-cn", scope.laws)

    def test_procedure_query(self) -> None:
        scope = resolve_scope("管辖 举证责任", terms=self.terms)
        self.assertEqual(scope.domains, ("民诉",))
        self.assertIn("civil-procedure-law-cn", scope.laws)

    def test_jurisprudence_query(self) -> None:
        scope = resolve_scope("承认规则 法效力", terms=self.terms)
        self.assertEqual(scope.domains, ("法理",))
        self.assertEqual(scope.laws, ())
        self.assertIn("法哲学", scope.subjects)

    def test_why_question_maps_to_jurisprudence(self) -> None:
        scope = resolve_scope("为什么人要服从规则", terms=self.terms)
        self.assertEqual(scope.domains, ("法理",))

    def test_unrelated_query_is_unscoped(self) -> None:
        scope = resolve_scope("今天天气不错", terms=self.terms)
        self.assertEqual(scope.domains, ())
        self.assertFalse(scope.filtered)
        self.assertFalse(scope.expanded)

    def test_cross_domain_tie_is_unscoped(self) -> None:
        # 民法命中「代理」，刑法命中「诈骗」→ 平局，不限定
        scope = resolve_scope("代理 诈骗", terms=self.terms)
        self.assertEqual(scope.domains, ())
        self.assertFalse(scope.filtered)

    def test_override_wins(self) -> None:
        scope = resolve_scope("房东不退押金", terms=self.terms, override="刑法")
        self.assertEqual(scope.domains, ("刑法",))
        self.assertIn("刑法", scope.subjects)

    def test_unknown_override_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_scope("押金", terms=self.terms, override="行政法")

    def test_expand_keeps_domain_but_drops_filter(self) -> None:
        scope = resolve_scope("正当防卫", terms=self.terms)
        expanded = scope.expand()
        self.assertEqual(expanded.domains, ("刑法",))
        self.assertEqual(expanded.subjects, ())
        self.assertTrue(expanded.expanded)
        self.assertFalse(expanded.filtered)

    def test_to_dict_shape(self) -> None:
        payload = resolve_scope("正当防卫", terms=self.terms).to_dict()
        self.assertEqual(
            set(payload), {"domains", "subjects", "laws", "matched_terms", "expanded"}
        )
        self.assertEqual(payload["domains"], ["刑法"])
        self.assertIn("正当防卫", payload["matched_terms"])

    def test_dictionary_is_large_enough(self) -> None:
        total = sum(len(config["terms"]) for config in self.terms.values())
        self.assertGreaterEqual(total, 100)

    def test_every_domain_has_terms(self) -> None:
        for name, config in self.terms.items():
            with self.subTest(domain=name):
                self.assertGreater(len(config["terms"]), 0)


if __name__ == "__main__":
    unittest.main()

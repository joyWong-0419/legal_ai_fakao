"""切词测试：词典种子、二元兜底、显式词不加工。"""

from __future__ import annotations

import unittest

from legal_ai.query_terms import load_dictionary_terms, split_terms


class SplitTermsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dictionary = load_dictionary_terms()

    def test_dictionary_seed_wins(self) -> None:
        terms = split_terms("故意和过失怎么区分", dictionary=self.dictionary)
        self.assertIn("故意", terms)
        self.assertIn("过失", terms)
        # 词典词排在前，且不重复
        self.assertEqual(terms[:2], ["故意", "过失"])
        self.assertEqual(len(terms), len(set(terms)))

    def test_long_term_preferred_over_short(self) -> None:
        terms = split_terms("合同解除后还能要求赔偿吗", dictionary=self.dictionary)
        self.assertEqual(terms[0], "合同解除")

    def test_seed_only_mode_has_no_bigrams(self) -> None:
        terms = split_terms(
            "举证责任由谁承担", dictionary=self.dictionary, include_bigrams=False
        )
        self.assertEqual(terms, ["举证责任"])

    def test_bigram_fallback_when_no_seed(self) -> None:
        terms = split_terms("甲乙丙丁", dictionary=("合同",), include_bigrams=True)
        self.assertTrue(terms)
        self.assertTrue(all(len(item) >= 2 for item in terms))

    def test_whole_query_fallback(self) -> None:
        terms = split_terms("啊", dictionary=(), include_bigrams=False)
        self.assertEqual(terms, ["啊"])

    def test_empty_query(self) -> None:
        self.assertEqual(split_terms("   "), [])

    def test_terms_capped(self) -> None:
        terms = split_terms("合同解除后还能要求赔偿吗名誉权被侵害怎么救济", max_terms=8)
        self.assertLessEqual(len(terms), 8)

    def test_function_char_bigrams_filtered(self) -> None:
        terms = split_terms("为什么可以", dictionary=(), include_bigrams=True)
        # 全是功能字，不应切出有意义二元组
        self.assertNotIn("为什", terms)
        self.assertNotIn("什么", terms)


if __name__ == "__main__":
    unittest.main()

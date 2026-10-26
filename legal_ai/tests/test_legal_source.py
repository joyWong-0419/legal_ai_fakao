from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from legal_ai.domain import LegalAIError
from legal_ai.legal_source import (
    LEGAL_SOURCE_SCHEMA_VERSION,
    LegalSourceConfig,
    article_sort_key,
    build_legal_source_audit,
    chinese_numeral_to_int,
    connect_database,
    cross_refs_for,
    extract_cross_refs,
    ingest_legal_source,
    load_legal_sources,
    parse_articles,
    parse_version_events,
    query_article,
    render_legal_source_audit_markdown,
    write_legal_source_audit,
)


# 全部为自行编写的合成法律文本，不复制任何真实法条正文。
SAMPLE_LAW = """# 中华人民共和国示例法

2020年1月1日 第十三届全国人民代表大会第三次会议通过

2023年12月29日 第十四届全国人民代表大会常务委员会第七次会议通过的《示例法修正案（二）》

<!-- INFO END -->

## 第一编 总则

### 第一章 基本规定

第一条 为了示例，制定本法。

第二条 本法所称示例，指依照本法第三条的处理方式。

## 第二编 处理

### 第二章 处理规则

第三条 处理示例，依照本法第二条、第四条的规定办理。

第四条 示例处理完毕。

第五条 示例法自施行之日起适用。

第六条之一 本目为示例性增补条款。
"""


def sample_config(**overrides) -> LegalSourceConfig:
    values = {
        "law_id": "example-law-cn",
        "title": "中华人民共和国示例法",
        "short_title": "示例法",
        "enacting_body": "全国人民代表大会",
        "source_file": "lawbook/示例法.md",
        "enacted_on": "2020-01-01",
        "base_effective_from": "2020-01-01",
        "text_as_of": "2024-03-01",
        "current_version_label": "示例法修正案（二）",
        "enabled": True,
    }
    values.update(overrides)
    return LegalSourceConfig.from_dict(values)


class ChineseNumeralTests(unittest.TestCase):
    def test_converts_article_numbers(self):
        cases = {
            "一": 1,
            "十": 10,
            "十七": 17,
            "二十": 20,
            "二十三": 23,
            "一百": 100,
            "一百零五": 105,
            "一百二十三": 123,
            "二百": 200,
            "一千二百六十": 1260,
            "四百五十二": 452,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(chinese_numeral_to_int(text), expected)

    def test_rejects_invalid_input(self):
        for bad in ("", "abc", "第", "零"):
            with self.subTest(bad=bad):
                with self.assertRaises(LegalAIError):
                    chinese_numeral_to_int(bad)

    def test_article_sort_key(self):
        self.assertEqual(article_sort_key("17"), (17, 0))
        self.assertEqual(article_sort_key("17-1"), (17, 1))


class ParseArticleTests(unittest.TestCase):
    def setUp(self):
        self.articles = parse_articles(SAMPLE_LAW)
        self.by_no = {item.article_no: item for item in self.articles}

    def test_reads_only_line_start_article_headings(self):
        # 正文里的「本法第三条」「本法第二条、第四条」不得被当成新条文
        self.assertEqual(
            [item.article_no for item in self.articles],
            ["1", "2", "3", "4", "5", "6-1"],
        )

    def test_supplemental_article_is_numbered_with_suffix(self):
        supplemental = self.by_no["6-1"]
        self.assertEqual(supplemental.article_no_kind, "supplemental")
        self.assertEqual(supplemental.article_no_sort, 6)
        self.assertEqual(supplemental.supplement_index, 1)

    def test_chapter_path_follows_headings(self):
        self.assertEqual(
            self.by_no["1"].chapter_path,
            ("第一编 总则", "第一章 基本规定"),
        )
        self.assertEqual(
            self.by_no["4"].chapter_path,
            ("第二编 处理", "第二章 处理规则"),
        )

    def test_offsets_round_trip_to_source_text(self):
        for article in self.articles:
            with self.subTest(article=article.article_no):
                self.assertEqual(
                    SAMPLE_LAW[
                        article.char_offset_start : article.char_offset_end
                    ],
                    article.text,
                )

    def test_next_heading_is_not_swallowed_into_article(self):
        self.assertNotIn("第二编", self.by_no["2"].text)
        self.assertTrue(self.by_no["2"].text.startswith("第二条"))

    def test_cross_refs_skip_the_articles_own_heading(self):
        refs = extract_cross_refs(self.by_no["3"].text)
        self.assertEqual(
            [ref.to_article_no for ref in refs], ["2", "4"]
        )

    def test_cross_refs_detect_supplemental_target(self):
        refs = extract_cross_refs("第十条 处理时适用第六条之一的规定。")
        self.assertEqual([ref.to_article_no for ref in refs], ["6-1"])


class VersionEventTests(unittest.TestCase):
    def test_parses_adoption_events(self):
        events = parse_version_events(SAMPLE_LAW)
        labels = [event.label for event in events]
        self.assertIn("示例法修正案（二）", labels)
        adopted = {
            event.label: event.adopted_on for event in events
        }
        self.assertEqual(adopted["示例法修正案（二）"], "2023-12-29")

    def test_deduplicates_repeated_multi_block_headers(self):
        # 民法典式结构：多个 <!-- INFO END --> 各自带一份相同的文件头
        text = (
            "2020年5月28日 第十三届全国人民代表大会第三次会议通过\n"
            "<!-- INFO END -->\n"
            "## 第一编\n"
            "第一条 示例。\n"
            "2020年5月28日 第十三届全国人民代表大会第三次会议通过\n"
            "<!-- INFO END -->\n"
            "## 第二编\n"
            "第二条 示例。\n"
        )
        events = parse_version_events(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].adopted_on, "2020-05-28")


class LoadConfigTests(unittest.TestCase):
    def test_rejects_duplicate_law_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legal_sources.json"
            entry = {
                "law_id": "dup",
                "title": "示例",
                "source_file": "lawbook/x.md",
                "base_effective_from": "2020-01-01",
                "text_as_of": "2020-01-01",
            }
            path.write_text(
                json.dumps([entry, dict(entry)], ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(LegalAIError):
                load_legal_sources(path)

    def test_rejects_malformed_date(self):
        with self.assertRaises(LegalAIError):
            LegalSourceConfig.from_dict(
                {
                    "law_id": "x",
                    "title": "示例",
                    "source_file": "lawbook/x.md",
                    "base_effective_from": "2020/01/01",
                    "text_as_of": "2020-01-01",
                }
            )


class IngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "lawbook").mkdir(parents=True)
        self.source = self.root / "lawbook" / "示例法.md"
        self.source.write_text(SAMPLE_LAW, encoding="utf-8")
        self.database = self.root / "data" / "index" / "legal_sources.sqlite"

    def tearDown(self):
        self._tmp.cleanup()

    def _ingest(self, config: LegalSourceConfig | None = None):
        return ingest_legal_source(
            database_path=self.database,
            config=config or sample_config(),
            project_root=self.root,
        )

    def test_first_ingest_records_every_article(self):
        result = self._ingest()
        self.assertEqual(result.articles_total, 6)
        self.assertEqual(result.articles_inserted, 6)
        self.assertEqual(result.articles_unchanged, 0)
        # 第二条引用第三条、第三条引用第二条与第四条，共 3 条交叉引用
        self.assertEqual(result.cross_refs, 3)
        self.assertEqual(result.version_events, 2)

    def test_reingest_same_text_is_idempotent(self):
        self._ingest()
        second = self._ingest()
        self.assertEqual(second.articles_inserted, 0)
        self.assertEqual(second.articles_unchanged, 6)
        self.assertEqual(second.cross_refs, 0)

        connection = sqlite3.connect(str(self.database))
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM legal_article_versions"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 6)

    def test_changed_article_appends_version_and_keeps_old_text(self):
        self._ingest()
        changed = SAMPLE_LAW.replace(
            "第二条 本法所称示例，指依照本法第三条的处理方式。",
            "第二条 本法所称示例，指依照本法第五条的处理方式。",
        )
        self.source.write_text(changed, encoding="utf-8")
        updated = sample_config(text_as_of="2026-01-01")
        result = self._ingest(updated)

        self.assertEqual(result.articles_superseded, 1)
        self.assertEqual(result.articles_unchanged, 5)

        connection = sqlite3.connect(str(self.database))
        try:
            rows = connection.execute(
                """
                SELECT version_id, text, effective_from, effective_to, status
                FROM legal_article_versions
                WHERE law_id = ? AND article_no = ?
                ORDER BY effective_from
                """,
                ("example-law-cn", "2"),
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(len(rows), 2)
        old, new = rows
        self.assertIn("第三条", old[1])
        self.assertEqual(old[2], "2020-01-01")
        self.assertEqual(old[3], "2026-01-01")
        self.assertEqual(old[4], "superseded")
        self.assertIn("第五条", new[1])
        self.assertEqual(new[2], "2026-01-01")
        self.assertIsNone(new[3])
        self.assertEqual(new[4], "current")

    def test_time_travel_query_returns_the_effective_version(self):
        self._ingest()
        changed = SAMPLE_LAW.replace(
            "第四条 示例处理完毕。", "第四条 示例处理已完毕。"
        )
        self.source.write_text(changed, encoding="utf-8")
        self._ingest(sample_config(text_as_of="2026-01-01"))

        historical = query_article(
            database_path=self.database,
            law_id="example-law-cn",
            article_no="4",
            as_of="2024-06-01",
        )
        current = query_article(
            database_path=self.database,
            law_id="example-law-cn",
            article_no="4",
            as_of="2026-06-01",
        )
        self.assertIsNotNone(historical)
        self.assertIsNotNone(current)
        assert historical is not None and current is not None
        self.assertEqual(historical.text, "第四条 示例处理完毕。")
        self.assertEqual(current.text, "第四条 示例处理已完毕。")
        self.assertNotEqual(historical.version_id, current.version_id)

    def test_query_before_effective_date_returns_nothing(self):
        self._ingest()
        result = query_article(
            database_path=self.database,
            law_id="example-law-cn",
            article_no="1",
            as_of="2019-01-01",
        )
        self.assertIsNone(result)

    def test_identity_fields_cannot_be_changed(self):
        self._ingest()
        with self.assertRaises(LegalAIError):
            self._ingest(sample_config(title="中华人民共和国示例法（改）"))

    def test_refuses_to_close_version_without_newer_text_as_of(self):
        self._ingest()
        changed = SAMPLE_LAW.replace("第四条 示例处理完毕。", "第四条 改了。")
        self.source.write_text(changed, encoding="utf-8")
        with self.assertRaises(LegalAIError):
            self._ingest(sample_config(text_as_of="2020-01-01"))

    def test_missing_source_file_is_reported(self):
        self.source.unlink()
        with self.assertRaises(LegalAIError):
            self._ingest()

    def test_incompatible_schema_is_not_read_silently(self):
        self._ingest()
        connection = sqlite3.connect(str(self.database))
        try:
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(LegalAIError):
            connect_database(self.database, create=False)
        self.assertNotEqual(LEGAL_SOURCE_SCHEMA_VERSION, 99)

    def test_read_only_connection_refuses_writes(self):
        self._ingest()
        connection = connect_database(
            self.database, create=False, read_only=True
        )
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "INSERT INTO legal_source_metadata(key, value) "
                    "VALUES ('probe', 'probe')"
                )
        finally:
            connection.close()

    def test_read_only_connection_requires_existing_database(self):
        missing = self.root / "data" / "index" / "absent.sqlite"
        with self.assertRaises(LegalAIError):
            connect_database(missing, create=False, read_only=True)

    def test_cross_refs_are_queryable_both_directions(self):
        self._ingest()
        refs = cross_refs_for(
            database_path=self.database,
            law_id="example-law-cn",
            article_no="3",
        )
        self.assertEqual(
            sorted(item["to_article_no"] for item in refs["outgoing"]),
            ["2", "4"],
        )
        incoming = cross_refs_for(
            database_path=self.database,
            law_id="example-law-cn",
            article_no="2",
        )
        self.assertEqual(
            [item["from_article_no"] for item in incoming["incoming"]],
            ["3"],
        )


class AuditTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "lawbook").mkdir(parents=True)
        self.database = self.root / "data" / "index" / "legal_sources.sqlite"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_and_ingest(self, text: str):
        source = self.root / "lawbook" / "示例法.md"
        source.write_text(text, encoding="utf-8")
        return ingest_legal_source(
            database_path=self.database,
            config=sample_config(),
            project_root=self.root,
        )

    def test_reports_contiguous_article_numbers(self):
        self._write_and_ingest(SAMPLE_LAW)
        audit = build_legal_source_audit(self.database)
        law = audit["laws"][0]
        self.assertEqual(law["current_articles"], 6)
        self.assertEqual(law["supplemental_articles"], 1)
        self.assertEqual(law["missing_article_numbers"], [])
        self.assertEqual(audit["integrity_check"], "ok")
        self.assertEqual(audit["foreign_key_violations"], 0)

    def test_detects_missing_article_number(self):
        text = SAMPLE_LAW.replace("第三条 处理示例，依照本法第二条、第四条的规定办理。\n\n", "")
        self._write_and_ingest(text)
        audit = build_legal_source_audit(self.database)
        self.assertEqual(audit["laws"][0]["missing_article_numbers"], [3])

    def test_render_and_write_outputs(self):
        self._write_and_ingest(SAMPLE_LAW)
        audit = build_legal_source_audit(self.database)
        markdown = render_legal_source_audit_markdown(audit)
        self.assertIn("法源层审计", markdown)
        self.assertIn("示例法", markdown)

        json_path = self.root / "reports" / "legal_source_audit.json"
        md_path = self.root / "reports" / "legal_source_audit.md"
        write_legal_source_audit(audit, json_path, md_path)
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        reloaded = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["counts"]["laws"], 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .acceptance import write_citation_acceptance_markdown
from .domain import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_PAGE_MAPS_PATH,
    REPO_ROOT,
    LegalAIError,
)
from .evidence import build_evidence_package, render_evidence_markdown
from .evaluation import (
    evaluate_dataset,
    render_evaluation_text,
    write_evaluation_report,
)
from .extract import extract_book, merge_quality_reports
from .inventory import (
    format_inventory,
    inspect_books,
    load_books,
    write_inventory_report,
)
from .obsidian_export import export_query_to_obsidian
from .ocr_pilot import (
    DEFAULT_CHECKPOINT_EVERY,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RENDER_DPI,
    DEFAULT_TIMEOUT_SECONDS,
    run_ocr_pilot,
    select_random_pages,
    write_ocr_pilot_report,
)
from .ocr_exclusions import load_ocr_page_exclusions
from .ocr_import import import_pp_ocrv6_json, write_external_ocr_import_report
from .index import (
    DEFAULT_DATABASE_RELATIVE_PATH,
    build_index,
    write_index_report,
)
from .legal_source import (
    DEFAULT_LEGAL_SOURCES_CONFIG_PATH,
    DEFAULT_LEGAL_SOURCE_DATABASE_RELATIVE_PATH,
    build_legal_source_audit,
    cross_refs_for,
    ingest_legal_source,
    load_legal_sources,
    query_article,
    render_article_text,
    render_legal_source_audit_markdown,
    write_legal_source_audit,
)
from .process import merge_processing_reports, process_book
from .page_map import (
    build_page_map_audit,
    confirm_page_sample,
    load_page_maps,
    render_page_map_audit_markdown,
    write_page_map_audit,
)
from .search import render_search_markdown, search_index
from .verify import (
    render_verification_text,
    verify_corpus,
    write_verification_report,
)


PROJECT_ROOT = REPO_ROOT / "legal_ai"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / DEFAULT_DATABASE_RELATIVE_PATH
DEFAULT_LEGAL_SOURCE_DATABASE_PATH = (
    PROJECT_ROOT / DEFAULT_LEGAL_SOURCE_DATABASE_RELATIVE_PATH
)


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def _repo_path(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m legal_ai",
        description="本地、私有、可追溯的法律学习语料工具（P0-P6）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser(
        "inventory", help="校验并列出六本目标书籍"
    )
    inventory_parser.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    inventory_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="书目配置路径（默认 legal_ai/config/books.json）",
    )

    ingest_parser = subparsers.add_parser(
        "ingest", help="逐页抽取原始文本并生成质量报告"
    )
    selection = ingest_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="抽取全部书籍")
    selection.add_argument("--book-id", help="只抽取指定 book_id")
    ingest_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="书目配置路径（默认 legal_ai/config/books.json）",
    )

    process_parser = subparsers.add_parser(
        "process", help="规范化、切句并生成页内检索块"
    )
    process_selection = process_parser.add_mutually_exclusive_group(
        required=True
    )
    process_selection.add_argument(
        "--all", action="store_true", help="处理全部已抽取书籍"
    )
    process_selection.add_argument("--book-id", help="只处理指定 book_id")
    process_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="书目配置路径（默认 legal_ai/config/books.json）",
    )

    page_map_parser = subparsers.add_parser(
        "page-map", help="审计并人工确认 PDF 页到印刷页的分段映射"
    )
    page_map_actions = page_map_parser.add_subparsers(
        dest="page_map_command", required=True
    )
    audit_parser = page_map_actions.add_parser(
        "audit", help="生成页码映射审计与随机人工抽样清单"
    )
    audit_parser.add_argument(
        "--format", choices=("markdown", "json"), default="markdown"
    )
    audit_parser.add_argument(
        "--per-book",
        type=int,
        choices=(1, 2),
        default=2,
        help="每本书随机抽取 1 或 2 页（默认 2）",
    )
    audit_parser.add_argument(
        "--seed", type=int, default=20260710, help="确定性随机种子"
    )
    audit_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH
    )
    audit_parser.add_argument(
        "--maps", type=Path, default=DEFAULT_PAGE_MAPS_PATH
    )

    confirm_parser = page_map_actions.add_parser(
        "confirm", help="记录一页人工观察并在条件满足时升级映射段"
    )
    confirm_parser.add_argument("--book-id", required=True)
    confirm_parser.add_argument("--pdf-page", type=int, required=True)
    observed = confirm_parser.add_mutually_exclusive_group(required=True)
    observed.add_argument("--printed-page", help="页面上看到的印刷页标签")
    observed.add_argument(
        "--no-visible-page",
        action="store_true",
        help="确认该 PDF 页没有可见印刷页码",
    )
    confirm_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH
    )
    confirm_parser.add_argument(
        "--maps", type=Path, default=DEFAULT_PAGE_MAPS_PATH
    )

    index_parser = subparsers.add_parser(
        "build-index", help="从本地 JSONL 原子重建 SQLite/FTS5 索引"
    )
    index_parser.add_argument(
        "--rebuild",
        action="store_true",
        required=True,
        help="显式确认重建本地索引",
    )
    index_parser.add_argument(
        "--discard-retrieval-history",
        action="store_true",
        help="重建时丢弃与当前书目范围不再可复现的历史检索记录",
    )
    index_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH
    )
    index_parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE_PATH
    )

    search_parser = subparsers.add_parser(
        "search", help="检索本地 SQLite/FTS5 法律学习语料"
    )
    search_parser.add_argument("query", help="中文术语、短语或多词查询")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument(
        "--format", choices=("markdown", "json"), default="markdown"
    )
    search_parser.add_argument(
        "--book-id", action="append", default=[], help="可重复指定书籍过滤"
    )
    search_parser.add_argument(
        "--jurisdiction", action="append", default=[], help="可重复指定法域过滤"
    )
    search_parser.add_argument(
        "--subject", action="append", default=[], help="可重复指定学科过滤"
    )
    search_parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE_PATH
    )

    context_parser = subparsers.add_parser(
        "context", help="从检索结果回查句子记录并生成可核验的直接引文证据包"
    )
    context_parser.add_argument("query", help="中文术语、短语或多词查询")
    context_parser.add_argument("--limit", type=int, default=8)
    context_parser.add_argument(
        "--format", choices=("json", "markdown"), default="json"
    )
    context_parser.add_argument(
        "--book-id", action="append", default=[], help="可重复指定书籍过滤"
    )
    context_parser.add_argument(
        "--jurisdiction", action="append", default=[], help="可重复指定法域过滤"
    )
    context_parser.add_argument(
        "--subject", action="append", default=[], help="可重复指定学科过滤"
    )
    context_parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE_PATH
    )

    verify_parser = subparsers.add_parser(
        "verify", help="只读核验 PDF、页码映射、句子、chunk、FTS 和证据包"
    )
    verify_parser.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    verify_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH
    )
    verify_parser.add_argument(
        "--maps", type=Path, default=DEFAULT_PAGE_MAPS_PATH
    )
    verify_parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE_PATH
    )
    verify_parser.add_argument(
        "--evidence",
        type=Path,
        action="append",
        default=[],
        help="额外核验一个证据包；可重复指定",
    )
    verify_parser.add_argument(
        "--current-scope",
        action="store_true",
        help="只核验当前启用书目与显式 --evidence，不扫描历史证据包",
    )

    citation_parser = subparsers.add_parser(
        "citation-audit", help="复用 P3 随机页面生成 P5 人工引用核验清单"
    )
    citation_parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE_PATH
    )
    citation_parser.add_argument(
        "--audit",
        type=Path,
        default=PROJECT_ROOT / "reports" / "page_map_audit.json",
    )
    citation_parser.add_argument(
        "--output",
        type=Path,
        default=(
            PROJECT_ROOT
            / "reports"
            / "private"
            / "citation_acceptance.md"
        ),
    )

    obsidian_parser = subparsers.add_parser(
        "obsidian-export",
        help="从本地证据包新建 Obsidian/Claudian 证据笔记（永不覆盖）",
    )
    obsidian_parser.add_argument("query", help="要形成证据笔记的学习问题")
    obsidian_parser.add_argument("--limit", type=int, default=8)
    obsidian_parser.add_argument(
        "--book-id", action="append", default=[], help="可重复指定书籍过滤"
    )
    obsidian_parser.add_argument(
        "--jurisdiction", action="append", default=[], help="可重复指定法域过滤"
    )
    obsidian_parser.add_argument(
        "--subject", action="append", default=[], help="可重复指定学科过滤"
    )
    obsidian_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("法学攻克系统") / "AI法律学习系统" / "证据笔记",
        help="vault 内输出目录；默认写入 AI法律学习系统/证据笔记",
    )
    obsidian_parser.add_argument(
        "--filename", help="可选 .md 文件名；同名时自动创建安全后缀"
    )
    obsidian_parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE_PATH
    )

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="运行冻结黄金问题集的离线页级检索评测"
    )
    evaluate_parser.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    evaluate_parser.add_argument(
        "--golden",
        type=Path,
        default=PROJECT_ROOT / "config" / "golden_questions.json",
    )
    evaluate_parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE_PATH
    )

    ocr_pilot_parser = subparsers.add_parser(
        "ocr-pilot", help="对已配置的扫描版书籍执行有限页数、本地可审计 OCR 试点"
    )
    ocr_pilot_parser.add_argument("--book-id", required=True)
    pilot_selection = ocr_pilot_parser.add_mutually_exclusive_group(required=True)
    pilot_selection.add_argument(
        "--pages", type=int, nargs="+", help="显式指定一组一基 PDF 页码"
    )
    pilot_selection.add_argument(
        "--random-count", type=int, help="从全书均匀随机抽取指定页数"
    )
    ocr_pilot_parser.add_argument(
        "--seed", type=int, default=20260811, help="--random-count 的确定性随机种子"
    )
    ocr_pilot_parser.add_argument("--dpi", type=int, default=DEFAULT_RENDER_DPI)
    ocr_pilot_parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS
    )
    ocr_pilot_parser.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS
    )
    ocr_pilot_parser.add_argument("--force", action="store_true")
    ocr_pilot_parser.add_argument(
        "--report",
        type=Path,
        help="仅允许写入 legal_ai/reports/ 下的 OCR 审计 JSON 路径",
    )
    ocr_pilot_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH
    )

    ocr_parser = subparsers.add_parser(
        "ocr", help="Run auditable, full-book local OCR for a configured scanned book"
    )
    ocr_parser.add_argument("--book-id", required=True)
    ocr_parser.add_argument(
        "--all",
        action="store_true",
        required=True,
        help="OCR every non-excluded page; exclusions are source-hash bound",
    )
    ocr_parser.add_argument("--dpi", type=int, default=DEFAULT_RENDER_DPI)
    ocr_parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS
    )
    ocr_parser.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS
    )
    ocr_parser.add_argument(
        "--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY
    )
    ocr_parser.add_argument("--force", action="store_true")
    ocr_parser.add_argument(
        "--exclusions",
        type=Path,
        default=PROJECT_ROOT / "config" / "ocr_page_exclusions.json",
        help="Source-bound OCR page-exclusion registry",
    )
    ocr_parser.add_argument(
        "--report",
        type=Path,
        help="Only a path below legal_ai/reports/ is accepted",
    )
    ocr_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    ocr_import_parser = subparsers.add_parser(
        "ocr-import", help="Import page-bounded external PP-OCRv6 JSON without running OCR"
    )
    ocr_import_parser.add_argument("--book-id", required=True)
    ocr_import_parser.add_argument(
        "--input", type=Path, required=True, help="External PP-OCRv6 page-result JSON"
    )
    ocr_import_parser.add_argument(
        "--exclusions",
        type=Path,
        default=PROJECT_ROOT / "config" / "ocr_page_exclusions.json",
    )
    ocr_import_parser.add_argument("--force", action="store_true")
    ocr_import_parser.add_argument("--report", type=Path)
    ocr_import_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    legal_source_parser = subparsers.add_parser(
        "legal-source", help="法源层：append-only 的法条版本库（民法典、刑法）"
    )
    legal_source_actions = legal_source_parser.add_subparsers(
        dest="legal_source_command", required=True
    )

    ls_ingest = legal_source_actions.add_parser(
        "ingest", help="解析 lawbook/*.md 并写入 append-only 法条版本库"
    )
    ls_ingest.add_argument(
        "--law-id",
        action="append",
        default=[],
        help="只摄取指定 law_id（可重复）",
    )
    ls_ingest.add_argument(
        "--all", action="store_true", help="摄取配置中全部启用的法源"
    )
    ls_ingest.add_argument(
        "--config", type=Path, default=DEFAULT_LEGAL_SOURCES_CONFIG_PATH
    )
    ls_ingest.add_argument(
        "--db", type=Path, default=DEFAULT_LEGAL_SOURCE_DATABASE_PATH
    )

    ls_audit = legal_source_actions.add_parser(
        "audit", help="审计法源库：条号连续性、交叉引用、版本状态"
    )
    ls_audit.add_argument(
        "--format", choices=("markdown", "json"), default="markdown"
    )
    ls_audit.add_argument(
        "--db", type=Path, default=DEFAULT_LEGAL_SOURCE_DATABASE_PATH
    )

    ls_show = legal_source_actions.add_parser(
        "show", help="按 as-of 时点查询条文（时间旅行查询）"
    )
    ls_show.add_argument("--law-id", required=True)
    ls_show.add_argument("--article", required=True, help="条号，如 17 或 17-1")
    ls_show.add_argument(
        "--as-of",
        default=None,
        help="引用时点 YYYY-MM-DD（默认今天）",
    )
    ls_show.add_argument(
        "--refs", action="store_true", help="同时显示引出与被引用的条文"
    )
    ls_show.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    ls_show.add_argument(
        "--db", type=Path, default=DEFAULT_LEGAL_SOURCE_DATABASE_PATH
    )

    return parser


def _load_enabled_books(config_path: Path):
    return [book for book in load_books(_repo_path(config_path)) if book.enabled]


def run_inventory(output_format: str, config_path: Path) -> int:
    records = inspect_books(_load_enabled_books(config_path), REPO_ROOT)
    report_path = PROJECT_ROOT / "reports" / "inventory.json"
    write_inventory_report(records, report_path)
    if output_format == "json":
        print(
            json.dumps(
                {
                    "book_count": len(records),
                    "books": [record.to_dict() for record in records],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_inventory(records))
        print(f"\n共 {len(records)} 本；审计报告：{report_path}")
    return 0


def run_ingest(
    all_books: bool, book_id: str | None, config_path: Path
) -> int:
    books = _load_enabled_books(config_path)
    if not all_books:
        books = [book for book in books if book.book_id == book_id]
        if not books:
            raise LegalAIError(f"未知或未启用的 book_id：{book_id}")

    records = inspect_books(books, REPO_ROOT)

    def progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    reports = [
        extract_book(record, PROJECT_ROOT, progress=progress)
        for record in records
    ]
    aggregate_path = PROJECT_ROOT / "reports" / "extraction_quality.json"
    aggregate = merge_quality_reports(reports, aggregate_path, replace=all_books)
    print(
        json.dumps(
            {
                "processed_books": [item["book_id"] for item in reports],
                "processed_pages": sum(
                    item["pdf_page_count"] for item in reports
                ),
                "extraction_errors": sum(
                    item["extraction_error_count"] for item in reports
                ),
                "all_source_pdfs_unchanged": aggregate[
                    "all_source_pdfs_unchanged"
                ],
                "quality_report": str(aggregate_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run_process(
    all_books: bool, book_id: str | None, config_path: Path
) -> int:
    books = _load_enabled_books(config_path)
    if not all_books:
        books = [book for book in books if book.book_id == book_id]
        if not books:
            raise LegalAIError(f"未知或未启用的 book_id：{book_id}")

    records = inspect_books(books, REPO_ROOT)
    reports = [process_book(record, PROJECT_ROOT) for record in records]
    aggregate_path = PROJECT_ROOT / "reports" / "processing_quality.json"
    aggregate = merge_processing_reports(reports, aggregate_path, replace=all_books)
    print(
        json.dumps(
            {
                "processed_books": [item["book_id"] for item in reports],
                "processed_pages": sum(
                    item["processed_page_count"] for item in reports
                ),
                "sentences": sum(
                    item["sentence_count"] for item in reports
                ),
                "chunks": sum(item["chunk_count"] for item in reports),
                "processing_report": str(aggregate_path),
                "aggregate_totals": {
                    "books": aggregate["book_count"],
                    "pages": aggregate["total_processed_pages"],
                    "sentences": aggregate["total_sentences"],
                    "chunks": aggregate["total_chunks"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run_page_map_audit(
    *,
    output_format: str,
    per_book: int,
    seed: int,
    config_path: Path,
    maps_path: Path,
) -> int:
    books = _load_enabled_books(config_path)
    records = inspect_books(books, REPO_ROOT)
    page_counts = {
        record.book.book_id: record.actual_pdf_pages for record in records
    }
    maps_path = _repo_path(maps_path)
    page_maps = load_page_maps(maps_path, page_counts)
    audit = build_page_map_audit(
        books=books,
        page_maps=page_maps,
        page_counts=page_counts,
        project_root=PROJECT_ROOT,
        per_book=per_book,
        seed=seed,
    )
    json_path = PROJECT_ROOT / "reports" / "page_map_audit.json"
    markdown_path = PROJECT_ROOT / "reports" / "page_map_audit.md"
    write_page_map_audit(audit, json_path, markdown_path)
    if output_format == "json":
        print(json.dumps(audit, ensure_ascii=False, indent=2))
    else:
        print(render_page_map_audit_markdown(audit))
        print(f"\n审计报告：{markdown_path}", file=sys.stderr)
    return 0


def run_page_map_confirm(
    *,
    book_id: str,
    pdf_page: int,
    printed_page: str | None,
    no_visible_page: bool,
    config_path: Path,
    maps_path: Path,
) -> int:
    books = _load_enabled_books(config_path)
    records = inspect_books(books, REPO_ROOT)
    page_counts = {
        record.book.book_id: record.actual_pdf_pages for record in records
    }
    confirmation_path = (
        PROJECT_ROOT / "reports" / "private" / "page_map_confirmations.json"
    )
    audit_path = PROJECT_ROOT / "reports" / "page_map_audit.json"
    result = confirm_page_sample(
        maps_path=_repo_path(maps_path),
        confirmation_path=confirmation_path,
        audit_path=audit_path,
        page_counts=page_counts,
        book_id=book_id,
        pdf_page=pdf_page,
        observed_printed_page=printed_page,
        no_visible_page=no_visible_page,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run_legal_source_ingest(
    *,
    law_ids: Sequence[str],
    all_sources: bool,
    config_path: Path,
    database_path: Path,
) -> int:
    configs = [
        config
        for config in load_legal_sources(_repo_path(config_path))
        if config.enabled
    ]
    if not all_sources:
        wanted = set(law_ids)
        if not wanted:
            raise LegalAIError(
                "请用 --all 摄取全部启用法源，或用 --law-id 指定一个或多个"
            )
        known = {config.law_id for config in configs}
        unknown = sorted(wanted - known)
        if unknown:
            raise LegalAIError("未登记或未启用的 law_id：" + "、".join(unknown))
        configs = [config for config in configs if config.law_id in wanted]
    if not configs:
        raise LegalAIError("没有可摄取的法源")

    resolved_database = _repo_path(database_path)
    results = [
        ingest_legal_source(
            database_path=resolved_database,
            config=config,
            project_root=REPO_ROOT,
        )
        for config in configs
    ]
    print(
        json.dumps(
            [result.to_dict() for result in results],
            ensure_ascii=False,
            indent=2,
        )
    )
    audit = build_legal_source_audit(resolved_database)
    json_path = PROJECT_ROOT / "reports" / "legal_source_audit.json"
    markdown_path = PROJECT_ROOT / "reports" / "legal_source_audit.md"
    write_legal_source_audit(audit, json_path, markdown_path)
    print(f"审计报告：{markdown_path}", file=sys.stderr)
    return 0


def run_legal_source_audit(
    *, output_format: str, database_path: Path
) -> int:
    audit = build_legal_source_audit(_repo_path(database_path))
    json_path = PROJECT_ROOT / "reports" / "legal_source_audit.json"
    markdown_path = PROJECT_ROOT / "reports" / "legal_source_audit.md"
    write_legal_source_audit(audit, json_path, markdown_path)
    if output_format == "json":
        print(json.dumps(audit, ensure_ascii=False, indent=2))
    else:
        print(render_legal_source_audit_markdown(audit))
        print(f"\n审计报告：{markdown_path}", file=sys.stderr)
    return 0


def run_legal_source_show(
    *,
    law_id: str,
    article_no: str,
    as_of: str | None,
    output_format: str,
    with_refs: bool,
    database_path: Path,
) -> int:
    resolved_database = _repo_path(database_path)
    view = query_article(
        database_path=resolved_database,
        law_id=law_id,
        article_no=article_no,
        as_of=as_of,
    )
    if view is None:
        raise LegalAIError(
            f"{law_id} 第 {article_no} 条在 {as_of or '今天'} 没有生效版本；"
            "该条号可能不存在，或引用时点早于法律生效日。"
        )
    refs = None
    if with_refs:
        refs = cross_refs_for(
            database_path=resolved_database,
            law_id=law_id,
            article_no=view.article_no,
        )
    if output_format == "json":
        payload = view.to_dict()
        if refs is not None:
            payload["cross_refs"] = refs
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_article_text(view, refs))
    return 0


def run_build_index(
    *,
    rebuild: bool,
    config_path: Path,
    database_path: Path,
    discard_retrieval_history: bool = False,
) -> int:
    books = _load_enabled_books(config_path)
    records = inspect_books(books, REPO_ROOT)
    report = build_index(
        records=records,
        project_root=PROJECT_ROOT,
        database_path=_repo_path(database_path),
        rebuild=rebuild,
        preserve_retrieval_runs=not discard_retrieval_history,
    )
    report_path = PROJECT_ROOT / "reports" / "index_quality.json"
    write_index_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_search(
    *,
    query: str,
    limit: int,
    output_format: str,
    book_ids: list[str],
    jurisdictions: list[str],
    subjects: list[str],
    database_path: Path,
) -> int:
    response = search_index(
        database_path=_repo_path(database_path),
        query=query,
        limit=limit,
        book_ids=book_ids,
        jurisdictions=jurisdictions,
        subjects=subjects,
    )
    if output_format == "json":
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        print(render_search_markdown(response), end="")
    return 0


def run_context(
    *,
    query: str,
    limit: int,
    output_format: str,
    book_ids: list[str],
    jurisdictions: list[str],
    subjects: list[str],
    database_path: Path,
) -> int:
    package = build_evidence_package(
        database_path=_repo_path(database_path),
        project_root=PROJECT_ROOT,
        query=query,
        limit=limit,
        book_ids=book_ids,
        jurisdictions=jurisdictions,
        subjects=subjects,
    )
    if output_format == "json":
        print(json.dumps(package, ensure_ascii=False, indent=2))
    else:
        print(render_evidence_markdown(package), end="")
    return 0


def run_verify(
    *,
    output_format: str,
    config_path: Path,
    maps_path: Path,
    database_path: Path,
    evidence_paths: list[Path],
    current_scope: bool = False,
) -> int:
    report = verify_corpus(
        database_path=_repo_path(database_path),
        project_root=PROJECT_ROOT,
        config_path=_repo_path(config_path),
        maps_path=_repo_path(maps_path),
        evidence_paths=[_repo_path(path) for path in evidence_paths],
        scan_saved_evidence=not current_scope,
    )
    report_path = PROJECT_ROOT / "reports" / "verification.json"
    write_verification_report(report_path, report)
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_verification_text(report), end="")
        print(f"报告：{report_path}")
    return 0 if report["ok"] else 1


def run_citation_audit(
    *, database_path: Path, audit_path: Path, output_path: Path
) -> int:
    result = write_citation_acceptance_markdown(
        database_path=_repo_path(database_path),
        audit_path=_repo_path(audit_path),
        output_path=_repo_path(output_path),
    )
    print(
        json.dumps(
            {
                "book_count": result["book_count"],
                "sample_count": result["sample_count"],
                "sample_policy": result["sample_policy"],
                "output_path": result["output_path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run_obsidian_export(
    *,
    query: str,
    limit: int,
    book_ids: list[str],
    jurisdictions: list[str],
    subjects: list[str],
    output_dir: Path,
    filename: str | None,
    database_path: Path,
) -> int:
    result = export_query_to_obsidian(
        database_path=_repo_path(database_path),
        output_dir=output_dir,
        query=query,
        project_root=PROJECT_ROOT,
        vault_root=REPO_ROOT,
        limit=limit,
        book_ids=book_ids,
        jurisdictions=jurisdictions,
        subjects=subjects,
        filename=filename,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run_evaluate(
    *, output_format: str, golden_path: Path, database_path: Path
) -> int:
    report = evaluate_dataset(
        database_path=_repo_path(database_path),
        golden_path=_repo_path(golden_path),
    )
    report_path = PROJECT_ROOT / "reports" / "retrieval_evaluation.json"
    write_evaluation_report(report, report_path)
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_evaluation_text(report), end="")
        print(f"报告：{report_path}")
    if not report["complete"]:
        return 2
    return 0 if report["passed"] else 1


def run_ocr_pilot_command(
    *,
    book_id: str,
    pages: list[int] | None,
    random_count: int | None,
    seed: int,
    dpi: int,
    timeout_seconds: int,
    max_attempts: int,
    force: bool,
    report_path: Path | None,
    config_path: Path,
) -> int:
    configured = load_books(_repo_path(config_path))
    book = next((item for item in configured if item.book_id == book_id), None)
    if book is None:
        raise LegalAIError(f"未知 book_id：{book_id}")
    # OCR pilots are explicitly named and bounded. They do not flip the
    # configured enabled flag or let a scanned book enter normal ingestion.
    from .inventory import inspect_book

    record = inspect_book(book, REPO_ROOT)
    selected_pages = (
        list(pages)
        if pages is not None
        else select_random_pages(
            pdf_page_count=record.actual_pdf_pages,
            count=int(random_count),
            seed=seed,
        )
    )
    report = run_ocr_pilot(
        record=record,
        project_root=PROJECT_ROOT,
        pages=selected_pages,
        dpi=dpi,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        force=force,
    )
    report["selection"] = {
        "method": "explicit_pages" if pages is not None else "uniform_random_without_replacement",
        "seed": None if pages is not None else seed,
        "pages": sorted(set(selected_pages)),
    }
    if report_path is None:
        resolved_report_path = PROJECT_ROOT / "reports" / f"ocr_pilot_{book_id}.json"
    else:
        resolved_report_path = _repo_path(report_path).resolve()
        permitted_reports_root = (PROJECT_ROOT / "reports").resolve()
        try:
            resolved_report_path.relative_to(permitted_reports_root)
        except ValueError as exc:
            raise LegalAIError("OCR 审计报告只能写入 legal_ai/reports/ 目录") from exc
    write_ocr_pilot_report(resolved_report_path, report)
    print(
        json.dumps(
            {
                "book_id": book_id,
                "selection": report["selection"],
                "summary": report["summary"],
                "report_path": str(resolved_report_path),
                "raw_ocr_jsonl": report["raw_ocr_jsonl"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["book"]["source_pdf_unchanged"] else 1


def run_ocr_full_command(
    *,
    book_id: str,
    dpi: int,
    timeout_seconds: int,
    max_attempts: int,
    checkpoint_every: int,
    force: bool,
    exclusions_path: Path,
    report_path: Path | None,
    config_path: Path,
) -> int:
    """Run a hash-bound, full-book OCR job without enabling corpus ingestion."""

    configured = load_books(_repo_path(config_path))
    book = next((item for item in configured if item.book_id == book_id), None)
    if book is None:
        raise LegalAIError(f"Unknown book_id: {book_id}")
    from .inventory import inspect_book

    record = inspect_book(book, REPO_ROOT)
    exclusions = load_ocr_page_exclusions(
        _repo_path(exclusions_path), record=record
    )
    selected_pages = [
        page
        for page in range(1, record.actual_pdf_pages + 1)
        if page not in exclusions
    ]
    if not selected_pages:
        raise LegalAIError("OCR exclusions leave no pages to process")

    def progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    progress_state_path = (
        PROJECT_ROOT / "reports" / "private" / f"ocr_full_{book_id}.progress.json"
    )
    report = run_ocr_pilot(
        record=record,
        project_root=PROJECT_ROOT,
        pages=selected_pages,
        dpi=dpi,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        checkpoint_every=checkpoint_every,
        force=force,
        scope="full_book_ocr",
        registered_skips={page: details["reason"] for page, details in exclusions.items()},
        progress=progress,
        progress_state_path=progress_state_path,
    )
    report["selection"] = {
        "method": "all_pages_except_registered_exclusions",
        "eligible_page_count": len(selected_pages),
        "excluded_pages": [
            {"pdf_page": page, **details}
            for page, details in sorted(exclusions.items())
        ],
    }
    if report_path is None:
        resolved_report_path = PROJECT_ROOT / "reports" / f"ocr_full_{book_id}.json"
    else:
        resolved_report_path = _repo_path(report_path).resolve()
        permitted_reports_root = (PROJECT_ROOT / "reports").resolve()
        try:
            resolved_report_path.relative_to(permitted_reports_root)
        except ValueError as exc:
            raise LegalAIError("OCR audit reports must be written below legal_ai/reports/") from exc
    write_ocr_pilot_report(resolved_report_path, report)
    print(
        json.dumps(
            {
                "book_id": book_id,
                "selection": report["selection"],
                "summary": report["summary"],
                "report_path": str(resolved_report_path),
                "progress_path": str(progress_state_path),
                "raw_ocr_jsonl": report["raw_ocr_jsonl"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["book"]["source_pdf_unchanged"] else 1


def run_ocr_import_command(
    *,
    book_id: str,
    input_path: Path,
    exclusions_path: Path,
    force: bool,
    report_path: Path | None,
    config_path: Path,
) -> int:
    """Import external OCR JSON; the command never initializes an OCR engine."""

    configured = load_books(_repo_path(config_path))
    book = next((item for item in configured if item.book_id == book_id), None)
    if book is None:
        raise LegalAIError(f"Unknown book_id: {book_id}")
    if book.ocr_policy == "off":
        raise LegalAIError(f"{book_id} is not configured to accept OCR")
    from .inventory import inspect_book

    record = inspect_book(book, REPO_ROOT)
    resolved_input = _repo_path(input_path).resolve()
    try:
        resolved_input.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise LegalAIError("External OCR input must remain below the project root") from exc
    exclusions = load_ocr_page_exclusions(
        _repo_path(exclusions_path), record=record
    )
    report = import_pp_ocrv6_json(
        record=record,
        project_root=PROJECT_ROOT,
        input_path=resolved_input,
        exclusions=exclusions,
        force=force,
    )
    if report_path is None:
        resolved_report_path = (
            PROJECT_ROOT / "reports" / f"ocr_import_{book_id}.json"
        )
    else:
        resolved_report_path = _repo_path(report_path).resolve()
        permitted_reports_root = (PROJECT_ROOT / "reports").resolve()
        try:
            resolved_report_path.relative_to(permitted_reports_root)
        except ValueError as exc:
            raise LegalAIError(
                "OCR import audit reports must be written below legal_ai/reports/"
            ) from exc
    write_external_ocr_import_report(resolved_report_path, report)
    print(
        json.dumps(
            {
                "book_id": book_id,
                "summary": report["summary"],
                "input": report["input"],
                "report_path": str(resolved_report_path),
                "raw_ocr_jsonl": report["raw_ocr_jsonl"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["book"]["source_pdf_unchanged"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            return run_inventory(args.format, args.config)
        if args.command == "ingest":
            return run_ingest(args.all, args.book_id, args.config)
        if args.command == "process":
            return run_process(args.all, args.book_id, args.config)
        if args.command == "page-map":
            if args.page_map_command == "audit":
                return run_page_map_audit(
                    output_format=args.format,
                    per_book=args.per_book,
                    seed=args.seed,
                    config_path=args.config,
                    maps_path=args.maps,
                )
            if args.page_map_command == "confirm":
                return run_page_map_confirm(
                    book_id=args.book_id,
                    pdf_page=args.pdf_page,
                    printed_page=args.printed_page,
                    no_visible_page=args.no_visible_page,
                    config_path=args.config,
                    maps_path=args.maps,
                )
        if args.command == "build-index":
            return run_build_index(
                rebuild=args.rebuild,
                config_path=args.config,
                database_path=args.db,
                discard_retrieval_history=args.discard_retrieval_history,
            )
        if args.command == "search":
            return run_search(
                query=args.query,
                limit=args.limit,
                output_format=args.format,
                book_ids=args.book_id,
                jurisdictions=args.jurisdiction,
                subjects=args.subject,
                database_path=args.db,
            )
        if args.command == "context":
            return run_context(
                query=args.query,
                limit=args.limit,
                output_format=args.format,
                book_ids=args.book_id,
                jurisdictions=args.jurisdiction,
                subjects=args.subject,
                database_path=args.db,
            )
        if args.command == "verify":
            return run_verify(
                output_format=args.format,
                config_path=args.config,
                maps_path=args.maps,
                database_path=args.db,
                evidence_paths=args.evidence,
                current_scope=args.current_scope,
            )
        if args.command == "citation-audit":
            return run_citation_audit(
                database_path=args.db,
                audit_path=args.audit,
                output_path=args.output,
            )
        if args.command == "obsidian-export":
            return run_obsidian_export(
                query=args.query,
                limit=args.limit,
                book_ids=args.book_id,
                jurisdictions=args.jurisdiction,
                subjects=args.subject,
                output_dir=args.output_dir,
                filename=args.filename,
                database_path=args.db,
            )
        if args.command == "evaluate":
            return run_evaluate(
                output_format=args.format,
                golden_path=args.golden,
                database_path=args.db,
            )
        if args.command == "ocr-pilot":
            return run_ocr_pilot_command(
                book_id=args.book_id,
                pages=args.pages,
                random_count=args.random_count,
                seed=args.seed,
                dpi=args.dpi,
                timeout_seconds=args.timeout_seconds,
                max_attempts=args.max_attempts,
                force=args.force,
                report_path=args.report,
                config_path=args.config,
            )
        if args.command == "ocr":
            return run_ocr_full_command(
                book_id=args.book_id,
                dpi=args.dpi,
                timeout_seconds=args.timeout_seconds,
                max_attempts=args.max_attempts,
                checkpoint_every=args.checkpoint_every,
                force=args.force,
                exclusions_path=args.exclusions,
                report_path=args.report,
                config_path=args.config,
            )
        if args.command == "ocr-import":
            return run_ocr_import_command(
                book_id=args.book_id,
                input_path=args.input,
                exclusions_path=args.exclusions,
                force=args.force,
                report_path=args.report,
                config_path=args.config,
            )
        if args.command == "legal-source":
            if args.legal_source_command == "ingest":
                return run_legal_source_ingest(
                    law_ids=args.law_id,
                    all_sources=args.all,
                    config_path=args.config,
                    database_path=args.db,
                )
            if args.legal_source_command == "audit":
                return run_legal_source_audit(
                    output_format=args.format, database_path=args.db
                )
            if args.legal_source_command == "show":
                return run_legal_source_show(
                    law_id=args.law_id,
                    article_no=args.article,
                    as_of=args.as_of,
                    output_format=args.format,
                    with_refs=args.refs,
                    database_path=args.db,
                )
        parser.error("未知命令")
    except LegalAIError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from . import PIPELINE_VERSION
from .domain import InventoryRecord, LegalAIError
from .inventory import file_sha256
from .quality import build_quality_report, quality_flags, text_sha256


ProgressCallback = Callable[[str], None]

# 书签标题里可能混入控制字符（扫描版常见 NUL），入库前必须清掉
_DISALLOWED_TITLE_CHARS = {
    chr(code) for code in range(0x20) if chr(code) not in {"\t", "\n", "\r"}
} | {chr(0x7F)}


def _clean_outline_title(text: str) -> str:
    """去掉控制字符并去掉首尾空白。"""
    return "".join(
        ch for ch in text if ch not in _DISALLOWED_TITLE_CHARS
    ).strip()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def _load_checkpoint(
    path: Path,
    *,
    book_id: str,
    source_sha256: str,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for expected_page, line in enumerate(handle, start=1):
                row = json.loads(line)
                if (
                    row["book_id"] != book_id
                    or row["pdf_page"] != expected_page
                    or row["page_id"]
                    != f"{book_id}:p{expected_page:04d}"
                    or row["pipeline_version"] != PIPELINE_VERSION
                    or row["source_pdf_sha256"] != source_sha256
                ):
                    return []
                rows.append(row)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
        return []
    return rows


def extract_outline(reader: PdfReader) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    extraction_error: str | None = None
    warning_messages: list[str] = []

    class OutlineWarningHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warning_messages.append(self.format(record)[:500])

    def walk(items: list[Any], level: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            raw_title = str(getattr(item, "title", "") or "")
            title = _clean_outline_title(raw_title)
            try:
                zero_based_page = reader.get_destination_page_number(item)
                pdf_page = (
                    zero_based_page + 1
                    if isinstance(zero_based_page, int)
                    and zero_based_page >= 0
                    else None
                )
                status = (
                    "valid"
                    if pdf_page is not None
                    and 1 <= pdf_page <= len(reader.pages)
                    else "invalid_page"
                )
            except Exception as exc:
                pdf_page = None
                status = f"unresolved:{type(exc).__name__}"
            entries.append(
                {
                    "level": level,
                    "title": title,
                    "pdf_page": pdf_page,
                    "status": status,
                }
            )

    pypdf_logger = logging.getLogger("pypdf")
    previous_level = pypdf_logger.level
    previous_propagate = pypdf_logger.propagate
    warning_handler = OutlineWarningHandler()
    warning_handler.setLevel(logging.WARNING)
    pypdf_logger.addHandler(warning_handler)
    pypdf_logger.setLevel(logging.WARNING)
    pypdf_logger.propagate = False
    try:
        try:
            outline = reader.outline
            if isinstance(outline, list):
                walk(outline, 1)
        except Exception as exc:
            extraction_error = f"{type(exc).__name__}: {exc}"
    finally:
        pypdf_logger.removeHandler(warning_handler)
        pypdf_logger.setLevel(previous_level)
        pypdf_logger.propagate = previous_propagate

    return {
        "schema_version": 1,
        "entries": entries,
        "extraction_error": extraction_error,
        "warning_messages": list(dict.fromkeys(warning_messages)),
    }


def extract_book(
    record: InventoryRecord,
    project_root: Path,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    book = record.book
    if progress:
        progress(
            f"[{book.book_id}] 开始抽取 {record.actual_pdf_pages} 页"
        )
    source_sha256_before = file_sha256(record.absolute_path)
    if source_sha256_before != record.sha256:
        raise LegalAIError(f"{book.book_id}：抽取前文件哈希发生变化")

    reader = PdfReader(record.absolute_path)
    if len(reader.pages) != book.expected_pdf_pages:
        raise LegalAIError(f"{book.book_id}：抽取前页数复核失败")

    raw_path = (
        project_root / "data" / "raw" / "pages" / f"{book.book_id}.jsonl"
    )
    partial_path = raw_path.with_suffix(raw_path.suffix + ".partial")
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    pages = _load_checkpoint(
        partial_path,
        book_id=book.book_id,
        source_sha256=source_sha256_before,
    )
    if pages and progress:
        progress(
            f"[{book.book_id}] 从断点续跑，已有 "
            f"{len(pages)}/{record.actual_pdf_pages} 页"
        )
    mode = "a" if pages else "w"
    partial_handle = partial_path.open(
        mode, encoding="utf-8", newline="\n"
    )
    for index, page in enumerate(reader.pages, start=1):
        if index <= len(pages):
            continue
        raw_text: str | None
        extraction_error: str | None
        try:
            extracted = page.extract_text()
            raw_text = extracted if extracted is not None else ""
            extraction_error = None
        except Exception as exc:
            raw_text = None
            extraction_error = f"{type(exc).__name__}: {exc}"
        row = {
            "page_id": f"{book.book_id}:p{index:04d}",
            "book_id": book.book_id,
            "pdf_page": index,
            "raw_text": raw_text,
            "raw_text_sha256": (
                text_sha256(raw_text) if raw_text is not None else None
            ),
            "extraction_error": extraction_error,
            "quality_flags": quality_flags(raw_text, extraction_error),
            "pipeline_version": PIPELINE_VERSION,
            "source_pdf_sha256": source_sha256_before,
        }
        pages.append(row)
        partial_handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        partial_handle.flush()
        if progress and (index == 1 or index % 100 == 0):
            progress(
                f"[{book.book_id}] 已抽取 {index}/"
                f"{record.actual_pdf_pages} 页"
            )
    partial_handle.close()
    if len(pages) != record.actual_pdf_pages:
        raise LegalAIError(
            f"{book.book_id}：断点页面数异常，"
            f"得到 {len(pages)}，预期 {record.actual_pdf_pages}"
        )

    outline = extract_outline(reader)
    outline.update(
        {
            "book_id": book.book_id,
            "title": book.title,
            "source_sha256": source_sha256_before,
        }
    )
    source_sha256_after = file_sha256(record.absolute_path)
    if source_sha256_before != source_sha256_after:
        raise LegalAIError(f"{book.book_id}：原始 PDF 在抽取期间发生变化")

    outline_path = (
        project_root
        / "data"
        / "raw"
        / "outlines"
        / f"{book.book_id}.json"
    )
    os.replace(partial_path, raw_path)
    _atomic_write_json(outline_path, outline)

    report = build_quality_report(
        book_id=book.book_id,
        title=book.title,
        file_path=book.file_path,
        source_sha256_before=source_sha256_before,
        source_sha256_after=source_sha256_after,
        pages=pages,
        outline=outline,
        pipeline_version=PIPELINE_VERSION,
    )
    if report["page_record_count"] != record.actual_pdf_pages:
        raise LegalAIError(f"{book.book_id}：页面记录数与 PDF 页数不一致")
    if progress:
        progress(
            f"[{book.book_id}] 完成；空页 "
            f"{report['empty_page_count']}，抽取错误 "
            f"{report['extraction_error_count']}"
        )
    return report


def merge_quality_reports(
    reports: list[dict[str, Any]], aggregate_path: Path, *, replace: bool = False
) -> dict[str, Any]:
    existing_by_id: dict[str, dict[str, Any]] = {}
    if not replace and aggregate_path.exists():
        try:
            existing = json.loads(
                aggregate_path.read_text(encoding="utf-8")
            )
            existing_by_id = {
                item["book_id"]: item for item in existing.get("books", [])
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            existing_by_id = {}
    for report in reports:
        existing_by_id[report["book_id"]] = report
    books = [existing_by_id[key] for key in sorted(existing_by_id)]
    for book in books:
        book.setdefault("outline_warning_count", 0)
        book.setdefault("outline_warning_messages", [])
    aggregate = {
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "book_count": len(books),
        "total_pdf_pages": sum(item["pdf_page_count"] for item in books),
        "total_page_records": sum(
            item["page_record_count"] for item in books
        ),
        "total_extraction_errors": sum(
            item["extraction_error_count"] for item in books
        ),
        "all_source_pdfs_unchanged": all(
            item["source_pdf_unchanged"] for item in books
        ),
        "books": books,
    }
    _atomic_write_json(aggregate_path, aggregate)
    return aggregate

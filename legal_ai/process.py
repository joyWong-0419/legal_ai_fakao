from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from . import PIPELINE_VERSION
from .domain import (
    DEFAULT_PAGE_MAPS_PATH,
    InventoryRecord,
    LegalAIError,
    TEXT_SOURCES,
    TEXT_VERIFICATION_STATUSES,
)
from .extract import _atomic_write_json
from .normalize import normalize_page_text
from .ocr import OCR_STATUSES, load_ocr_pages, ocr_pages_path
from .ocr_exclusions import load_ocr_page_exclusions
from .page_map import (
    BookPageMap,
    load_page_maps,
    page_map_by_book,
    resolve_page,
)
from .segment import (
    SEGMENTATION_VERSION,
    build_chunk_records,
    build_sentence_records,
)


PROCESSING_SCHEMA_VERSION = 2


def _needs_ocr(raw_page: dict[str, Any], *, all_pages: bool) -> bool:
    if all_pages:
        return True
    if raw_page.get("extraction_error"):
        return True
    flags = {str(item) for item in raw_page.get("quality_flags", [])}
    return bool(
        flags
        & {
            "empty",
            "near_empty",
            "replacement_character",
            "suspicious_characters",
        }
    )


def select_canonical_page(
    *,
    record: InventoryRecord,
    raw_page: dict[str, Any],
    ocr_page: dict[str, Any] | None,
    page_exclusion: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Choose a page's searchable text without overwriting pypdf evidence.

    `raw_text` always remains in `data/raw/pages`.  This function only selects
    a separate canonical source for normalization, sentence IDs and chunks.
    Stage B intentionally never creates `hybrid`; the value is reserved for a
    later, auditable page-level merge policy.
    """

    if page_exclusion is not None:
        reason = str(page_exclusion.get("reason") or "").strip()
        disposition = str(page_exclusion.get("disposition") or "").strip()
        if not reason or disposition not in {"frontmatter", "blank"}:
            raise LegalAIError(
                f"{raw_page.get('page_id')}：无效的 OCR 页面排除登记"
            )
        return {
            "selected_text": "",
            "text_source": "empty",
            "text_verification_status": "embedded_text",
            "ocr_status": "skipped",
            "ocr_engine": None,
            "ocr_avg_confidence": None,
            "ocr_text_sha256": None,
            "ocr_skip_reason": reason,
            "ocr_skip_disposition": disposition,
        }

    book = record.book
    raw_text_value = raw_page.get("raw_text")
    embedded_text = "" if raw_text_value is None else str(raw_text_value)
    embedded_available = bool(embedded_text.strip()) and not raw_page.get(
        "extraction_error"
    )
    needs_ocr = _needs_ocr(
        raw_page, all_pages=book.ocr_policy == "all_pages"
    )
    ocr_status = (
        str(ocr_page["ocr_status"])
        if ocr_page is not None
        else (
            "not_required"
            if book.ocr_policy == "off" or not needs_ocr
            else "pending"
        )
    )
    if ocr_status not in OCR_STATUSES:
        raise LegalAIError(f"{raw_page.get('page_id')}：未知 OCR 状态 {ocr_status}")
    ocr_generated = ocr_page is not None and ocr_status == "generated"

    use_ocr = False
    if book.text_layer_policy == "ignore":
        use_ocr = ocr_generated
    elif book.text_layer_policy == "auto":
        use_ocr = bool(ocr_generated and (needs_ocr or not embedded_available))
    elif book.text_layer_policy == "embedded_only":
        use_ocr = False
    else:  # BookConfig validates this, but keep process files self-defending.
        raise LegalAIError(
            f"{raw_page.get('page_id')}：未知 text_layer_policy "
            f"{book.text_layer_policy}"
        )

    if use_ocr:
        assert ocr_page is not None
        selected_text = str(ocr_page["ocr_text"])
        text_source = "ocr"
        text_verification_status = "ocr_unverified"
    elif embedded_available:
        selected_text = embedded_text
        text_source = "embedded_text"
        text_verification_status = "embedded_text"
    else:
        selected_text = ""
        text_source = "empty"
        # Empty pages never generate evidence.  The value records that there
        # is no OCR claim to verify while staying inside the v2 status enum.
        text_verification_status = "embedded_text"

    if text_source not in TEXT_SOURCES:
        raise LegalAIError(f"{raw_page.get('page_id')}：未知 text_source {text_source}")
    if text_verification_status not in TEXT_VERIFICATION_STATUSES:
        raise LegalAIError(
            f"{raw_page.get('page_id')}：未知 text_verification_status"
        )
    return {
        "selected_text": selected_text,
        "text_source": text_source,
        "text_verification_status": text_verification_status,
        "ocr_status": ocr_status,
        "ocr_engine": ocr_page.get("ocr_engine") if ocr_page else None,
        "ocr_avg_confidence": (
            ocr_page.get("ocr_avg_confidence") if ocr_page else None
        ),
        "ocr_text_sha256": (
            ocr_page.get("ocr_text_sha256") if ocr_page else None
        ),
        "ocr_skip_reason": None,
        "ocr_skip_disposition": None,
    }


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LegalAIError(
                    f"{path} 第 {line_number} 行不是合法 JSON"
                ) from exc
    return rows


def build_chapter_paths(
    outline: dict[str, Any], total_pages: int
) -> dict[int, list[str]]:
    by_page: dict[int, list[dict[str, Any]]] = {}
    for order, entry in enumerate(outline.get("entries", [])):
        pdf_page = entry.get("pdf_page")
        if (
            entry.get("status") == "valid"
            and isinstance(pdf_page, int)
            and 1 <= pdf_page <= total_pages
        ):
            normalized = dict(entry)
            normalized["_order"] = order
            by_page.setdefault(pdf_page, []).append(normalized)

    chapter_paths: dict[int, list[str]] = {}
    active: list[str] = []
    for page_number in range(1, total_pages + 1):
        entries = sorted(
            by_page.get(page_number, []),
            key=lambda item: (int(item.get("level") or 1), item["_order"]),
        )
        for entry in entries:
            level = max(1, int(entry.get("level") or 1))
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            active = active[: level - 1]
            active.append(title)
        chapter_paths[page_number] = list(active)
    return chapter_paths


BOOKMARK_OVERRIDES_DIR = "config/bookmark_overrides"


def load_bookmark_overrides(
    project_root: Path, book_id: str
) -> list[dict[str, Any]]:
    """读取某本书的书签修复映射；没有配置就返回空列表。"""
    path = project_root / BOOKMARK_OVERRIDES_DIR / f"{book_id}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"{book_id}：书签映射不是合法 JSON：{exc}") from exc
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise LegalAIError(f"{book_id}：书签映射缺少 entries 数组")
    return [entry for entry in entries if isinstance(entry, dict)]


def apply_bookmark_overrides(
    outline: dict[str, Any], overrides: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按 (pdf_page, level, 乱码原文) 三项精确匹配替换书签标题。

    三项都匹配才替换：pypdf 升级后乱码串一旦变化，宁可失效告警，
    也不要误改到别的条目。raw 书签文件保持原样，修复只作用于内存副本。
    """
    remaining: dict[tuple[int, int, str], str] = {}
    for item in overrides:
        try:
            key = (
                int(item["pdf_page"]),
                int(item["level"]),
                str(item["garbled"]),
            )
            title = str(item["title"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LegalAIError(f"书签映射条目不完整：{item!r}") from exc
        remaining[key] = title

    stats: dict[str, Any] = {
        "applied": 0,
        "unused": 0,
        "unused_titles": [],
        "total": len(remaining),
    }
    if not remaining:
        return outline, stats

    entries: list[dict[str, Any]] = []
    for entry in outline.get("entries", []):
        patched = dict(entry)
        try:
            key = (
                int(entry.get("pdf_page")),
                int(entry.get("level") or 1),
                str(entry.get("title") or ""),
            )
        except (TypeError, ValueError):
            entries.append(patched)
            continue
        replacement = remaining.pop(key, None)
        if replacement is not None:
            patched["title"] = replacement
            stats["applied"] += 1
        entries.append(patched)

    patched_outline = dict(outline)
    patched_outline["entries"] = entries
    stats["unused"] = len(remaining)
    stats["unused_titles"] = [key[2] for key in remaining]
    return patched_outline, stats


def process_book(
    record: InventoryRecord,
    project_root: Path,
    book_page_map: BookPageMap | None = None,
) -> dict[str, Any]:
    book_id = record.book.book_id
    raw_pages_path = (
        project_root / "data" / "raw" / "pages" / f"{book_id}.jsonl"
    )
    outline_path = (
        project_root / "data" / "raw" / "outlines" / f"{book_id}.json"
    )
    if not raw_pages_path.exists():
        raise LegalAIError(
            f"{book_id}：缺少逐页抽取文件，请先运行 ingest"
        )
    if not outline_path.exists():
        raise LegalAIError(f"{book_id}：缺少 PDF 书签文件，请先运行 ingest")

    raw_pages = load_jsonl(raw_pages_path)
    if len(raw_pages) != record.actual_pdf_pages:
        raise LegalAIError(
            f"{book_id}：raw 页数 {len(raw_pages)} 与 PDF 页数 "
            f"{record.actual_pdf_pages} 不一致"
        )
    outline = json.loads(outline_path.read_text(encoding="utf-8"))
    outline, bookmark_override_stats = apply_bookmark_overrides(
        outline, load_bookmark_overrides(project_root, book_id)
    )
    ocr_records = load_ocr_pages(
        ocr_pages_path(project_root, book_id),
        book_id=book_id,
        source_pdf_sha256=record.sha256,
        pdf_page_count=record.actual_pdf_pages,
    ) if record.book.ocr_policy != "off" else {}
    exclusions_path = project_root / "config" / "ocr_page_exclusions.json"
    ocr_page_exclusions = (
        load_ocr_page_exclusions(exclusions_path, record=record)
        if exclusions_path.exists()
        else {}
    )
    chapter_paths = build_chapter_paths(outline, len(raw_pages))
    if book_page_map is None:
        configured_maps = page_map_by_book(load_page_maps(DEFAULT_PAGE_MAPS_PATH))
        book_page_map = configured_maps.get(book_id)
    if book_page_map is not None:
        for mapping_segment in book_page_map.segments:
            if mapping_segment.pdf_end > record.actual_pdf_pages:
                raise LegalAIError(
                    f"{book_id}：页码映射结束页 {mapping_segment.pdf_end} "
                    f"超出 PDF 页数 {record.actual_pdf_pages}"
                )

    processed_pages: list[dict[str, Any]] = []
    sentence_rows: list[dict[str, Any]] = []
    chunk_rows: list[dict[str, Any]] = []
    skipped_pages: list[int] = []
    registered_excluded_pages: list[int] = []
    text_source_counts = {source: 0 for source in sorted(TEXT_SOURCES)}
    ocr_status_counts = {status: 0 for status in sorted(OCR_STATUSES)}

    for expected_page, page in enumerate(raw_pages, start=1):
        if page.get("pdf_page") != expected_page:
            raise LegalAIError(
                f"{book_id}：raw 页序异常，预期 {expected_page}，"
                f"得到 {page.get('pdf_page')}"
            )
        canonical = select_canonical_page(
            record=record,
            raw_page=page,
            ocr_page=ocr_records.get(expected_page),
            page_exclusion=ocr_page_exclusions.get(expected_page),
        )
        normalized = normalize_page_text(canonical["selected_text"])
        chapter_path = chapter_paths.get(expected_page, [])
        printed_page = resolve_page(book_page_map, expected_page)
        processed_page = {
            "page_id": page["page_id"],
            "book_id": book_id,
            "pdf_page": expected_page,
            "printed_page": printed_page.printed_page,
            "printed_page_status": printed_page.status,
            "raw_text_sha256": page.get("raw_text_sha256"),
            "text_source": canonical["text_source"],
            "text_verification_status": canonical[
                "text_verification_status"
            ],
            "ocr_status": canonical["ocr_status"],
            "ocr_engine": canonical["ocr_engine"],
            "ocr_avg_confidence": canonical["ocr_avg_confidence"],
            "ocr_text_sha256": canonical["ocr_text_sha256"],
            "normalized_text": normalized.text,
            "char_count": len(normalized.text),
            "quality_flags": list(page.get("quality_flags", []))
            + (
                [f"ocr_excluded_{canonical['ocr_skip_disposition']}"]
                if canonical["ocr_skip_disposition"] is not None
                else []
            ),
            "extraction_error": page.get("extraction_error"),
            "ocr_skip_reason": canonical["ocr_skip_reason"],
            "chapter_path": chapter_path,
            "normalization_version": normalized.version,
            "pipeline_version": PIPELINE_VERSION,
        }
        processed_pages.append(processed_page)
        text_source_counts[str(canonical["text_source"])] += 1
        ocr_status_counts[str(canonical["ocr_status"])] += 1

        if canonical["text_source"] == "empty":
            skipped_pages.append(expected_page)
            if canonical["ocr_skip_reason"] is not None:
                registered_excluded_pages.append(expected_page)
            continue
        raw_text_for_slice = str(canonical["selected_text"])
        page_sentences = build_sentence_records(
            book_id=book_id,
            page_id=page["page_id"],
            pdf_page=expected_page,
            normalized_text=normalized.text,
            raw_text=raw_text_for_slice,
            raw_slice=normalized.raw_slice_for_normalized_span,
            chapter_path=chapter_path,
            normalization_version=normalized.version,
        )
        sentence_rows.extend(page_sentences)
        chunk_rows.extend(
            build_chunk_records(
                book_id=book_id,
                page_id=page["page_id"],
                pdf_page=expected_page,
                sentences=page_sentences,
                chapter_path=chapter_path,
            )
        )

    base = project_root / "data" / "processed"
    _atomic_write_jsonl(base / "pages" / f"{book_id}.jsonl", processed_pages)
    _atomic_write_jsonl(
        base / "sentences" / f"{book_id}.jsonl", sentence_rows
    )
    _atomic_write_jsonl(base / "chunks" / f"{book_id}.jsonl", chunk_rows)

    sentence_ids = {row["sentence_id"] for row in sentence_rows}
    invalid_chunks = [
        row["chunk_id"]
        for row in chunk_rows
        if row["first_sentence_id"] not in sentence_ids
        or row["last_sentence_id"] not in sentence_ids
    ]
    if invalid_chunks:
        raise LegalAIError(
            f"{book_id}：chunk 引用不存在的句子：{invalid_chunks[:3]}"
        )

    return {
        "schema_version": PROCESSING_SCHEMA_VERSION,
        "book_id": book_id,
        "title": record.book.title,
        "pdf_page_count": len(raw_pages),
        "processed_page_count": len(processed_pages),
        "sentence_count": len(sentence_rows),
        "chunk_count": len(chunk_rows),
        "skipped_page_count": len(skipped_pages),
        "skipped_pages": skipped_pages,
        "registered_excluded_pages": registered_excluded_pages,
        "text_source_counts": text_source_counts,
        "ocr_status_counts": ocr_status_counts,
        "chapter_mapped_page_count": sum(
            1 for item in chapter_paths.values() if item
        ),
        "bookmark_overrides": bookmark_override_stats,
        "printed_page_status_counts": {
            status: sum(
                1
                for item in processed_pages
                if item["printed_page_status"] == status
            )
            for status in ("unknown", "detected", "verified", "rejected")
        },
        "normalization_version": processed_pages[0][
            "normalization_version"
        ]
        if processed_pages
        else "norm-v1",
        "segmentation_version": SEGMENTATION_VERSION,
        "pipeline_version": PIPELINE_VERSION,
    }


def merge_processing_reports(
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
    aggregate = {
        "schema_version": PROCESSING_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "book_count": len(books),
        "total_pdf_pages": sum(item["pdf_page_count"] for item in books),
        "total_processed_pages": sum(
            item["processed_page_count"] for item in books
        ),
        "total_sentences": sum(item["sentence_count"] for item in books),
        "total_chunks": sum(item["chunk_count"] for item in books),
        "total_skipped_pages": sum(
            item["skipped_page_count"] for item in books
        ),
        "text_source_totals": {
            source: sum(
                item.get("text_source_counts", {}).get(source, 0)
                for item in books
            )
            for source in sorted(TEXT_SOURCES)
        },
        "ocr_status_totals": {
            status: sum(
                item.get("ocr_status_counts", {}).get(status, 0)
                for item in books
            )
            for status in sorted(OCR_STATUSES)
        },
        "printed_page_status_totals": {
            status: sum(
                item.get("printed_page_status_counts", {}).get(status, 0)
                for item in books
            )
            for status in ("unknown", "detected", "verified", "rejected")
        },
        "books": books,
    }
    _atomic_write_json(aggregate_path, aggregate)
    return aggregate

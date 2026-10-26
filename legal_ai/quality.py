from __future__ import annotations

import hashlib
import statistics
import unicodedata
from typing import Any, Iterable


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def quality_flags(text: str | None, extraction_error: str | None) -> list[str]:
    if extraction_error:
        return ["extraction_error"]
    assert text is not None
    flags: list[str] = []
    stripped = text.strip()
    if not stripped:
        flags.append("empty")
    elif len(stripped) < 20:
        flags.append("near_empty")
    if "\ufffd" in text:
        flags.append("replacement_character")
    suspicious = suspicious_character_count(text)
    if text and suspicious / len(text) > 0.01:
        flags.append("suspicious_characters")
    return flags


def suspicious_character_count(text: str) -> int:
    count = 0
    for character in text:
        category = unicodedata.category(character)
        if character == "\ufffd":
            count += 1
        elif category in {"Co", "Cs"}:
            count += 1
        elif category == "Cc" and character not in "\n\r\t":
            count += 1
    return count


def sample_page_numbers(total_pages: int) -> list[int]:
    candidates = [
        1,
        2,
        3,
        max(1, total_pages // 2),
        max(1, total_pages // 2 + 1),
        min(total_pages, total_pages // 2 + 2),
        max(1, total_pages - 2),
        max(1, total_pages - 1),
        total_pages,
    ]
    return list(dict.fromkeys(candidates))


def build_quality_report(
    *,
    book_id: str,
    title: str,
    file_path: str,
    source_sha256_before: str,
    source_sha256_after: str,
    pages: list[dict[str, Any]],
    outline: dict[str, Any],
    pipeline_version: str,
) -> dict[str, Any]:
    char_counts = [
        len((page["raw_text"] or "").strip())
        for page in pages
        if not page["extraction_error"]
    ]
    body_counts = [count for count in char_counts if count >= 100]
    total_chars = sum(char_counts)
    replacement_count = sum(
        (page["raw_text"] or "").count("\ufffd") for page in pages
    )
    suspicious_count = sum(
        suspicious_character_count(page["raw_text"] or "")
        for page in pages
    )
    empty_pages = [
        page["pdf_page"]
        for page in pages
        if "empty" in page["quality_flags"]
    ]
    near_empty_pages = [
        page["pdf_page"]
        for page in pages
        if "near_empty" in page["quality_flags"]
    ]
    error_pages = [
        page["pdf_page"] for page in pages if page["extraction_error"]
    ]
    replacement_pages = [
        page["pdf_page"]
        for page in pages
        if "replacement_character" in page["quality_flags"]
    ]
    suspicious_pages = [
        page["pdf_page"]
        for page in pages
        if suspicious_character_count(page["raw_text"] or "") > 0
    ]

    duplicate_pairs: list[list[int]] = []
    previous: dict[str, Any] | None = None
    for page in pages:
        if (
            previous
            and page["raw_text_sha256"]
            and page["raw_text_sha256"] == previous["raw_text_sha256"]
            and (page["raw_text"] or "").strip()
        ):
            duplicate_pairs.append(
                [previous["pdf_page"], page["pdf_page"]]
            )
        previous = page

    samples = []
    sample_numbers = set(sample_page_numbers(len(pages)))
    for page in pages:
        if page["pdf_page"] in sample_numbers:
            samples.append(
                {
                    "pdf_page": page["pdf_page"],
                    "char_count": len((page["raw_text"] or "").strip()),
                    "quality_flags": page["quality_flags"],
                    "extraction_error": page["extraction_error"],
                }
            )

    outline_entries = outline.get("entries", [])
    invalid_outline_entries = [
        entry for entry in outline_entries if entry["status"] != "valid"
    ]
    return {
        "schema_version": 1,
        "pipeline_version": pipeline_version,
        "book_id": book_id,
        "title": title,
        "file_path": file_path,
        "pdf_page_count": len(pages),
        "page_record_count": len(pages),
        "source_sha256_before": source_sha256_before,
        "source_sha256_after": source_sha256_after,
        "source_pdf_unchanged": (
            source_sha256_before == source_sha256_after
        ),
        "empty_page_count": len(empty_pages),
        "empty_page_ratio": (
            len(empty_pages) / len(pages) if pages else 0.0
        ),
        "empty_pages": empty_pages,
        "near_empty_page_count": len(near_empty_pages),
        "near_empty_pages": near_empty_pages,
        "body_page_char_count_median": (
            statistics.median(body_counts) if body_counts else 0
        ),
        "replacement_character_count": replacement_count,
        "replacement_character_ratio": (
            replacement_count / total_chars if total_chars else 0.0
        ),
        "replacement_character_pages": replacement_pages,
        "suspicious_character_count": suspicious_count,
        "suspicious_character_ratio": (
            suspicious_count / total_chars if total_chars else 0.0
        ),
        "suspicious_character_pages": suspicious_pages,
        "extraction_error_count": len(error_pages),
        "extraction_error_pages": error_pages,
        "consecutive_duplicate_count": len(duplicate_pairs),
        "consecutive_duplicate_page_pairs": duplicate_pairs,
        "outline_entry_count": len(outline_entries),
        "outline_invalid_entry_count": len(invalid_outline_entries),
        "outline_invalid_entries": invalid_outline_entries,
        "outline_extraction_error": outline.get("extraction_error"),
        "outline_warning_count": len(
            outline.get("warning_messages", [])
        ),
        "outline_warning_messages": outline.get("warning_messages", []),
        "sample_pages": samples,
    }

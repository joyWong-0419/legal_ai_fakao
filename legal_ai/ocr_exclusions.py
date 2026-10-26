from __future__ import annotations

"""Source-bound exclusions for pages that must not become legal-text OCR."""

import json
from pathlib import Path
from typing import Mapping

from .domain import InventoryRecord, LegalAIError


OCR_EXCLUSION_SCHEMA_VERSION = "ocr-page-exclusions-v1"


def load_ocr_page_exclusions(
    path: Path, *, record: InventoryRecord
) -> dict[int, dict[str, str]]:
    """Load validated exclusions for one book and its exact source PDF hash."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LegalAIError(f"OCR exclusion registry is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"OCR exclusion registry is not valid JSON: {path}") from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != OCR_EXCLUSION_SCHEMA_VERSION
    ):
        raise LegalAIError("OCR exclusion registry schema is invalid")
    books = document.get("books")
    if not isinstance(books, Mapping):
        raise LegalAIError("OCR exclusion registry books map is invalid")
    configured = books.get(record.book.book_id, {})
    if not isinstance(configured, Mapping):
        raise LegalAIError("OCR exclusion book configuration is invalid")
    if configured and configured.get("source_pdf_sha256") != record.sha256:
        raise LegalAIError("OCR exclusion registry does not match the current source PDF")
    raw_pages = configured.get("pages", {})
    if not isinstance(raw_pages, Mapping):
        raise LegalAIError("OCR exclusion pages map is invalid")

    exclusions: dict[int, dict[str, str]] = {}
    for raw_page, raw_details in raw_pages.items():
        try:
            page = int(raw_page)
        except (TypeError, ValueError) as exc:
            raise LegalAIError("OCR exclusion page keys must be integers") from exc
        if str(page) != str(raw_page) or not 1 <= page <= record.actual_pdf_pages:
            raise LegalAIError("OCR exclusion page is outside the source PDF")
        if not isinstance(raw_details, Mapping):
            raise LegalAIError("OCR exclusion details must be an object")
        disposition = raw_details.get("disposition")
        reason = raw_details.get("reason")
        if (
            disposition not in {"frontmatter", "blank"}
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise LegalAIError("OCR exclusion requires a disposition and non-empty reason")
        exclusions[page] = {
            "disposition": str(disposition),
            "reason": reason.strip(),
        }
    return exclusions

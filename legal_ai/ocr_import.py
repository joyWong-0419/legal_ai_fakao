from __future__ import annotations

"""Import page-bounded OCR JSON without loading or invoking an OCR engine."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .domain import InventoryRecord, LegalAIError
from .inventory import file_sha256
from .ocr import ocr_pages_path, write_ocr_pages


EXTERNAL_OCR_IMPORT_SCHEMA_VERSION = "external-ocr-import-v1"
PP_OCRV6_ENGINE = "paddleocr"
PP_OCRV6_ENGINE_VERSION = "external-pp-ocrv6"
PP_OCRV6_MODEL_ID = "PP-OCRv6"
PP_OCRV6_PROFILE = "external_pp_ocrv6_json_v1"


def _skipped_row(
    *,
    record: InventoryRecord,
    pdf_page: int,
    profile: str,
    reason: str,
    external_json_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": "ocr-page-v1",
        "page_id": f"{record.book.book_id}:p{pdf_page:04d}",
        "book_id": record.book.book_id,
        "pdf_page": pdf_page,
        "source_pdf_sha256": record.sha256,
        "ocr_status": "skipped",
        "ocr_engine": None,
        "ocr_engine_version": None,
        "ocr_model_id": None,
        # The external JSON does not state a source rendering DPI.  Null is
        # intentional: importing must not invent a rendering provenance claim.
        "render_dpi": None,
        "preprocess_profile": profile,
        "ocr_text": None,
        "ocr_text_sha256": None,
        "ocr_avg_confidence": None,
        "blocks": [],
        "error": None,
        "skip_reason": reason,
        "external_ocr_json_sha256": external_json_sha256,
    }


def _failed_row(
    *,
    record: InventoryRecord,
    pdf_page: int,
    profile: str,
    external_json_sha256: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "schema_version": "ocr-page-v1",
        "page_id": f"{record.book.book_id}:p{pdf_page:04d}",
        "book_id": record.book.book_id,
        "pdf_page": pdf_page,
        "source_pdf_sha256": record.sha256,
        "ocr_status": "failed",
        "ocr_engine": PP_OCRV6_ENGINE,
        "ocr_engine_version": PP_OCRV6_ENGINE_VERSION,
        "ocr_model_id": PP_OCRV6_MODEL_ID,
        "render_dpi": None,
        "preprocess_profile": profile,
        "ocr_text": None,
        "ocr_text_sha256": None,
        "ocr_avg_confidence": None,
        "blocks": [],
        "error": {"code": code, "message": message[:500]},
        "skip_reason": None,
        "external_ocr_json_sha256": external_json_sha256,
    }


def _generated_row(
    *,
    record: InventoryRecord,
    pdf_page: int,
    profile: str,
    external_json_sha256: str,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    text = "\n".join(str(block["text"]) for block in blocks)
    return {
        "schema_version": "ocr-page-v1",
        "page_id": f"{record.book.book_id}:p{pdf_page:04d}",
        "book_id": record.book.book_id,
        "pdf_page": pdf_page,
        "source_pdf_sha256": record.sha256,
        "ocr_status": "generated",
        "ocr_engine": PP_OCRV6_ENGINE,
        "ocr_engine_version": PP_OCRV6_ENGINE_VERSION,
        "ocr_model_id": PP_OCRV6_MODEL_ID,
        "render_dpi": None,
        "preprocess_profile": profile,
        "ocr_text": text,
        "ocr_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "ocr_avg_confidence": sum(float(block["confidence"]) for block in blocks)
        / len(blocks),
        "blocks": blocks,
        "error": None,
        "skip_reason": None,
        "external_ocr_json_sha256": external_json_sha256,
    }


def _required_list(page: Mapping[str, Any], field: str, *, pdf_page: int) -> list[Any]:
    value = page.get(field)
    if not isinstance(value, list):
        raise LegalAIError(f"External OCR page {pdf_page} field {field} must be an array")
    return value


def _coerce_bbox(value: Any) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        coordinates = [int(float(item)) for item in value]
    except (TypeError, ValueError, OverflowError):
        return None
    if coordinates[0] >= coordinates[2] or coordinates[1] >= coordinates[3]:
        return None
    return coordinates


def _valid_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return score if 0.0 < score <= 1.0 else None


def _external_page_blocks(
    page: Mapping[str, Any], *, pdf_page: int, page_id: str
) -> tuple[int, list[dict[str, Any]], int]:
    """Return source count, valid ordered blocks, and discarded-block count."""

    pruned = page.get("prunedResult")
    if not isinstance(pruned, Mapping):
        raise LegalAIError(f"External OCR page {pdf_page} lacks object prunedResult")
    texts = _required_list(pruned, "rec_texts", pdf_page=pdf_page)
    scores = _required_list(pruned, "rec_scores", pdf_page=pdf_page)
    boxes = _required_list(pruned, "rec_boxes", pdf_page=pdf_page)
    polys = _required_list(pruned, "rec_polys", pdf_page=pdf_page)
    if not (len(texts) == len(scores) == len(boxes) == len(polys)):
        raise LegalAIError(
            f"External OCR page {pdf_page} has mismatched text/score/box/poly arrays"
        )

    blocks: list[dict[str, Any]] = []
    discarded = 0
    for text_value, score_value, box_value in zip(texts, scores, boxes):
        text = str(text_value).strip()
        score = _valid_score(score_value)
        bbox = _coerce_bbox(box_value)
        if not text or score is None or bbox is None:
            discarded += 1
            continue
        position = len(blocks) + 1
        blocks.append(
            {
                "block_id": f"{page_id}:b{position:04d}",
                "text": text,
                "bbox": bbox,
                "confidence": score,
                # PP-OCR's result arrays are its exported reading order.  This
                # adapter preserves that order instead of guessing a new layout.
                "reading_order": position,
                "role": "body",
            }
        )
    return len(texts), blocks, discarded


def import_pp_ocrv6_json(
    *,
    record: InventoryRecord,
    project_root: Path,
    input_path: Path,
    exclusions: Mapping[int, Mapping[str, str]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Convert PP-OCRv6 page JSON to validated raw OCR JSONL.

    The function is intentionally an import adapter: it never constructs an
    OCR engine, follows image URLs, or renders a PDF.
    """

    resolved_input = input_path.resolve()
    if not resolved_input.is_file():
        raise LegalAIError(f"External OCR JSON is missing: {input_path}")
    output_path = ocr_pages_path(project_root, record.book.book_id)
    if output_path.exists() and not force:
        raise LegalAIError(
            f"{record.book.book_id} already has raw OCR; use --force to replace it"
        )
    try:
        document = json.loads(resolved_input.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"External OCR JSON is invalid: {resolved_input}") from exc
    if not isinstance(document, list):
        raise LegalAIError("External OCR JSON root must be a page-result array")
    if len(document) != record.actual_pdf_pages:
        raise LegalAIError(
            f"External OCR page count {len(document)} does not match source PDF "
            f"page count {record.actual_pdf_pages}"
        )
    if any(not isinstance(page, Mapping) for page in document):
        raise LegalAIError("External OCR JSON contains a non-object page result")

    profile = record.book.ocr_profile or PP_OCRV6_PROFILE
    external_json_sha256 = file_sha256(resolved_input)
    registered_exclusions = dict(exclusions or {})
    rows: list[dict[str, Any]] = []
    empty_source_pages: list[int] = []
    failed_pages: list[int] = []
    excluded_pages: list[int] = []
    discarded_block_count = 0
    pages_with_discarded_blocks: list[int] = []

    for pdf_page, source_page in enumerate(document, start=1):
        page_id = f"{record.book.book_id}:p{pdf_page:04d}"
        exclusion = registered_exclusions.get(pdf_page)
        if exclusion is not None:
            reason = str(exclusion.get("reason") or "").strip()
            if not reason:
                raise LegalAIError(f"External OCR exclusion page {pdf_page} lacks a reason")
            rows.append(
                _skipped_row(
                    record=record,
                    pdf_page=pdf_page,
                    profile=profile,
                    reason=reason,
                    external_json_sha256=external_json_sha256,
                )
            )
            excluded_pages.append(pdf_page)
            continue

        source_count, blocks, discarded = _external_page_blocks(
            source_page, pdf_page=pdf_page, page_id=page_id
        )
        discarded_block_count += discarded
        if discarded:
            pages_with_discarded_blocks.append(pdf_page)
        if source_count == 0:
            rows.append(
                _skipped_row(
                    record=record,
                    pdf_page=pdf_page,
                    profile=profile,
                    reason="external_ocr_empty_page",
                    external_json_sha256=external_json_sha256,
                )
            )
            empty_source_pages.append(pdf_page)
        elif not blocks:
            rows.append(
                _failed_row(
                    record=record,
                    pdf_page=pdf_page,
                    profile=profile,
                    external_json_sha256=external_json_sha256,
                    code="external_ocr_no_valid_blocks",
                    message="External OCR page contained text blocks but none passed validation",
                )
            )
            failed_pages.append(pdf_page)
        else:
            rows.append(
                _generated_row(
                    record=record,
                    pdf_page=pdf_page,
                    profile=profile,
                    external_json_sha256=external_json_sha256,
                    blocks=blocks,
                )
            )

    write_ocr_pages(
        output_path,
        rows,
        book_id=record.book.book_id,
        source_pdf_sha256=record.sha256,
        pdf_page_count=record.actual_pdf_pages,
    )
    source_pdf_sha256_after = file_sha256(record.absolute_path)
    return {
        "schema_version": EXTERNAL_OCR_IMPORT_SCHEMA_VERSION,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "book": {
            "book_id": record.book.book_id,
            "pdf_page_count": record.actual_pdf_pages,
            "source_pdf_sha256": record.sha256,
            "source_pdf_unchanged": source_pdf_sha256_after == record.sha256,
        },
        "input": {
            "path": str(resolved_input),
            "sha256": external_json_sha256,
            "page_result_count": len(document),
            "engine": PP_OCRV6_ENGINE,
            "engine_version": PP_OCRV6_ENGINE_VERSION,
            "model_id": PP_OCRV6_MODEL_ID,
            "preprocess_profile": profile,
            "render_dpi": None,
        },
        "raw_ocr_jsonl": str(output_path.relative_to(project_root)),
        "summary": {
            "generated_pages": sum(row["ocr_status"] == "generated" for row in rows),
            "skipped_pages": sum(row["ocr_status"] == "skipped" for row in rows),
            "failed_pages": sum(row["ocr_status"] == "failed" for row in rows),
            "empty_source_page_count": len(empty_source_pages),
            "registered_exclusion_count": len(excluded_pages),
            "discarded_invalid_block_count": discarded_block_count,
            "pages_with_discarded_blocks": pages_with_discarded_blocks,
            "all_generated_text_is_ocr_unverified": True,
        },
        "empty_source_pages": empty_source_pages,
        "registered_excluded_pages": excluded_pages,
        "failed_pages": failed_pages,
    }


def write_external_ocr_import_report(path: Path, report: Mapping[str, Any]) -> None:
    """Write metadata only; reports deliberately never copy imported OCR text."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)

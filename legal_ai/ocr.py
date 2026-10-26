from __future__ import annotations

"""Versioned OCR JSONL contract helpers.

This module deliberately does not render PDFs or invoke an OCR engine.  Stage
B only makes externally generated page records safe to validate, persist and
consume during canonical-page processing.  Engine adapters belong to stage C.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .domain import LegalAIError


OCR_PAGE_SCHEMA_VERSION = "ocr-page-v1"
OCR_STATUSES = frozenset(
    {"pending", "generated", "failed", "skipped", "not_required"}
)
OCR_BLOCK_ROLES = frozenset({"title", "body", "footnote", "header", "footer", "table"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "page_id",
        "book_id",
        "pdf_page",
        "source_pdf_sha256",
        "ocr_status",
        "ocr_engine",
        "ocr_engine_version",
        "ocr_model_id",
        "render_dpi",
        "preprocess_profile",
        "ocr_text",
        "ocr_text_sha256",
        "ocr_avg_confidence",
        "blocks",
        "error",
        "skip_reason",
    }
)
_BLOCK_REQUIRED_FIELDS = frozenset(
    {"block_id", "text", "bbox", "confidence", "reading_order", "role"}
)
_AUDIT_HASH_FIELDS = frozenset(
    {"render_cache_key", "render_image_sha256", "external_ocr_json_sha256"}
)


def ocr_pages_path(project_root: Path, book_id: str) -> Path:
    return project_root / "data" / "raw" / "ocr" / "pages" / f"{book_id}.jsonl"


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_PATTERN.fullmatch(value))


def _require_nonempty_string(value: object, field: str, page_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegalAIError(f"{page_id}：{field} 必须是非空字符串")
    return value


def _validate_blocks(value: object, page_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LegalAIError(f"{page_id}：generated OCR 必须有非空 blocks")
    blocks: list[dict[str, Any]] = []
    for sequence, raw_block in enumerate(value, start=1):
        if not isinstance(raw_block, dict):
            raise LegalAIError(f"{page_id}：OCR block 必须是对象")
        missing = sorted(_BLOCK_REQUIRED_FIELDS - raw_block.keys())
        if missing:
            raise LegalAIError(
                f"{page_id}：OCR block 缺少字段：{', '.join(missing)}"
            )
        expected_id = f"{page_id}:b{sequence:04d}"
        if raw_block["block_id"] != expected_id:
            raise LegalAIError(
                f"{page_id}：OCR block_id 必须是 {expected_id}"
            )
        _require_nonempty_string(raw_block["text"], "block.text", page_id)
        bbox = raw_block["bbox"]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(isinstance(item, bool) or not isinstance(item, int) for item in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            raise LegalAIError(f"{page_id}：OCR block.bbox 必须是有效 [x1,y1,x2,y2]")
        confidence = raw_block["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 < float(confidence) <= 1.0
        ):
            raise LegalAIError(f"{page_id}：OCR block.confidence 必须在 (0,1]")
        if raw_block["reading_order"] != sequence:
            raise LegalAIError(f"{page_id}：OCR block.reading_order 必须连续从 1 开始")
        if raw_block["role"] not in OCR_BLOCK_ROLES:
            raise LegalAIError(f"{page_id}：未知 OCR block.role {raw_block['role']}")
        blocks.append(dict(raw_block))
    return blocks


def validate_ocr_page(
    value: Mapping[str, Any],
    *,
    expected_book_id: str | None = None,
    expected_source_pdf_sha256: str | None = None,
    expected_pdf_page_count: int | None = None,
) -> dict[str, Any]:
    """Validate one page record and return a detached JSON-compatible copy."""

    if not isinstance(value, Mapping):
        raise LegalAIError("OCR 页面记录必须是对象")
    missing = sorted(_REQUIRED_FIELDS - value.keys())
    if missing:
        raise LegalAIError("OCR 页面记录缺少字段：" + ", ".join(missing))
    row = dict(value)
    page_id = _require_nonempty_string(row["page_id"], "page_id", "OCR 页面")
    book_id = _require_nonempty_string(row["book_id"], "book_id", page_id)
    pdf_page = row["pdf_page"]
    if isinstance(pdf_page, bool) or not isinstance(pdf_page, int) or pdf_page <= 0:
        raise LegalAIError(f"{page_id}：pdf_page 必须是正整数")
    if expected_pdf_page_count is not None and pdf_page > expected_pdf_page_count:
        raise LegalAIError(f"{page_id}：pdf_page 超出 PDF 页数")
    if page_id != f"{book_id}:p{pdf_page:04d}":
        raise LegalAIError(f"{page_id}：page_id 与 book_id/pdf_page 不一致")
    if expected_book_id is not None and book_id != expected_book_id:
        raise LegalAIError(f"{page_id}：OCR book_id 与当前书目不一致")
    source_sha256 = row["source_pdf_sha256"]
    if not _is_sha256(source_sha256):
        raise LegalAIError(f"{page_id}：source_pdf_sha256 必须是小写 SHA-256")
    if (
        expected_source_pdf_sha256 is not None
        and source_sha256 != expected_source_pdf_sha256
    ):
        raise LegalAIError(f"{page_id}：OCR 来源 PDF SHA-256 不一致")
    if row["schema_version"] != OCR_PAGE_SCHEMA_VERSION:
        raise LegalAIError(
            f"{page_id}：OCR schema_version 必须是 {OCR_PAGE_SCHEMA_VERSION}"
        )
    status = row["ocr_status"]
    if status not in OCR_STATUSES:
        raise LegalAIError(f"{page_id}：未知 ocr_status {status}")
    dpi = row["render_dpi"]
    if dpi is not None and (
        isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 1200
    ):
        raise LegalAIError(f"{page_id}：render_dpi 必须为 null 或 72-1200 之间的整数")
    _require_nonempty_string(row["preprocess_profile"], "preprocess_profile", page_id)

    engine_fields = ("ocr_engine", "ocr_engine_version", "ocr_model_id")
    if status in {"generated", "failed", "pending"}:
        for field in engine_fields:
            _require_nonempty_string(row[field], field, page_id)
    elif any(row[field] is not None for field in engine_fields):
        raise LegalAIError(f"{page_id}：{status} OCR 页面不得声明引擎或模型")

    if status == "generated":
        text = _require_nonempty_string(row["ocr_text"], "ocr_text", page_id)
        actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row["ocr_text_sha256"] != actual_hash:
            raise LegalAIError(f"{page_id}：ocr_text_sha256 不一致")
        confidence = row["ocr_avg_confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 < float(confidence) <= 1.0
        ):
            raise LegalAIError(f"{page_id}：ocr_avg_confidence 必须在 (0,1]")
        blocks = _validate_blocks(row["blocks"], page_id)
        if text != "\n".join(str(block["text"]) for block in blocks):
            raise LegalAIError(f"{page_id}：ocr_text 必须按 reading_order 拼接 blocks")
        if row["error"] is not None or row["skip_reason"] is not None:
            raise LegalAIError(f"{page_id}：generated OCR 不得带 error 或 skip_reason")
    else:
        if any(
            row[field] is not None
            for field in ("ocr_text", "ocr_text_sha256", "ocr_avg_confidence")
        ) or row["blocks"] != []:
            raise LegalAIError(
                f"{page_id}：{status} OCR 不能带文本、哈希、置信度或 blocks"
            )
        if status == "failed":
            error = row["error"]
            if (
                not isinstance(error, Mapping)
                or not isinstance(error.get("code"), str)
                or not error.get("code")
            ):
                raise LegalAIError(f"{page_id}：failed OCR 必须提供 error.code")
            if row["skip_reason"] is not None:
                raise LegalAIError(f"{page_id}：failed OCR 不得带 skip_reason")
        elif status == "skipped":
            _require_nonempty_string(row["skip_reason"], "skip_reason", page_id)
            if row["error"] is not None:
                raise LegalAIError(f"{page_id}：skipped OCR 不得带 error")
        elif row["error"] is not None or row["skip_reason"] is not None:
            raise LegalAIError(f"{page_id}：{status} OCR 不得带 error 或 skip_reason")

    # Stage C fields are optional so existing v2 fixture data remains valid,
    # but values written by the pilot are still contract-checked when present.
    for field in _AUDIT_HASH_FIELDS:
        if field in row and not _is_sha256(row[field]):
            raise LegalAIError(f"{page_id}：{field} 必须是小写 SHA-256")
    if "ocr_attempts" in row and (
        isinstance(row["ocr_attempts"], bool)
        or not isinstance(row["ocr_attempts"], int)
        or row["ocr_attempts"] <= 0
    ):
        raise LegalAIError(f"{page_id}：ocr_attempts 必须是正整数")
    return row


def load_ocr_pages(
    path: Path,
    *,
    book_id: str,
    source_pdf_sha256: str,
    pdf_page_count: int,
) -> dict[int, dict[str, Any]]:
    """Load validated OCR records keyed by one-based PDF page number.

    Missing OCR JSONL is an honest empty result.  The caller decides whether
    that becomes `not_required`, `pending`, or an OCR coverage gap.
    """

    if not path.exists():
        return {}
    rows: dict[int, dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise LegalAIError(f"{path} 第 {line_number} 行不能为空")
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LegalAIError(
                        f"{path} 第 {line_number} 行不是合法 OCR JSON"
                    ) from exc
                row = validate_ocr_page(
                    raw,
                    expected_book_id=book_id,
                    expected_source_pdf_sha256=source_pdf_sha256,
                    expected_pdf_page_count=pdf_page_count,
                )
                page = int(row["pdf_page"])
                if page in rows:
                    raise LegalAIError(f"{path}：重复 OCR PDF 页 {page}")
                rows[page] = row
    except OSError as exc:
        raise LegalAIError(f"无法读取 OCR JSONL：{path}") from exc
    return rows


def write_ocr_pages(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    book_id: str,
    source_pdf_sha256: str,
    pdf_page_count: int,
) -> None:
    """Atomically persist validated OCR records without invoking OCR."""

    normalized: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    for row in rows:
        checked = validate_ocr_page(
            row,
            expected_book_id=book_id,
            expected_source_pdf_sha256=source_pdf_sha256,
            expected_pdf_page_count=pdf_page_count,
        )
        page = int(checked["pdf_page"])
        if page in seen_pages:
            raise LegalAIError(f"OCR 输出包含重复 PDF 页 {page}")
        seen_pages.add(page)
        normalized.append(checked)
    normalized.sort(key=lambda item: int(item["pdf_page"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in normalized:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)

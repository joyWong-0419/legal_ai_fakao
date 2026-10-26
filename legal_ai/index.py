from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import LEGACY_RAW_PIPELINE_VERSIONS, PIPELINE_VERSION
from .domain import (
    InventoryRecord,
    LegalAIError,
    TEXT_SOURCES,
    TEXT_VERIFICATION_STATUSES,
)
from .ocr import OCR_STATUSES
from .page_map import PAGE_MAP_STATUSES
from .normalize import NORMALIZATION_VERSION
from .segment import SEGMENTATION_VERSION


INDEX_SCHEMA_VERSION = 2
INDEX_VERSION = "fts5-trigram-v2"
DEFAULT_DATABASE_RELATIVE_PATH = Path("data") / "index" / "legal_corpus.sqlite"


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA user_version = 2;

CREATE TABLE index_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE books (
    book_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    subtitle TEXT,
    edition TEXT,
    volume TEXT,
    metadata_json TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    pdf_page_count INTEGER NOT NULL CHECK (pdf_page_count > 0),
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL
);

CREATE TABLE book_jurisdictions (
    book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
    jurisdiction TEXT NOT NULL,
    PRIMARY KEY (book_id, jurisdiction)
);

CREATE TABLE book_subjects (
    book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    PRIMARY KEY (book_id, subject)
);

CREATE TABLE pages (
    page_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
    pdf_page INTEGER NOT NULL CHECK (pdf_page > 0),
    printed_page TEXT,
    printed_page_status TEXT NOT NULL
        CHECK (printed_page_status IN ('unknown','detected','verified','rejected')),
    chapter_path_json TEXT NOT NULL,
    raw_text TEXT,
    normalized_text TEXT NOT NULL,
    raw_text_sha256 TEXT,
    text_source TEXT NOT NULL
        CHECK (text_source IN ('embedded_text','ocr','hybrid','empty')),
    text_verification_status TEXT NOT NULL
        CHECK (text_verification_status IN ('embedded_text','ocr_unverified','ocr_verified')),
    ocr_status TEXT NOT NULL
        CHECK (ocr_status IN ('pending','generated','failed','skipped','not_required')),
    ocr_engine TEXT,
    ocr_avg_confidence REAL
        CHECK (ocr_avg_confidence IS NULL OR (ocr_avg_confidence > 0 AND ocr_avg_confidence <= 1)),
    ocr_text_sha256 TEXT,
    char_count INTEGER NOT NULL CHECK (char_count >= 0),
    quality_flags_json TEXT NOT NULL,
    UNIQUE (book_id, pdf_page)
);

CREATE TABLE sentences (
    sentence_id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq > 0),
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset >= start_offset),
    text_sha256 TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    segmentation_version TEXT NOT NULL,
    UNIQUE (page_id, seq)
);

CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq > 0),
    first_sentence_id TEXT NOT NULL REFERENCES sentences(sentence_id),
    last_sentence_id TEXT NOT NULL REFERENCES sentences(sentence_id),
    sentence_ids_json TEXT NOT NULL,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL CHECK (char_count >= 0),
    chapter_path_json TEXT NOT NULL,
    segmentation_version TEXT NOT NULL,
    UNIQUE (page_id, seq)
);

CREATE TABLE retrieval_runs (
    run_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    index_version TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    results_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_pages_book_page ON pages(book_id, pdf_page);
CREATE INDEX idx_sentences_page_seq ON sentences(page_id, seq);
CREATE INDEX idx_chunks_page_seq ON chunks(page_id, seq);
CREATE INDEX idx_jurisdictions_value ON book_jurisdictions(jurisdiction, book_id);
CREATE INDEX idx_subjects_value ON book_subjects(subject, book_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED,
    text,
    title,
    chapter_path,
    subjects,
    jurisdictions,
    tokenize='trigram'
);
"""


def validate_database_output_path(
    database_path: Path, project_root: Path
) -> Path:
    allowed_root = (project_root / "data" / "index").resolve()
    candidate = database_path.resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise LegalAIError(
            "数据库重建目标必须位于 legal_ai/data/index/，"
            f"拒绝写入：{candidate}"
        ) from exc
    if candidate.suffix.lower() not in {".sqlite", ".sqlite3"}:
        raise LegalAIError("数据库文件扩展名必须是 .sqlite 或 .sqlite3")
    return candidate


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _book_metadata(record: InventoryRecord) -> dict[str, Any]:
    value = asdict(record.book)
    for field in ("authors", "translators", "jurisdictions", "subjects"):
        value[field] = list(value[field])
    return value


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        raise LegalAIError(f"索引输入不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LegalAIError(
                    f"索引输入不是合法 JSONL：{path} 第 {line_number} 行"
                ) from exc
            if not isinstance(value, dict):
                raise LegalAIError(
                    f"索引输入行必须是对象：{path} 第 {line_number} 行"
                )
            yield value


def _require_page_status(value: Any, page_id: str) -> str:
    status = str(value)
    if status not in PAGE_MAP_STATUSES:
        raise LegalAIError(f"{page_id}：未知印刷页状态 {status}")
    return status


def _require_text_source(value: Any, page_id: str) -> str:
    source = str(value)
    if source not in TEXT_SOURCES:
        raise LegalAIError(f"{page_id}：未知 text_source {source}")
    return source


def _require_text_verification_status(value: Any, page_id: str) -> str:
    status = str(value)
    if status not in TEXT_VERIFICATION_STATUSES:
        raise LegalAIError(f"{page_id}：未知 text_verification_status {status}")
    return status


def _require_ocr_status(value: Any, page_id: str) -> str:
    status = str(value)
    if status not in OCR_STATUSES:
        raise LegalAIError(f"{page_id}：未知 ocr_status {status}")
    return status


def _validate_canonical_provenance(
    processed_page: Mapping[str, Any], page_id: str
) -> tuple[str, str, str, str | None, float | None, str | None]:
    source = _require_text_source(processed_page.get("text_source"), page_id)
    verification = _require_text_verification_status(
        processed_page.get("text_verification_status"), page_id
    )
    ocr_status = _require_ocr_status(processed_page.get("ocr_status"), page_id)
    ocr_engine = processed_page.get("ocr_engine")
    if ocr_engine is not None and (
        not isinstance(ocr_engine, str) or not ocr_engine.strip()
    ):
        raise LegalAIError(f"{page_id}：ocr_engine 必须是非空字符串或 null")
    confidence_value = processed_page.get("ocr_avg_confidence")
    if confidence_value is None:
        confidence = None
    elif (
        isinstance(confidence_value, bool)
        or not isinstance(confidence_value, (int, float))
        or not 0.0 < float(confidence_value) <= 1.0
    ):
        raise LegalAIError(f"{page_id}：ocr_avg_confidence 必须在 (0,1] 或 null")
    else:
        confidence = float(confidence_value)
    ocr_hash = processed_page.get("ocr_text_sha256")
    if ocr_hash is not None and (
        not isinstance(ocr_hash, str) or len(ocr_hash) != 64
    ):
        raise LegalAIError(f"{page_id}：ocr_text_sha256 必须是 SHA-256 或 null")

    if source == "embedded_text" and verification != "embedded_text":
        raise LegalAIError(f"{page_id}：embedded_text 必须标为 embedded_text")
    if source in {"ocr", "hybrid"} and verification not in {
        "ocr_unverified",
        "ocr_verified",
    }:
        raise LegalAIError(f"{page_id}：OCR 文本必须带 OCR 核验状态")
    if source == "ocr" and ocr_status != "generated":
        raise LegalAIError(f"{page_id}：text_source=ocr 必须来自 generated OCR")
    if source == "empty" and verification != "embedded_text":
        raise LegalAIError(f"{page_id}：empty 页面不得伪造 OCR 核验状态")
    if ocr_status == "generated" and (
        not ocr_engine or confidence is None or ocr_hash is None
    ):
        raise LegalAIError(f"{page_id}：generated OCR 缺少引擎、置信度或文本哈希")
    if ocr_status != "generated" and (
        confidence is not None or ocr_hash is not None
    ):
        raise LegalAIError(f"{page_id}：未生成 OCR 不得带置信度或文本哈希")
    return source, verification, ocr_status, ocr_engine, confidence, ocr_hash


def _insert_book(
    connection: sqlite3.Connection,
    record: InventoryRecord,
    project_root: Path,
    ingested_at: str,
) -> dict[str, int]:
    book = record.book
    book_id = book.book_id
    connection.execute(
        """
        INSERT INTO books (
            book_id, title, subtitle, edition, volume, metadata_json,
            file_path, sha256, pdf_page_count, ingested_at, pipeline_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            book_id,
            book.title,
            book.subtitle,
            book.edition,
            book.volume,
            _json(_book_metadata(record)),
            book.file_path,
            record.sha256,
            record.actual_pdf_pages,
            ingested_at,
            PIPELINE_VERSION,
        ),
    )
    connection.executemany(
        "INSERT INTO book_jurisdictions (book_id, jurisdiction) VALUES (?, ?)",
        ((book_id, item) for item in sorted(set(book.jurisdictions))),
    )
    connection.executemany(
        "INSERT INTO book_subjects (book_id, subject) VALUES (?, ?)",
        ((book_id, item) for item in sorted(set(book.subjects))),
    )

    raw_path = project_root / "data" / "raw" / "pages" / f"{book_id}.jsonl"
    processed_base = project_root / "data" / "processed"
    processed_path = processed_base / "pages" / f"{book_id}.jsonl"
    raw_pages = list(_iter_jsonl(raw_path))
    processed_pages = list(_iter_jsonl(processed_path))
    if len(raw_pages) != record.actual_pdf_pages or len(processed_pages) != len(
        raw_pages
    ):
        raise LegalAIError(
            f"{book_id}：raw/processed/PDF 页数不一致 "
            f"({len(raw_pages)}/{len(processed_pages)}/{record.actual_pdf_pages})"
        )

    page_ids: set[str] = set()
    page_numbers: dict[str, int] = {}
    page_texts: dict[str, str] = {}
    page_chapters: dict[str, list[Any]] = {}
    for expected_page, (raw_page, processed_page) in enumerate(
        zip(raw_pages, processed_pages), start=1
    ):
        page_id = f"{book_id}:p{expected_page:04d}"
        if (
            raw_page.get("page_id") != page_id
            or processed_page.get("page_id") != page_id
            or raw_page.get("book_id") != book_id
            or processed_page.get("book_id") != book_id
            or raw_page.get("pdf_page") != expected_page
            or processed_page.get("pdf_page") != expected_page
        ):
            raise LegalAIError(f"{book_id}：第 {expected_page} 页 ID 或顺序不一致")
        raw_pipeline_version = raw_page.get("pipeline_version")
        if raw_pipeline_version not in {
            PIPELINE_VERSION,
            *LEGACY_RAW_PIPELINE_VERSIONS,
        }:
            raise LegalAIError(f"{page_id}：raw pipeline_version 不兼容")
        if processed_page.get("pipeline_version") != PIPELINE_VERSION:
            raise LegalAIError(f"{page_id}：processed pipeline_version 不一致")
        if processed_page.get("normalization_version") != NORMALIZATION_VERSION:
            raise LegalAIError(f"{page_id}：normalization_version 不一致")
        if raw_page.get("source_pdf_sha256") != record.sha256:
            raise LegalAIError(f"{page_id}：source PDF SHA-256 不一致")
        raw_text = raw_page.get("raw_text")
        raw_hash = raw_page.get("raw_text_sha256")
        if raw_text is None:
            if raw_hash is not None:
                raise LegalAIError(f"{page_id}：空 raw_text 不得声明文本哈希")
        else:
            expected_raw_hash = hashlib.sha256(
                str(raw_text).encode("utf-8")
            ).hexdigest()
            if raw_hash != expected_raw_hash:
                raise LegalAIError(f"{page_id}：raw_text_sha256 不一致")
        page_ids.add(page_id)
        page_numbers[page_id] = expected_page
        normalized_text = str(processed_page.get("normalized_text") or "")
        page_texts[page_id] = normalized_text
        chapter_path = processed_page.get("chapter_path") or []
        if not isinstance(chapter_path, list):
            raise LegalAIError(f"{page_id}：chapter_path 必须是数组")
        page_chapters[page_id] = chapter_path
        char_count = int(processed_page.get("char_count") or 0)
        if char_count != len(normalized_text):
            raise LegalAIError(f"{page_id}：char_count 与规范化文本不一致")
        (
            text_source,
            text_verification_status,
            ocr_status,
            ocr_engine,
            ocr_avg_confidence,
            ocr_text_sha256,
        ) = _validate_canonical_provenance(processed_page, page_id)
        connection.execute(
            """
            INSERT INTO pages (
                page_id, book_id, pdf_page, printed_page,
                printed_page_status, chapter_path_json, raw_text,
                normalized_text, raw_text_sha256, text_source,
                text_verification_status, ocr_status, ocr_engine,
                ocr_avg_confidence, ocr_text_sha256, char_count,
                quality_flags_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                book_id,
                expected_page,
                processed_page.get("printed_page"),
                _require_page_status(
                    processed_page.get("printed_page_status", "unknown"), page_id
                ),
                _json(chapter_path),
                raw_text,
                normalized_text,
                raw_hash,
                text_source,
                text_verification_status,
                ocr_status,
                ocr_engine,
                ocr_avg_confidence,
                ocr_text_sha256,
                char_count,
                _json(processed_page.get("quality_flags") or []),
            ),
        )

    sentence_path = processed_base / "sentences" / f"{book_id}.jsonl"
    sentence_page: dict[str, str] = {}
    sentence_sequence: dict[str, int] = {}
    sentence_text: dict[str, str] = {}
    last_sentence_seq: dict[str, int] = {}
    sentence_count = 0
    for sentence in _iter_jsonl(sentence_path):
        sentence_id = str(sentence.get("sentence_id") or "")
        page_id = str(sentence.get("page_id") or "")
        if not sentence_id or page_id not in page_ids:
            raise LegalAIError(f"{book_id}：句子引用未知页面 {page_id}")
        if (
            sentence.get("book_id") != book_id
            or sentence.get("pdf_page") != page_numbers[page_id]
        ):
            raise LegalAIError(f"{sentence_id}：句子 book_id/pdf_page 不一致")
        if sentence_id in sentence_page:
            raise LegalAIError(f"{book_id}：重复 sentence_id {sentence_id}")
        sequence = int(sentence.get("seq"))
        if sequence != last_sentence_seq.get(page_id, 0) + 1:
            raise LegalAIError(f"{sentence_id}：页内句子 seq 不连续")
        if sentence_id != f"{page_id}:s{sequence:03d}":
            raise LegalAIError(f"{sentence_id}：sentence_id 与 seq 不一致")
        start_offset = int(sentence.get("start_offset"))
        end_offset = int(sentence.get("end_offset"))
        normalized_text = str(sentence.get("normalized_text") or "")
        if not 0 <= start_offset <= end_offset <= len(page_texts[page_id]):
            raise LegalAIError(f"{sentence_id}：offset 超出页级规范化文本")
        if page_texts[page_id][start_offset:end_offset] != normalized_text:
            raise LegalAIError(f"{sentence_id}：offset 不能复现句子文本")
        expected_text_hash = hashlib.sha256(
            normalized_text.encode("utf-8")
        ).hexdigest()
        if sentence.get("text_sha256") != expected_text_hash:
            raise LegalAIError(f"{sentence_id}：text_sha256 不一致")
        if sentence.get("normalization_version") != NORMALIZATION_VERSION:
            raise LegalAIError(f"{sentence_id}：normalization_version 不一致")
        if sentence.get("segmentation_version") != SEGMENTATION_VERSION:
            raise LegalAIError(f"{sentence_id}：segmentation_version 不一致")
        connection.execute(
            """
            INSERT INTO sentences (
                sentence_id, page_id, seq, raw_text, normalized_text,
                start_offset, end_offset, text_sha256,
                normalization_version, segmentation_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sentence_id,
                page_id,
                sequence,
                str(sentence.get("raw_text") or ""),
                normalized_text,
                start_offset,
                end_offset,
                expected_text_hash,
                NORMALIZATION_VERSION,
                SEGMENTATION_VERSION,
            ),
        )
        sentence_page[sentence_id] = page_id
        sentence_sequence[sentence_id] = sequence
        sentence_text[sentence_id] = normalized_text
        last_sentence_seq[page_id] = sequence
        sentence_count += 1

    chunk_path = processed_base / "chunks" / f"{book_id}.jsonl"
    chunk_count = 0
    last_chunk_seq: dict[str, int] = {}
    for chunk in _iter_jsonl(chunk_path):
        chunk_id = str(chunk.get("chunk_id") or "")
        page_id = str(chunk.get("page_id") or "")
        first_sentence_id = str(chunk.get("first_sentence_id") or "")
        last_sentence_id = str(chunk.get("last_sentence_id") or "")
        sentence_ids = chunk.get("sentence_ids")
        if not chunk_id or page_id not in page_ids:
            raise LegalAIError(f"{book_id}：chunk 引用未知页面 {page_id}")
        if (
            chunk.get("book_id") != book_id
            or chunk.get("pdf_page") != page_numbers[page_id]
        ):
            raise LegalAIError(f"{chunk_id}：chunk book_id/pdf_page 不一致")
        sequence = int(chunk.get("seq"))
        if sequence != last_chunk_seq.get(page_id, 0) + 1:
            raise LegalAIError(f"{chunk_id}：页内 chunk seq 不连续")
        if chunk_id != f"{page_id}:c{sequence:03d}":
            raise LegalAIError(f"{chunk_id}：chunk_id 与 seq 不一致")
        if not isinstance(sentence_ids, list) or not sentence_ids:
            raise LegalAIError(f"{chunk_id}：sentence_ids 必须是非空数组")
        if sentence_ids[0] != first_sentence_id or sentence_ids[-1] != last_sentence_id:
            raise LegalAIError(f"{chunk_id}：首尾句与 sentence_ids 不一致")
        if any(sentence_page.get(item) != page_id for item in sentence_ids):
            raise LegalAIError(f"{chunk_id}：chunk 句子不存在或跨 PDF 页")
        sequences = [sentence_sequence[item] for item in sentence_ids]
        if sequences != list(range(sequences[0], sequences[-1] + 1)):
            raise LegalAIError(f"{chunk_id}：chunk 内句子顺序不连续")
        text = str(chunk.get("text") or "")
        expected_chunk_text = "\n".join(sentence_text[item] for item in sentence_ids)
        if text != expected_chunk_text:
            raise LegalAIError(f"{chunk_id}：chunk 文本不能由句子复现")
        if int(chunk.get("char_count") or 0) != len(text):
            raise LegalAIError(f"{chunk_id}：char_count 与 chunk 文本不一致")
        chapter_path = chunk.get("chapter_path") or []
        if chapter_path != page_chapters[page_id]:
            raise LegalAIError(f"{chunk_id}：章节路径与页面不一致")
        if chunk.get("segmentation_version") != SEGMENTATION_VERSION:
            raise LegalAIError(f"{chunk_id}：segmentation_version 不一致")
        connection.execute(
            """
            INSERT INTO chunks (
                chunk_id, page_id, seq, first_sentence_id, last_sentence_id,
                sentence_ids_json, text, char_count, chapter_path_json,
                segmentation_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                page_id,
                sequence,
                first_sentence_id,
                last_sentence_id,
                _json(sentence_ids),
                text,
                len(text),
                _json(chapter_path),
                SEGMENTATION_VERSION,
            ),
        )
        display_title = (
            f"{book.title} {book.subtitle}" if book.subtitle else book.title
        )
        connection.execute(
            """
            INSERT INTO chunks_fts (
                chunk_id, text, title, chapter_path, subjects, jurisdictions
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                text,
                display_title,
                " > ".join(str(item) for item in chapter_path),
                " ".join(book.subjects),
                " ".join(book.jurisdictions),
            ),
        )
        last_chunk_seq[page_id] = sequence
        chunk_count += 1

    return {
        "pages": len(raw_pages),
        "sentences": sentence_count,
        "chunks": chunk_count,
    }


def _validate_index(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise LegalAIError(f"SQLite integrity_check 失败：{integrity}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise LegalAIError(f"SQLite 外键不一致：{foreign_keys[:3]}")
    counts = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in (
            "books",
            "pages",
            "sentences",
            "chunks",
            "chunks_fts",
            "retrieval_runs",
        )
    }
    if counts["chunks"] != counts["chunks_fts"]:
        raise LegalAIError("chunks 与 FTS 条目数量不一致")
    missing_fts = connection.execute(
        "SELECT chunk_id FROM chunks EXCEPT SELECT chunk_id FROM chunks_fts LIMIT 3"
    ).fetchall()
    extra_fts = connection.execute(
        "SELECT chunk_id FROM chunks_fts EXCEPT SELECT chunk_id FROM chunks LIMIT 3"
    ).fetchall()
    duplicate_fts = connection.execute(
        """
        SELECT chunk_id, COUNT(*)
        FROM chunks_fts
        GROUP BY chunk_id
        HAVING COUNT(*) <> 1
        LIMIT 3
        """
    ).fetchall()
    if missing_fts or extra_fts or duplicate_fts:
        raise LegalAIError(
            "chunks 与 FTS chunk_id 集不一致："
            f"missing={missing_fts}, extra={extra_fts}, duplicate={duplicate_fts}"
        )
    orphan_boundaries = connection.execute(
        """
        SELECT COUNT(*)
        FROM chunks c
        JOIN sentences first_s ON first_s.sentence_id = c.first_sentence_id
        JOIN sentences last_s ON last_s.sentence_id = c.last_sentence_id
        WHERE first_s.page_id <> c.page_id OR last_s.page_id <> c.page_id
        """
    ).fetchone()[0]
    if orphan_boundaries:
        raise LegalAIError("chunk 首尾句存在跨页引用")
    return {"integrity_check": integrity, "counts": counts}


def _load_existing_retrieval_runs(database_path: Path) -> list[tuple[Any, ...]]:
    if not database_path.exists():
        return []
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(database_path))
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retrieval_runs'"
        ).fetchone()
        if exists is None:
            return []
        rows = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT run_id, query, filters_json, pipeline_version,
                       index_version, candidates_json, results_json, created_at
                FROM retrieval_runs
                ORDER BY created_at, run_id
                """
            ).fetchall()
        ]
        # Retrieval runs encode score and provenance semantics.  Replaying a
        # p1 run inside the v2 database would make verification report a false
        # version mismatch, so only same-version history is retained.
        return [
            row
            for row in rows
            if row[3] == PIPELINE_VERSION and row[4] == INDEX_VERSION
        ]
    except sqlite3.DatabaseError:
        return []
    finally:
        if connection is not None:
            connection.close()


def build_index(
    *,
    records: Sequence[InventoryRecord],
    project_root: Path,
    database_path: Path,
    rebuild: bool,
    preserve_retrieval_runs: bool = True,
) -> dict[str, Any]:
    database_path = validate_database_output_path(database_path, project_root)
    if database_path.exists() and not rebuild:
        raise LegalAIError("索引已存在；请显式使用 --rebuild 重建")
    existing_retrieval_runs = (
        _load_existing_retrieval_runs(database_path)
        if preserve_retrieval_runs
        else []
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.",
        suffix=".tmp",
        dir=str(database_path.parent),
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)

    built_at = datetime.now(timezone.utc).isoformat()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(temporary))
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        try:
            connection.executescript(SCHEMA_SQL)
        except sqlite3.OperationalError as exc:
            raise LegalAIError(
                "当前 SQLite 不支持所需的 FTS5 trigram tokenizer"
            ) from exc
        connection.executemany(
            "INSERT INTO index_metadata (key, value) VALUES (?, ?)",
            (
                ("index_version", INDEX_VERSION),
                ("pipeline_version", PIPELINE_VERSION),
                ("normalization_version", NORMALIZATION_VERSION),
                ("segmentation_version", SEGMENTATION_VERSION),
                ("built_at", built_at),
            ),
        )
        if existing_retrieval_runs:
            connection.executemany(
                """
                INSERT INTO retrieval_runs (
                    run_id, query, filters_json, pipeline_version,
                    index_version, candidates_json, results_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                existing_retrieval_runs,
            )
        per_book: list[dict[str, Any]] = []
        for record in records:
            counts = _insert_book(connection, record, project_root, built_at)
            per_book.append(
                {
                    "book_id": record.book.book_id,
                    "title": record.book.title,
                    **counts,
                }
            )
        validation = _validate_index(connection)
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary, database_path)
    except Exception as exc:
        if connection is not None:
            connection.close()
        if temporary.exists():
            temporary.unlink()
        if isinstance(exc, LegalAIError):
            raise
        raise LegalAIError(f"构建 SQLite 索引失败：{exc}") from exc

    try:
        display_database_path = str(
            database_path.relative_to(project_root.parent.resolve())
        )
    except ValueError:
        display_database_path = str(database_path)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "index_version": INDEX_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "built_at": built_at,
        "database_path": display_database_path,
        "sqlite_version": sqlite3.sqlite_version,
        "book_count": validation["counts"]["books"],
        "page_count": validation["counts"]["pages"],
        "sentence_count": validation["counts"]["sentences"],
        "chunk_count": validation["counts"]["chunks"],
        "fts_row_count": validation["counts"]["chunks_fts"],
        "retrieval_run_count": validation["counts"]["retrieval_runs"],
        "retrieval_runs_preserved": len(existing_retrieval_runs),
        "integrity_check": validation["integrity_check"],
        "books": per_book,
    }


def write_index_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)

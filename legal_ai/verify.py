from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pypdf import PdfReader

from . import LEGACY_RAW_PIPELINE_VERSIONS, PIPELINE_VERSION
from .domain import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_PAGE_MAPS_PATH,
    BookConfig,
    LegalAIError,
    TEXT_SOURCES,
    TEXT_VERIFICATION_STATUSES,
)
from .evidence import (
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    MAX_QUOTE_CHARS,
    MAX_SENTENCES_PER_QUOTE,
)
from .index import INDEX_SCHEMA_VERSION, INDEX_VERSION
from .inventory import file_sha256, load_books, resolve_book_path
from .normalize import NORMALIZATION_VERSION, normalize_page_text
from .ocr import OCR_STATUSES, load_ocr_pages, ocr_pages_path
from .page_map import (
    PAGE_MAP_STATUSES,
    load_page_maps,
    page_map_by_book,
    resolve_page,
)
from .segment import SEGMENTATION_VERSION


VERIFICATION_SCHEMA_VERSION = 1
VERIFICATION_VERSION = "verify-v1"
MAX_REPORTED_ISSUES = 200

REQUIRED_TABLES = {
    "index_metadata",
    "books",
    "book_jurisdictions",
    "book_subjects",
    "pages",
    "sentences",
    "chunks",
    "retrieval_runs",
    "chunks_fts",
}

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@+\-]+$")


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if 0 < len(text) <= 160 and _SAFE_IDENTIFIER.fullmatch(text):
        return text
    return f"sha256:{_digest_text(text)[:16]}"


def _safe_detail(value: Any) -> Any:
    """Keep reports useful without ever copying corpus or query text."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (list, tuple, set)):
        return [_safe_detail(item) for item in list(value)[:10]]
    return _safe_identifier(value)


class _IssueCollector:
    def __init__(self, maximum: int = MAX_REPORTED_ISSUES) -> None:
        self.maximum = maximum
        self.total = 0
        self.issues: list[dict[str, Any]] = []

    def add(
        self,
        code: str,
        entity: str,
        entity_id: Any = None,
        **details: Any,
    ) -> None:
        self.total += 1
        if len(self.issues) >= self.maximum:
            return
        issue: dict[str, Any] = {
            "code": code,
            "entity": entity,
        }
        safe_id = _safe_identifier(entity_id)
        if safe_id is not None:
            issue["id"] = safe_id
        for key, value in details.items():
            issue[key] = _safe_detail(value)
        self.issues.append(issue)

    def extend(self, issues: Iterable[Mapping[str, Any]]) -> None:
        for item in issues:
            code = str(item.get("code") or "verification_issue")
            entity = str(item.get("entity") or "unknown")
            entity_id = item.get("id")
            details = {
                str(key): value
                for key, value in item.items()
                if key not in {"code", "entity", "id"}
            }
            self.add(code, entity, entity_id, **details)


def _json_value(
    value: Any,
    *,
    collector: _IssueCollector,
    code: str,
    entity: str,
    entity_id: Any,
    expected_type: type | tuple[type, ...] | None = None,
) -> Any:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        collector.add(code, entity, entity_id)
        return None
    if expected_type is not None and not isinstance(parsed, expected_type):
        collector.add(f"{code}_structure", entity, entity_id)
        return None
    return parsed


def _expected_metadata(book: BookConfig) -> dict[str, Any]:
    value = asdict(book)
    for field in ("authors", "translators", "jurisdictions", "subjects"):
        value[field] = list(value[field])
    return value


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise LegalAIError(f"索引不存在：{database_path}")
    try:
        connection = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"无法只读打开 SQLite 索引：{exc}") from exc


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _score_mapping_valid(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value)
        and all(isinstance(key, str) and _is_number(item) for key, item in value.items())
    )


def _sequence_is_consecutive(values: Sequence[int]) -> bool:
    return bool(values) and list(values) == list(
        range(values[0], values[0] + len(values))
    )


def _list_of_strings(value: Any, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) for item in value)
    )


def _mapping_field_matches(
    item: Mapping[str, Any], field: str, expected: Any
) -> bool:
    return field in item and item[field] == expected


def _load_run_for_evidence(
    connection: sqlite3.Connection,
    run_id: str,
    collector: _IssueCollector,
    *,
    missing_is_issue: bool = True,
) -> dict[str, Any] | None:
    try:
        row = connection.execute(
            """
            SELECT query, filters_json, pipeline_version, index_version,
                   candidates_json, results_json
            FROM retrieval_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        collector.add("evidence_run_lookup_failed", "retrieval_run", run_id)
        return None
    if row is None:
        if missing_is_issue:
            collector.add("evidence_run_missing", "retrieval_run", run_id)
        return None
    filters = _json_value(
        row[1],
        collector=collector,
        code="retrieval_filters_json_invalid",
        entity="retrieval_run",
        entity_id=run_id,
        expected_type=dict,
    )
    candidates = _json_value(
        row[4],
        collector=collector,
        code="retrieval_candidates_json_invalid",
        entity="retrieval_run",
        entity_id=run_id,
        expected_type=list,
    )
    results = _json_value(
        row[5],
        collector=collector,
        code="retrieval_results_json_invalid",
        entity="retrieval_run",
        entity_id=run_id,
        expected_type=list,
    )
    return {
        "query": row[0],
        "filters": filters,
        "pipeline_version": row[2],
        "index_version": row[3],
        "candidates": candidates,
        "results": results,
    }


def _expected_corpus_scope(
    connection: sqlite3.Connection,
    filters: Any,
) -> list[str] | None:
    if not isinstance(filters, Mapping):
        return None
    fields = ("book_ids", "jurisdictions", "subjects")
    if any(not _list_of_strings(filters.get(field, [])) for field in fields):
        return None
    clauses: list[str] = []
    parameters: list[str] = []
    book_ids = filters.get("book_ids", [])
    jurisdictions = filters.get("jurisdictions", [])
    subjects = filters.get("subjects", [])
    if book_ids:
        placeholders = ",".join("?" for _ in book_ids)
        clauses.append(f"b.book_id IN ({placeholders})")
        parameters.extend(book_ids)
    if jurisdictions:
        placeholders = ",".join("?" for _ in jurisdictions)
        clauses.append(
            "EXISTS (SELECT 1 FROM book_jurisdictions bj "
            "WHERE bj.book_id=b.book_id "
            f"AND bj.jurisdiction IN ({placeholders}))"
        )
        parameters.extend(jurisdictions)
    if subjects:
        placeholders = ",".join("?" for _ in subjects)
        clauses.append(
            "EXISTS (SELECT 1 FROM book_subjects bs "
            "WHERE bs.book_id=b.book_id "
            f"AND bs.subject IN ({placeholders}))"
        )
        parameters.extend(subjects)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        return [
            str(row[0])
            for row in connection.execute(
                f"SELECT b.book_id FROM books b{where} ORDER BY b.book_id",
                parameters,
            ).fetchall()
        ]
    except sqlite3.DatabaseError:
        return None


def verify_evidence_package(
    connection: sqlite3.Connection,
    package: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Verify one evidence package against sentence records, without writes.

    The returned issues contain identifiers, counts and hashes only.  No quote,
    query, title, chapter or PDF text is copied into the verification output.
    """
    collector = _IssueCollector()
    if not isinstance(package, Mapping):
        collector.add("evidence_package_not_object", "evidence_package")
        return collector.issues

    package_schema_version = package.get("schema_version")
    is_v2_package = package_schema_version == EVIDENCE_SCHEMA_VERSION
    # Existing P0-P6 packages are evidence-v1 records from embedded text.
    # Keep them read-only verifiable after a v2 rebuild, but never emit them
    # for new context calls.
    is_legacy_v1_package = (
        package_schema_version == 1 and package.get("evidence_version") == "evidence-v1"
    )
    if not is_v2_package and not is_legacy_v1_package:
        collector.add(
            "evidence_schema_version_mismatch",
            "evidence_package",
            expected=f"1 or {EVIDENCE_SCHEMA_VERSION}",
        )
    if not is_legacy_v1_package and package.get("evidence_version") != EVIDENCE_VERSION:
        collector.add(
            "evidence_version_mismatch",
            "evidence_package",
            expected=EVIDENCE_VERSION,
        )
    try:
        datetime.fromisoformat(str(package.get("generated_at")).replace("Z", "+00:00"))
    except ValueError:
        collector.add("evidence_generated_at_invalid", "evidence_package")

    run_id_value = package.get("run_id")
    run_id = run_id_value if isinstance(run_id_value, str) else ""
    if not run_id:
        collector.add("evidence_run_id_invalid", "evidence_package")
    run = (
        _load_run_for_evidence(
            connection,
            run_id,
            collector,
            missing_is_issue=not is_legacy_v1_package,
        )
        if run_id
        else None
    )
    if run is not None:
        if run["query"] != package.get("query"):
            collector.add("evidence_query_mismatch", "evidence_package", run_id)
        if run["pipeline_version"] != PIPELINE_VERSION:
            collector.add(
                "evidence_run_pipeline_version_mismatch",
                "retrieval_run",
                run_id,
                expected=PIPELINE_VERSION,
            )
        if run["index_version"] != INDEX_VERSION:
            collector.add(
                "evidence_run_index_version_mismatch",
                "retrieval_run",
                run_id,
                expected=INDEX_VERSION,
            )
        run_parameters = run.get("filters")
        expected_filters = None
        if isinstance(run_parameters, Mapping):
            expected_filters = run_parameters.get("filters")
            if expected_filters is None and all(
                field in run_parameters
                for field in ("book_ids", "jurisdictions", "subjects")
            ):
                expected_filters = run_parameters
        if expected_filters != package.get("filters"):
            collector.add("evidence_filters_mismatch", "evidence_package", run_id)
        expected_scope = _expected_corpus_scope(connection, expected_filters)
        if expected_scope is None:
            collector.add("evidence_filter_structure_invalid", "evidence_package", run_id)
        elif package.get("corpus_scope") != expected_scope:
            collector.add("evidence_corpus_scope_mismatch", "evidence_package", run_id)

    if not isinstance(package.get("coverage_notice"), str):
        collector.add("evidence_coverage_notice_invalid", "evidence_package", run_id)
    if not _list_of_strings(package.get("gaps"), allow_empty=True):
        collector.add("evidence_gaps_invalid", "evidence_package", run_id)
    package_query_terms = package.get("query_terms")
    if not _list_of_strings(package_query_terms, allow_empty=False):
        collector.add("evidence_query_terms_invalid", "evidence_package", run_id)
        package_query_terms = []
    if run is not None and isinstance(run.get("filters"), Mapping):
        logged_query_terms = run["filters"].get("query_terms")
        if logged_query_terms is not None and logged_query_terms != package_query_terms:
            collector.add("evidence_query_terms_mismatch", "evidence_package", run_id)

    items = package.get("evidence")
    if not isinstance(items, list):
        collector.add("evidence_list_invalid", "evidence_package", run_id)
        return collector.issues
    if package.get("evidence_count") != len(items):
        collector.add(
            "evidence_count_mismatch",
            "evidence_package",
            run_id,
            expected=len(items),
        )

    run_results: dict[str, Mapping[str, Any]] = {}
    if run is not None and isinstance(run.get("results"), list):
        for result in run["results"]:
            if isinstance(result, Mapping) and isinstance(result.get("chunk_id"), str):
                run_results[result["chunk_id"]] = result

    scope = package.get("corpus_scope")
    scope_is_valid = _list_of_strings(scope)
    valid_scope = set(scope) if scope_is_valid else set()
    seen_evidence_ids: set[str] = set()
    seen_sentence_windows: set[tuple[str, ...]] = set()
    for index, item in enumerate(items, start=1):
        fallback_id = f"{run_id}:e{index:03d}" if run_id else f"e{index:03d}"
        if not isinstance(item, Mapping):
            collector.add("evidence_item_not_object", "evidence", fallback_id)
            continue
        evidence_id_value = item.get("evidence_id")
        evidence_id = evidence_id_value if isinstance(evidence_id_value, str) else ""
        if not evidence_id:
            collector.add("evidence_id_invalid", "evidence", fallback_id)
            evidence_id = fallback_id
        expected_evidence_id = f"{run_id}:e{index:03d}"
        if run_id and evidence_id != expected_evidence_id:
            collector.add("evidence_id_sequence_mismatch", "evidence", evidence_id)
        if evidence_id in seen_evidence_ids:
            collector.add("evidence_id_duplicate", "evidence", evidence_id)
        seen_evidence_ids.add(evidence_id)

        chunk_id_value = item.get("chunk_id")
        chunk_id = chunk_id_value if isinstance(chunk_id_value, str) else ""
        if not chunk_id:
            collector.add("evidence_chunk_id_invalid", "evidence", evidence_id)
            continue
        try:
            row = connection.execute(
                """
                SELECT c.page_id, c.sentence_ids_json,
                       p.book_id, p.pdf_page, p.printed_page,
                       p.printed_page_status, p.chapter_path_json,
                       p.quality_flags_json, p.text_source,
                       p.text_verification_status, p.ocr_status,
                       p.ocr_avg_confidence, p.ocr_text_sha256,
                       b.title, b.subtitle, b.file_path, b.metadata_json
                FROM chunks c
                JOIN pages p ON p.page_id = c.page_id
                JOIN books b ON b.book_id = p.book_id
                WHERE c.chunk_id = ?
                """,
                (chunk_id,),
            ).fetchone()
        except sqlite3.DatabaseError:
            collector.add("evidence_chunk_lookup_failed", "chunk", chunk_id)
            continue
        if row is None:
            collector.add("evidence_chunk_missing", "chunk", chunk_id)
            continue

        page_id = str(row[0])
        book_id = str(row[2])
        scalar_fields = {
            "chunk_id": chunk_id,
            "page_id": page_id,
            "book_id": book_id,
            "pdf_page": int(row[3]),
            "printed_page": row[4],
            "title": row[13],
            "subtitle": row[14],
            "file_path": row[15],
        }
        for field, expected in scalar_fields.items():
            if not _mapping_field_matches(item, field, expected):
                collector.add(
                    "evidence_database_field_mismatch",
                    "evidence",
                    evidence_id,
                    field=field,
                )
        declared_page_status = item.get("printed_page_status")
        current_page_status = str(row[5])
        # A persisted package may honestly retain `detected` after the same
        # candidate mapping is later promoted to `verified` by human review.
        # That historical state is conservative and remains reproducible.
        # The reverse transition is never accepted: an old `verified` package
        # against a demoted current mapping must fail verification.
        status_matches = declared_page_status == current_page_status
        safe_historical_promotion = (
            declared_page_status == "detected"
            and current_page_status == "verified"
            and item.get("printed_page") == row[4]
        )
        if not status_matches and not safe_historical_promotion:
            collector.add(
                "evidence_database_field_mismatch",
                "evidence",
                evidence_id,
                field="printed_page_status",
            )
        expected_obsidian_link = (
            f"[[{row[15]}#page={int(row[3])}\\|打开 PDF 第 {int(row[3])} 页]]"
        )
        if item.get("obsidian_pdf_link") != expected_obsidian_link:
            collector.add("evidence_obsidian_link_mismatch", "evidence", evidence_id)
        if scope_is_valid and book_id not in valid_scope:
            collector.add("evidence_book_outside_scope", "evidence", evidence_id)

        chapter = _json_value(
            row[6],
            collector=collector,
            code="page_chapter_json_invalid",
            entity="page",
            entity_id=page_id,
            expected_type=list,
        )
        quality_flags = _json_value(
            row[7],
            collector=collector,
            code="page_quality_json_invalid",
            entity="page",
            entity_id=page_id,
            expected_type=list,
        )
        metadata = _json_value(
            row[16],
            collector=collector,
            code="book_metadata_json_invalid",
            entity="book",
            entity_id=book_id,
            expected_type=dict,
        )
        if chapter is not None and item.get("chapter_path") != chapter:
            collector.add("evidence_chapter_mismatch", "evidence", evidence_id)
        if quality_flags is not None:
            if item.get("quality_flags") != quality_flags:
                collector.add("evidence_quality_flags_mismatch", "evidence", evidence_id)
            if quality_flags and row[8] != "ocr":
                collector.add("evidence_uses_flagged_page", "evidence", evidence_id)
        if metadata is not None:
            for field in (
                "authors",
                "translators",
                "edition",
                "volume",
                "source_type",
                "authority_tier",
                "jurisdictions",
                "subjects",
            ):
                # edition/volume and term diagnostics were added during the
                # evidence-v1 rollout.  Validate them when present while
                # keeping already persisted v1 packages verifiable.
                if field in {"edition", "volume"} and field not in item:
                    continue
                if item.get(field) != metadata.get(field):
                    collector.add(
                        "evidence_source_metadata_mismatch",
                        "evidence",
                        evidence_id,
                        field=field,
                    )

        page_status = str(row[5])
        if page_status not in PAGE_MAP_STATUSES:
            collector.add("evidence_page_status_invalid", "page", page_id)
        elif page_status in {"unknown", "rejected"} and row[4] is not None:
            collector.add("evidence_unconfirmed_page_has_label", "page", page_id)
        elif page_status in {"detected", "verified"} and row[4] is None:
            collector.add("evidence_mapped_page_missing_label", "page", page_id)

        chunk_sentence_ids = _json_value(
            row[1],
            collector=collector,
            code="chunk_sentence_ids_json_invalid",
            entity="chunk",
            entity_id=chunk_id,
            expected_type=list,
        )
        sentence_ids = item.get("sentence_ids")
        if not _list_of_strings(sentence_ids, allow_empty=False):
            collector.add("evidence_sentence_ids_invalid", "evidence", evidence_id)
            continue
        assert isinstance(sentence_ids, list)
        sentence_window = tuple(sentence_ids)
        if sentence_window in seen_sentence_windows:
            collector.add("evidence_sentence_window_duplicate", "evidence", evidence_id)
        seen_sentence_windows.add(sentence_window)
        if len(sentence_ids) > MAX_SENTENCES_PER_QUOTE:
            collector.add(
                "evidence_sentence_limit_exceeded",
                "evidence",
                evidence_id,
                maximum=MAX_SENTENCES_PER_QUOTE,
                actual=len(sentence_ids),
            )
        if len(set(sentence_ids)) != len(sentence_ids):
            collector.add("evidence_sentence_ids_duplicate", "evidence", evidence_id)
        if _list_of_strings(chunk_sentence_ids, allow_empty=False):
            positions = [
                chunk_sentence_ids.index(sentence_id)
                for sentence_id in sentence_ids
                if sentence_id in chunk_sentence_ids
            ]
            if len(positions) != len(sentence_ids):
                collector.add("evidence_sentence_outside_chunk", "evidence", evidence_id)
            elif not _sequence_is_consecutive(positions):
                collector.add("evidence_sentences_not_consecutive", "evidence", evidence_id)

        placeholders = ",".join("?" for _ in sentence_ids)
        try:
            sentence_rows = connection.execute(
                f"""
                SELECT sentence_id, page_id, seq, normalized_text, text_sha256
                FROM sentences
                WHERE sentence_id IN ({placeholders})
                """,
                sentence_ids,
            ).fetchall()
        except sqlite3.DatabaseError:
            collector.add("evidence_sentence_lookup_failed", "evidence", evidence_id)
            continue
        by_id = {str(sentence_row[0]): sentence_row for sentence_row in sentence_rows}
        if set(by_id) != set(sentence_ids):
            collector.add("evidence_sentence_missing", "evidence", evidence_id)
            continue
        ordered = [by_id[sentence_id] for sentence_id in sentence_ids]
        if any(str(sentence_row[1]) != page_id for sentence_row in ordered):
            collector.add("evidence_sentence_page_mismatch", "evidence", evidence_id)
        sequences = [int(sentence_row[2]) for sentence_row in ordered]
        if not _sequence_is_consecutive(sequences):
            collector.add("evidence_sentence_sequence_invalid", "evidence", evidence_id)
        for sentence_row in ordered:
            expected_hash = _digest_text(str(sentence_row[3]))
            if sentence_row[4] != expected_hash:
                collector.add(
                    "evidence_database_sentence_hash_mismatch",
                    "sentence",
                    sentence_row[0],
                    expected_hash=expected_hash,
                )

        expected_quote = "\n".join(str(sentence_row[3]) for sentence_row in ordered)
        expected_quote_hash = _digest_text(expected_quote)
        expected_matched_terms = [
            term
            for term in package_query_terms
            if term.casefold() in expected_quote.casefold()
        ]
        expected_unmatched_terms = [
            term for term in package_query_terms if term not in expected_matched_terms
        ]
        has_term_diagnostics = any(
            field in item
            for field in (
                "matched_query_terms",
                "unmatched_query_terms",
                "selection_basis",
            )
        )
        if has_term_diagnostics:
            if item.get("matched_query_terms") != expected_matched_terms:
                collector.add("evidence_matched_terms_mismatch", "evidence", evidence_id)
            if item.get("unmatched_query_terms") != expected_unmatched_terms:
                collector.add("evidence_unmatched_terms_mismatch", "evidence", evidence_id)
            if item.get("selection_basis") != "lexical_match_in_sentence_record":
                collector.add("evidence_selection_basis_invalid", "evidence", evidence_id)
        quote = item.get("quote")
        if not isinstance(quote, str):
            collector.add("evidence_quote_invalid", "evidence", evidence_id)
            quote_hash = None
        else:
            quote_hash = _digest_text(quote)
            if quote != expected_quote:
                collector.add(
                    "evidence_quote_mismatch",
                    "evidence",
                    evidence_id,
                    expected_hash=expected_quote_hash,
                    actual_hash=quote_hash,
                )
            if len(quote) > MAX_QUOTE_CHARS:
                collector.add(
                    "evidence_quote_too_long",
                    "evidence",
                    evidence_id,
                    maximum=MAX_QUOTE_CHARS,
                    actual=len(quote),
                )
            if item.get("quote_char_count") != len(quote):
                collector.add("evidence_quote_char_count_mismatch", "evidence", evidence_id)
        if item.get("quote_sha256") != expected_quote_hash:
            collector.add(
                "evidence_quote_hash_mismatch",
                "evidence",
                evidence_id,
                expected_hash=expected_quote_hash,
            )
        if quote_hash is not None and item.get("quote_sha256") != quote_hash:
            collector.add("evidence_declared_quote_hash_invalid", "evidence", evidence_id)
        if item.get("quote_source_field") != "sentences.normalized_text":
            collector.add("evidence_quote_source_invalid", "evidence", evidence_id)
        expected_inference_level = "direct_quote"
        if row[8] == "ocr":
            expected_inference_level = (
                "ocr_verified_quote"
                if row[9] == "ocr_verified"
                else "ocr_unverified_quote"
            )
        if item.get("inference_level") != expected_inference_level:
            collector.add("evidence_inference_level_invalid", "evidence", evidence_id)
        if is_v2_package:
            provenance_fields = {
                "text_source": str(row[8]),
                "text_verification_status": str(row[9]),
                "ocr_status": str(row[10]),
                "ocr_avg_confidence": row[11],
                "ocr_text_sha256": row[12],
            }
            for field, expected in provenance_fields.items():
                if not _mapping_field_matches(item, field, expected):
                    collector.add(
                        "evidence_database_field_mismatch",
                        "evidence",
                        evidence_id,
                        field=field,
                    )

        logged = run_results.get(chunk_id)
        if run is not None and logged is None:
            collector.add("evidence_chunk_not_in_run_results", "evidence", evidence_id)
        elif logged is not None:
            logged_scores = logged.get("scores")
            if not _score_mapping_valid(logged_scores):
                collector.add("evidence_run_scores_invalid", "retrieval_run", run_id)
            else:
                if item.get("retrieval_scores") != logged_scores:
                    collector.add("evidence_retrieval_scores_mismatch", "evidence", evidence_id)
                assert isinstance(logged_scores, Mapping)
                if item.get("retrieval_score") != logged_scores.get("final"):
                    collector.add("evidence_retrieval_score_mismatch", "evidence", evidence_id)
    return collector.issues


def _verify_retrieval_runs(
    connection: sqlite3.Connection,
    chunk_ids: set[str],
    collector: _IssueCollector,
) -> int:
    count = 0
    try:
        cursor = connection.execute(
            """
            SELECT run_id, filters_json, pipeline_version, index_version,
                   candidates_json, results_json, created_at
            FROM retrieval_runs
            ORDER BY run_id
            """
        )
        for row in cursor:
            count += 1
            run_id = str(row[0])
            if row[2] != PIPELINE_VERSION:
                collector.add(
                    "retrieval_pipeline_version_mismatch",
                    "retrieval_run",
                    run_id,
                    expected=PIPELINE_VERSION,
                )
            if row[3] != INDEX_VERSION:
                collector.add(
                    "retrieval_index_version_mismatch",
                    "retrieval_run",
                    run_id,
                    expected=INDEX_VERSION,
                )
            parameters = _json_value(
                row[1],
                collector=collector,
                code="retrieval_filters_json_invalid",
                entity="retrieval_run",
                entity_id=run_id,
                expected_type=dict,
            )
            candidates = _json_value(
                row[4],
                collector=collector,
                code="retrieval_candidates_json_invalid",
                entity="retrieval_run",
                entity_id=run_id,
                expected_type=list,
            )
            results = _json_value(
                row[5],
                collector=collector,
                code="retrieval_results_json_invalid",
                entity="retrieval_run",
                entity_id=run_id,
                expected_type=list,
            )
            if isinstance(parameters, Mapping):
                filters = parameters.get("filters")
                if filters is None and all(
                    field in parameters
                    for field in ("book_ids", "jurisdictions", "subjects")
                ):
                    filters = parameters
                if _expected_corpus_scope(connection, filters) is None:
                    collector.add("retrieval_filter_structure_invalid", "retrieval_run", run_id)
            candidate_ids: set[str] = set()
            if isinstance(candidates, list):
                for index, candidate in enumerate(candidates, start=1):
                    if not isinstance(candidate, Mapping) or not isinstance(
                        candidate.get("chunk_id"), str
                    ):
                        collector.add(
                            "retrieval_candidate_invalid",
                            "retrieval_run",
                            run_id,
                            position=index,
                        )
                        continue
                    chunk_id = candidate["chunk_id"]
                    if chunk_id in candidate_ids:
                        collector.add("retrieval_candidate_duplicate", "chunk", chunk_id)
                    candidate_ids.add(chunk_id)
                    if chunk_id not in chunk_ids:
                        collector.add("retrieval_candidate_chunk_missing", "chunk", chunk_id)
                    if not _score_mapping_valid(candidate.get("scores")):
                        collector.add("retrieval_candidate_scores_invalid", "chunk", chunk_id)
            result_ids: set[str] = set()
            result_ranks: list[int] = []
            if isinstance(results, list):
                for result in results:
                    if not isinstance(result, Mapping) or not isinstance(
                        result.get("chunk_id"), str
                    ):
                        collector.add("retrieval_result_invalid", "retrieval_run", run_id)
                        continue
                    chunk_id = result["chunk_id"]
                    if chunk_id in result_ids:
                        collector.add("retrieval_result_duplicate", "chunk", chunk_id)
                    result_ids.add(chunk_id)
                    rank = result.get("rank")
                    if isinstance(rank, int) and not isinstance(rank, bool):
                        result_ranks.append(rank)
                    else:
                        collector.add("retrieval_result_rank_invalid", "chunk", chunk_id)
                    if chunk_id not in candidate_ids:
                        collector.add("retrieval_result_not_candidate", "chunk", chunk_id)
                    if chunk_id not in chunk_ids:
                        collector.add("retrieval_result_chunk_missing", "chunk", chunk_id)
                    if not _score_mapping_valid(result.get("scores")):
                        collector.add("retrieval_result_scores_invalid", "chunk", chunk_id)
                if result_ranks != list(range(1, len(result_ranks) + 1)):
                    collector.add("retrieval_result_ranks_not_consecutive", "retrieval_run", run_id)
            try:
                datetime.fromisoformat(str(row[6]).replace("Z", "+00:00"))
            except ValueError:
                collector.add("retrieval_created_at_invalid", "retrieval_run", run_id)
    except sqlite3.DatabaseError:
        collector.add("retrieval_runs_query_failed", "database")
    return count


def _verify_saved_evidence(
    connection: sqlite3.Connection,
    paths: Sequence[Path],
    collector: _IssueCollector,
) -> tuple[int, int]:
    package_count = 0
    item_count = 0
    for path in paths:
        package_id = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            collector.add("evidence_file_missing", "evidence_package", package_id)
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            collector.add("evidence_file_invalid", "evidence_package", package_id)
            continue
        package_count += 1
        if not isinstance(payload, Mapping):
            collector.add("evidence_package_not_object", "evidence_package", package_id)
            continue
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            item_count += len(evidence)
        issues = verify_evidence_package(connection, payload)
        for issue in issues:
            enriched = dict(issue)
            enriched["package"] = _safe_identifier(package_id)
            collector.extend([enriched])
    return package_count, item_count


def verify_corpus(
    *,
    database_path: Path,
    project_root: Path,
    config_path: Path = DEFAULT_CONFIG_PATH,
    maps_path: Path = DEFAULT_PAGE_MAPS_PATH,
    evidence_paths: Iterable[Path] | None = None,
    scan_saved_evidence: bool = True,
) -> dict[str, Any]:
    """Run a complete read-only verification of the local corpus and evidence."""
    collector = _IssueCollector()
    checks: dict[str, Any] = {
        "database_read_only": True,
        "pdf_files_checked": 0,
        "pages_checked": 0,
        "page_map_resolutions_checked": 0,
        "sentences_checked": 0,
        "chunks_checked": 0,
        "fts_rows_checked": 0,
        "retrieval_runs_checked": 0,
        "evidence_packages_checked": 0,
        "evidence_items_checked": 0,
    }
    expected_versions = {
        "index_version": INDEX_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "segmentation_version": SEGMENTATION_VERSION,
    }
    books = load_books(config_path)
    enabled_books = [book for book in books if book.enabled]
    expected_books = {book.book_id: book for book in enabled_books}
    repo_root = project_root.resolve().parent

    actual_inventory: dict[str, dict[str, Any]] = {}
    for book in enabled_books:
        try:
            path = resolve_book_path(repo_root, book.file_path)
        except LegalAIError:
            collector.add("pdf_path_outside_repository", "book", book.book_id)
            continue
        if not path.is_file():
            collector.add("pdf_file_missing", "book", book.book_id)
            continue
        try:
            digest = file_sha256(path)
            page_count = len(PdfReader(path).pages)
        except Exception:
            collector.add("pdf_file_unreadable", "book", book.book_id)
            continue
        checks["pdf_files_checked"] += 1
        actual_inventory[book.book_id] = {
            "sha256": digest,
            "pdf_page_count": page_count,
        }
        if page_count != book.expected_pdf_pages:
            collector.add(
                "pdf_config_page_count_mismatch",
                "book",
                book.book_id,
                expected=book.expected_pdf_pages,
                actual=page_count,
            )

    ocr_records_by_book: dict[str, dict[int, dict[str, Any]]] = {}
    for book in enabled_books:
        inventory = actual_inventory.get(book.book_id)
        if inventory is None or book.ocr_policy == "off":
            continue
        try:
            ocr_records_by_book[book.book_id] = load_ocr_pages(
                ocr_pages_path(project_root, book.book_id),
                book_id=book.book_id,
                source_pdf_sha256=str(inventory["sha256"]),
                pdf_page_count=int(inventory["pdf_page_count"]),
            )
        except LegalAIError:
            collector.add("ocr_raw_contract_invalid", "book", book.book_id)

    connection = _read_only_connection(database_path)
    try:
        try:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity_ok = len(integrity_rows) == 1 and integrity_rows[0][0] == "ok"
            checks["sqlite_integrity_check"] = "ok" if integrity_ok else "failed"
            if not integrity_ok:
                collector.add(
                    "sqlite_integrity_check_failed",
                    "database",
                    failure_count=len(integrity_rows),
                )
        except sqlite3.DatabaseError:
            checks["sqlite_integrity_check"] = "failed"
            collector.add("sqlite_integrity_check_unavailable", "database")
        try:
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            checks["foreign_key_violations"] = len(foreign_keys)
            for row in foreign_keys[:10]:
                collector.add(
                    "sqlite_foreign_key_violation",
                    "database",
                    table=row[0],
                    row_id=row[1],
                    parent=row[2],
                )
        except sqlite3.DatabaseError:
            collector.add("sqlite_foreign_key_check_unavailable", "database")

        try:
            schema_rows = connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        except sqlite3.DatabaseError:
            collector.add("sqlite_schema_unreadable", "database")
            schema_rows = []
        schema = {str(row[0]): row[1] for row in schema_rows}
        missing_tables = sorted(REQUIRED_TABLES - schema.keys())
        for table in missing_tables:
            collector.add("sqlite_required_table_missing", "table", table)
        if "chunks_fts" in schema and "trigram" not in str(schema["chunks_fts"]).casefold():
            collector.add("fts_tokenizer_mismatch", "table", "chunks_fts")
        try:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            checks["sqlite_user_version"] = user_version
            if user_version != INDEX_SCHEMA_VERSION:
                collector.add(
                    "sqlite_user_version_mismatch",
                    "database",
                    expected=INDEX_SCHEMA_VERSION,
                    actual=user_version,
                )
        except sqlite3.DatabaseError:
            collector.add("sqlite_user_version_unavailable", "database")

        if missing_tables:
            return _finalize_report(
                database_path=database_path,
                collector=collector,
                checks=checks,
                expected_versions=expected_versions,
                actual_versions={},
                counts={},
            )

        actual_versions = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key, value FROM index_metadata"
            ).fetchall()
        }
        for key, expected in expected_versions.items():
            if actual_versions.get(key) != expected:
                collector.add(
                    "index_metadata_version_mismatch",
                    "index_metadata",
                    key,
                    expected=expected,
                    actual=actual_versions.get(key),
                )
        if "built_at" not in actual_versions:
            collector.add("index_built_at_missing", "index_metadata", "built_at")
        else:
            try:
                datetime.fromisoformat(actual_versions["built_at"].replace("Z", "+00:00"))
            except ValueError:
                collector.add("index_built_at_invalid", "index_metadata", "built_at")

        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "books",
                "pages",
                "sentences",
                "chunks",
                "chunks_fts",
                "retrieval_runs",
            )
        }
        checks["database_counts"] = counts

        book_rows = {
            str(row[0]): row
            for row in connection.execute(
                """
                SELECT book_id, title, subtitle, edition, volume, metadata_json,
                       file_path, sha256, pdf_page_count, pipeline_version
                FROM books
                ORDER BY book_id
                """
            ).fetchall()
        }
        for book_id in sorted(expected_books.keys() - book_rows.keys()):
            collector.add("configured_book_missing_from_index", "book", book_id)
        for book_id in sorted(book_rows.keys() - expected_books.keys()):
            collector.add("unexpected_book_in_index", "book", book_id)
        for book_id in sorted(expected_books.keys() & book_rows.keys()):
            book = expected_books[book_id]
            row = book_rows[book_id]
            expected_fields = {
                "title": book.title,
                "subtitle": book.subtitle,
                "edition": book.edition,
                "volume": book.volume,
                "file_path": book.file_path,
                "pipeline_version": PIPELINE_VERSION,
            }
            actual_fields = {
                "title": row[1],
                "subtitle": row[2],
                "edition": row[3],
                "volume": row[4],
                "file_path": row[6],
                "pipeline_version": row[9],
            }
            for field, expected in expected_fields.items():
                if actual_fields[field] != expected:
                    collector.add("book_field_mismatch", "book", book_id, field=field)
            metadata = _json_value(
                row[5],
                collector=collector,
                code="book_metadata_json_invalid",
                entity="book",
                entity_id=book_id,
                expected_type=dict,
            )
            if metadata is not None and metadata != _expected_metadata(book):
                collector.add("book_metadata_mismatch", "book", book_id)
            if int(row[8]) != book.expected_pdf_pages:
                collector.add(
                    "book_config_page_count_mismatch",
                    "book",
                    book_id,
                    expected=book.expected_pdf_pages,
                    actual=int(row[8]),
                )
            jurisdictions = {
                str(item[0])
                for item in connection.execute(
                    "SELECT jurisdiction FROM book_jurisdictions WHERE book_id=?",
                    (book_id,),
                ).fetchall()
            }
            subjects = {
                str(item[0])
                for item in connection.execute(
                    "SELECT subject FROM book_subjects WHERE book_id=?",
                    (book_id,),
                ).fetchall()
            }
            if jurisdictions != set(book.jurisdictions):
                collector.add("book_jurisdictions_mismatch", "book", book_id)
            if subjects != set(book.subjects):
                collector.add("book_subjects_mismatch", "book", book_id)
            actual = actual_inventory.get(book_id)
            if actual is not None:
                if row[7] != actual["sha256"]:
                    collector.add(
                        "pdf_index_hash_mismatch",
                        "book",
                        book_id,
                        expected_hash=row[7],
                        actual_hash=actual["sha256"],
                    )
                if int(row[8]) != actual["pdf_page_count"]:
                    collector.add(
                        "pdf_index_page_count_mismatch",
                        "book",
                        book_id,
                        expected=int(row[8]),
                        actual=actual["pdf_page_count"],
                    )

        page_counts_for_maps = {
            book.book_id: book.expected_pdf_pages for book in enabled_books
        }
        maps_valid = True
        try:
            maps = page_map_by_book(
                load_page_maps(
                    maps_path,
                    page_counts_for_maps,
                    allow_unconfigured_books=True,
                )
            )
        except LegalAIError:
            maps_valid = False
            maps = {}
            collector.add("page_map_config_invalid", "page_map")

        expected_page_seq: dict[str, int] = {}
        indexed_page_counts: dict[str, int] = {}
        chunk_ids: set[str] = set()
        page_cursor = connection.execute(
            """
            SELECT page_id, book_id, pdf_page, printed_page,
                   printed_page_status, chapter_path_json, raw_text,
                   normalized_text, raw_text_sha256, char_count,
                   quality_flags_json, text_source,
                   text_verification_status, ocr_status, ocr_engine,
                   ocr_avg_confidence, ocr_text_sha256
            FROM pages
            ORDER BY book_id, pdf_page
            """
        )
        for page in page_cursor:
            checks["pages_checked"] += 1
            page_id = str(page[0])
            book_id = str(page[1])
            pdf_page = int(page[2])
            indexed_page_counts[book_id] = indexed_page_counts.get(book_id, 0) + 1
            expected_sequence = expected_page_seq.get(book_id, 0) + 1
            expected_page_seq[book_id] = expected_sequence
            if pdf_page != expected_sequence:
                collector.add(
                    "page_sequence_not_contiguous",
                    "page",
                    page_id,
                    expected=expected_sequence,
                    actual=pdf_page,
                )
            if page_id != f"{book_id}:p{pdf_page:04d}":
                collector.add("page_id_not_canonical", "page", page_id)
            status = str(page[4])
            if status not in PAGE_MAP_STATUSES:
                collector.add("page_status_invalid", "page", page_id)
            elif status in {"unknown", "rejected"} and page[3] is not None:
                collector.add("unconfirmed_page_has_label", "page", page_id)
            elif status in {"detected", "verified"} and page[3] is None:
                collector.add("mapped_page_missing_label", "page", page_id)
            chapter = _json_value(
                page[5],
                collector=collector,
                code="page_chapter_json_invalid",
                entity="page",
                entity_id=page_id,
                expected_type=list,
            )
            _json_value(
                page[10],
                collector=collector,
                code="page_quality_json_invalid",
                entity="page",
                entity_id=page_id,
                expected_type=list,
            )
            text_source = str(page[11])
            verification_status = str(page[12])
            ocr_status = str(page[13])
            if text_source not in TEXT_SOURCES:
                collector.add("page_text_source_invalid", "page", page_id)
            if verification_status not in TEXT_VERIFICATION_STATUSES:
                collector.add("page_text_verification_status_invalid", "page", page_id)
            if ocr_status not in OCR_STATUSES:
                collector.add("page_ocr_status_invalid", "page", page_id)
            ocr_confidence = page[15]
            ocr_hash = page[16]
            if ocr_confidence is not None and (
                isinstance(ocr_confidence, bool)
                or not isinstance(ocr_confidence, (int, float))
                or not 0.0 < float(ocr_confidence) <= 1.0
            ):
                collector.add("page_ocr_confidence_invalid", "page", page_id)
            if ocr_hash is not None and (
                not isinstance(ocr_hash, str) or len(ocr_hash) != 64
            ):
                collector.add("page_ocr_text_hash_invalid", "page", page_id)
            if text_source == "embedded_text" and verification_status != "embedded_text":
                collector.add("page_text_provenance_invalid", "page", page_id)
            if text_source in {"ocr", "hybrid"} and verification_status not in {
                "ocr_unverified",
                "ocr_verified",
            }:
                collector.add("page_text_provenance_invalid", "page", page_id)
            if text_source == "ocr" and ocr_status != "generated":
                collector.add("page_text_provenance_invalid", "page", page_id)
            raw_text = page[6]
            raw_hash = page[8]
            if raw_text is None:
                if raw_hash is not None:
                    collector.add("page_null_raw_text_has_hash", "page", page_id)
            else:
                expected_raw_hash = _digest_text(str(raw_text))
                if raw_hash != expected_raw_hash:
                    collector.add(
                        "page_raw_text_hash_mismatch",
                        "page",
                        page_id,
                        expected_hash=expected_raw_hash,
                    )
            normalized_text = str(page[7])
            if int(page[9]) != len(normalized_text):
                collector.add("page_char_count_mismatch", "page", page_id)
            if text_source == "ocr":
                ocr_record = ocr_records_by_book.get(book_id, {}).get(pdf_page)
                if ocr_record is None:
                    collector.add("page_ocr_record_missing", "page", page_id)
                    source_text_for_sentences = ""
                    normalized_page = normalize_page_text(source_text_for_sentences)
                else:
                    if ocr_record.get("ocr_status") != "generated":
                        collector.add("page_ocr_record_not_generated", "page", page_id)
                    if ocr_record.get("ocr_text_sha256") != ocr_hash:
                        collector.add("page_ocr_text_hash_mismatch", "page", page_id)
                    if ocr_record.get("ocr_avg_confidence") != ocr_confidence:
                        collector.add("page_ocr_confidence_mismatch", "page", page_id)
                    source_text_for_sentences = str(ocr_record.get("ocr_text") or "")
                    normalized_page = normalize_page_text(source_text_for_sentences)
            elif text_source == "hybrid":
                # Stage B reserves hybrid for a later explicit merge contract.
                collector.add("page_hybrid_source_not_supported", "page", page_id)
                source_text_for_sentences = ""
                normalized_page = normalize_page_text(source_text_for_sentences)
            elif text_source == "empty":
                # A registered frontmatter/blank exclusion deliberately wins
                # over any preserved pypdf text on the source page.
                source_text_for_sentences = ""
                normalized_page = normalize_page_text(source_text_for_sentences)
            else:
                source_text_for_sentences = str(raw_text or "")
                normalized_page = normalize_page_text(source_text_for_sentences)
            normalized_from_raw = normalized_page.text
            if normalized_text != normalized_from_raw:
                collector.add(
                    "page_normalization_mismatch",
                    "page",
                    page_id,
                    expected_hash=_digest_text(normalized_from_raw),
                    actual_hash=_digest_text(normalized_text),
                )
            if maps_valid:
                resolution = resolve_page(maps.get(book_id), pdf_page)
                checks["page_map_resolutions_checked"] += 1
                if page[3] != resolution.printed_page or status != resolution.status:
                    collector.add("page_map_resolution_mismatch", "page", page_id)

            sentence_by_id: dict[str, tuple[int, str]] = {}
            expected_sentence_seq = 0
            sentence_cursor = connection.execute(
                """
                SELECT sentence_id, seq, raw_text, normalized_text,
                       start_offset, end_offset, text_sha256,
                       normalization_version, segmentation_version
                FROM sentences
                WHERE page_id=?
                ORDER BY seq, sentence_id
                """,
                (page_id,),
            )
            for sentence in sentence_cursor:
                checks["sentences_checked"] += 1
                sentence_id = str(sentence[0])
                sequence = int(sentence[1])
                expected_sentence_seq += 1
                if sequence != expected_sentence_seq:
                    collector.add(
                        "sentence_sequence_not_contiguous",
                        "sentence",
                        sentence_id,
                        expected=expected_sentence_seq,
                        actual=sequence,
                    )
                if sentence_id != f"{page_id}:s{sequence:03d}":
                    collector.add("sentence_id_not_canonical", "sentence", sentence_id)
                sentence_text = str(sentence[3])
                start = int(sentence[4])
                end = int(sentence[5])
                if not 0 <= start <= end <= len(normalized_text):
                    collector.add("sentence_offsets_out_of_range", "sentence", sentence_id)
                elif normalized_text[start:end] != sentence_text:
                    collector.add(
                        "sentence_offset_text_mismatch",
                        "sentence",
                        sentence_id,
                        expected_hash=_digest_text(normalized_text[start:end]),
                        actual_hash=_digest_text(sentence_text),
                    )
                if 0 <= start <= end <= len(normalized_page.text):
                    expected_raw_slice = normalized_page.raw_slice_for_normalized_span(
                        source_text_for_sentences, start, end
                    )
                    if str(sentence[2]) != expected_raw_slice:
                        collector.add(
                            "sentence_raw_slice_mismatch",
                            "sentence",
                            sentence_id,
                            expected_hash=_digest_text(expected_raw_slice),
                            actual_hash=_digest_text(str(sentence[2])),
                        )
                else:
                    collector.add(
                        "sentence_raw_mapping_offsets_out_of_range",
                        "sentence",
                        sentence_id,
                    )
                expected_sentence_hash = _digest_text(sentence_text)
                if sentence[6] != expected_sentence_hash:
                    collector.add(
                        "sentence_text_hash_mismatch",
                        "sentence",
                        sentence_id,
                        expected_hash=expected_sentence_hash,
                    )
                if sentence[7] != NORMALIZATION_VERSION:
                    collector.add(
                        "sentence_normalization_version_mismatch",
                        "sentence",
                        sentence_id,
                        expected=NORMALIZATION_VERSION,
                    )
                if sentence[8] != SEGMENTATION_VERSION:
                    collector.add(
                        "sentence_segmentation_version_mismatch",
                        "sentence",
                        sentence_id,
                        expected=SEGMENTATION_VERSION,
                    )
                sentence_by_id[sentence_id] = (sequence, sentence_text)

            covered_sentences: set[str] = set()
            expected_chunk_seq = 0
            chunk_cursor = connection.execute(
                """
                SELECT chunk_id, seq, first_sentence_id, last_sentence_id,
                       sentence_ids_json, text, char_count,
                       chapter_path_json, segmentation_version
                FROM chunks
                WHERE page_id=?
                ORDER BY seq, chunk_id
                """,
                (page_id,),
            )
            for chunk in chunk_cursor:
                checks["chunks_checked"] += 1
                chunk_id = str(chunk[0])
                chunk_ids.add(chunk_id)
                sequence = int(chunk[1])
                expected_chunk_seq += 1
                if sequence != expected_chunk_seq:
                    collector.add(
                        "chunk_sequence_not_contiguous",
                        "chunk",
                        chunk_id,
                        expected=expected_chunk_seq,
                        actual=sequence,
                    )
                if chunk_id != f"{page_id}:c{sequence:03d}":
                    collector.add("chunk_id_not_canonical", "chunk", chunk_id)
                sentence_ids = _json_value(
                    chunk[4],
                    collector=collector,
                    code="chunk_sentence_ids_json_invalid",
                    entity="chunk",
                    entity_id=chunk_id,
                    expected_type=list,
                )
                if not _list_of_strings(sentence_ids, allow_empty=False):
                    collector.add("chunk_sentence_ids_invalid", "chunk", chunk_id)
                    continue
                assert isinstance(sentence_ids, list)
                if sentence_ids[0] != chunk[2] or sentence_ids[-1] != chunk[3]:
                    collector.add("chunk_boundaries_mismatch", "chunk", chunk_id)
                if any(sentence_id not in sentence_by_id for sentence_id in sentence_ids):
                    collector.add("chunk_sentence_missing_or_cross_page", "chunk", chunk_id)
                    continue
                sequences = [sentence_by_id[sentence_id][0] for sentence_id in sentence_ids]
                if not _sequence_is_consecutive(sequences):
                    collector.add("chunk_sentence_sequence_invalid", "chunk", chunk_id)
                expected_chunk_text = "\n".join(
                    sentence_by_id[sentence_id][1] for sentence_id in sentence_ids
                )
                if chunk[5] != expected_chunk_text:
                    collector.add(
                        "chunk_text_mismatch",
                        "chunk",
                        chunk_id,
                        expected_hash=_digest_text(expected_chunk_text),
                        actual_hash=_digest_text(str(chunk[5])),
                    )
                if int(chunk[6]) != len(str(chunk[5])):
                    collector.add("chunk_char_count_mismatch", "chunk", chunk_id)
                chunk_chapter = _json_value(
                    chunk[7],
                    collector=collector,
                    code="chunk_chapter_json_invalid",
                    entity="chunk",
                    entity_id=chunk_id,
                    expected_type=list,
                )
                if chapter is not None and chunk_chapter != chapter:
                    collector.add("chunk_chapter_mismatch", "chunk", chunk_id)
                if chunk[8] != SEGMENTATION_VERSION:
                    collector.add(
                        "chunk_segmentation_version_mismatch",
                        "chunk",
                        chunk_id,
                        expected=SEGMENTATION_VERSION,
                    )
                covered_sentences.update(sentence_ids)
            missing_sentence_coverage = set(sentence_by_id) - covered_sentences
            if missing_sentence_coverage:
                collector.add(
                    "page_sentences_not_chunked",
                    "page",
                    page_id,
                    count=len(missing_sentence_coverage),
                    sample=sorted(missing_sentence_coverage)[:3],
                )

        for book_id, row in book_rows.items():
            expected_count = int(row[8])
            actual_count = indexed_page_counts.get(book_id, 0)
            if actual_count != expected_count:
                collector.add(
                    "book_indexed_page_count_mismatch",
                    "book",
                    book_id,
                    expected=expected_count,
                    actual=actual_count,
                )

        fts_count = counts["chunks_fts"]
        if counts["chunks"] != fts_count:
            collector.add(
                "fts_chunk_count_mismatch",
                "table",
                "chunks_fts",
                expected=counts["chunks"],
                actual=fts_count,
            )
        missing_fts = connection.execute(
            "SELECT chunk_id FROM chunks EXCEPT SELECT chunk_id FROM chunks_fts LIMIT 10"
        ).fetchall()
        extra_fts = connection.execute(
            "SELECT chunk_id FROM chunks_fts EXCEPT SELECT chunk_id FROM chunks LIMIT 10"
        ).fetchall()
        duplicate_fts = connection.execute(
            """
            SELECT chunk_id FROM chunks_fts
            GROUP BY chunk_id HAVING COUNT(*) <> 1 LIMIT 10
            """
        ).fetchall()
        for row in missing_fts:
            collector.add("chunk_missing_from_fts", "chunk", row[0])
        for row in extra_fts:
            collector.add("unknown_chunk_in_fts", "chunk", row[0])
        for row in duplicate_fts:
            collector.add("duplicate_chunk_in_fts", "chunk", row[0])

        fts_cursor = connection.execute(
            """
            SELECT f.chunk_id, f.text, f.title, f.chapter_path,
                   f.subjects, f.jurisdictions,
                   c.text, c.chapter_path_json,
                   b.title, b.subtitle, b.metadata_json
            FROM chunks_fts f
            JOIN chunks c ON c.chunk_id=f.chunk_id
            JOIN pages p ON p.page_id=c.page_id
            JOIN books b ON b.book_id=p.book_id
            ORDER BY f.chunk_id
            """
        )
        for row in fts_cursor:
            checks["fts_rows_checked"] += 1
            chunk_id = str(row[0])
            metadata = _json_value(
                row[10],
                collector=collector,
                code="book_metadata_json_invalid",
                entity="chunk",
                entity_id=chunk_id,
                expected_type=dict,
            )
            chapter = _json_value(
                row[7],
                collector=collector,
                code="chunk_chapter_json_invalid",
                entity="chunk",
                entity_id=chunk_id,
                expected_type=list,
            )
            expected_title = f"{row[8]} {row[9]}" if row[9] else str(row[8])
            expected_fields: dict[str, Any] = {
                "text": row[6],
                "title": expected_title,
                "chapter_path": (
                    " > ".join(str(item) for item in chapter)
                    if isinstance(chapter, list)
                    else None
                ),
                "subjects": (
                    " ".join(metadata.get("subjects", []))
                    if isinstance(metadata, Mapping)
                    and _list_of_strings(metadata.get("subjects", []))
                    else None
                ),
                "jurisdictions": (
                    " ".join(metadata.get("jurisdictions", []))
                    if isinstance(metadata, Mapping)
                    and _list_of_strings(metadata.get("jurisdictions", []))
                    else None
                ),
            }
            actual_fields = {
                "text": row[1],
                "title": row[2],
                "chapter_path": row[3],
                "subjects": row[4],
                "jurisdictions": row[5],
            }
            for field, expected in expected_fields.items():
                if expected is not None and actual_fields[field] != expected:
                    collector.add("fts_field_mismatch", "chunk", chunk_id, field=field)

        checks["retrieval_runs_checked"] = _verify_retrieval_runs(
            connection, chunk_ids, collector
        )

        paths: list[Path] = []
        if scan_saved_evidence:
            paths.extend(sorted((project_root / "data" / "evidence").glob("*.json")))
        paths.extend(Path(path) for path in evidence_paths or ())
        unique_paths: list[Path] = []
        seen_paths: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved not in seen_paths:
                unique_paths.append(path)
                seen_paths.add(resolved)
        package_count, item_count = _verify_saved_evidence(
            connection, unique_paths, collector
        )
        checks["evidence_packages_checked"] = package_count
        checks["evidence_items_checked"] = item_count

        return _finalize_report(
            database_path=database_path,
            collector=collector,
            checks=checks,
            expected_versions=expected_versions,
            actual_versions=actual_versions,
            counts=counts,
        )
    except (sqlite3.DatabaseError, TypeError, ValueError, KeyError):
        collector.add("sqlite_verification_query_failed", "database")
        return _finalize_report(
            database_path=database_path,
            collector=collector,
            checks=checks,
            expected_versions=expected_versions,
            actual_versions={},
            counts={},
        )
    finally:
        connection.close()


def _finalize_report(
    *,
    database_path: Path,
    collector: _IssueCollector,
    checks: Mapping[str, Any],
    expected_versions: Mapping[str, str],
    actual_versions: Mapping[str, str],
    counts: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "verification_version": VERIFICATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": _safe_identifier(database_path.name),
        "ok": collector.total == 0,
        "error_count": collector.total,
        "reported_issue_count": len(collector.issues),
        "issues_truncated": collector.total > len(collector.issues),
        "issues": collector.issues,
        "expected_versions": dict(expected_versions),
        "actual_versions": {
            key: _safe_detail(actual_versions.get(key)) for key in expected_versions
        },
        "counts": dict(counts),
        "checks": dict(checks),
    }


def write_verification_report(
    path: Path, report: Mapping[str, Any]
) -> None:
    """Atomically write the short, non-corpus verification report."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def render_verification_text(report: Mapping[str, Any]) -> str:
    """Render a concise summary without copying any corpus text."""

    checks_value = report.get("checks")
    checks = checks_value if isinstance(checks_value, Mapping) else {}
    counts_value = report.get("counts")
    counts = counts_value if isinstance(counts_value, Mapping) else {}
    state = "通过" if report.get("ok") else "未通过"
    lines = [
        f"P5 全链路校验：{state}",
        f"错误数：{report.get('error_count', 0)}",
        (
            "数据库："
            f"books={counts.get('books', 0)}, pages={counts.get('pages', 0)}, "
            f"sentences={counts.get('sentences', 0)}, chunks={counts.get('chunks', 0)}, "
            f"FTS={counts.get('chunks_fts', 0)}"
        ),
        (
            "只读检查："
            f"PDF={checks.get('pdf_files_checked', 0)}, "
            f"页码映射={checks.get('page_map_resolutions_checked', 0)}, "
            f"检索运行={checks.get('retrieval_runs_checked', 0)}, "
            f"证据包={checks.get('evidence_packages_checked', 0)}, "
            f"证据={checks.get('evidence_items_checked', 0)}"
        ),
        f"SQLite integrity_check：{checks.get('sqlite_integrity_check', 'unknown')}",
        f"外键违规：{checks.get('foreign_key_violations', 'unknown')}",
    ]
    issues = report.get("issues")
    if isinstance(issues, list) and issues:
        lines.append("问题代码：")
        for issue in issues[:10]:
            if isinstance(issue, Mapping):
                lines.append(
                    f"- {issue.get('code', 'unknown')} "
                    f"({issue.get('entity', 'unknown')}:{issue.get('id', '-')})"
                )
    return "\n".join(lines) + "\n"

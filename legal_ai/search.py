from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import PIPELINE_VERSION
from .domain import LegalAIError
from .index import INDEX_VERSION
from .normalize import NORMALIZATION_VERSION, normalize_page_text
from .segment import SEGMENTATION_VERSION


MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 32
MAX_RESULT_LIMIT = 50
RANKING_VERSION = "rank-v1"
SCORE_WEIGHTS = {
    "fts": 0.55,
    "exact": 0.25,
    "chapter": 0.10,
    "metadata": 0.10,
}
TERM_SPLIT_PATTERN = re.compile(r"[\s,，。；;、！？!?：:]+")


def _unique(values: Iterable[str] | None) -> list[str]:
    return sorted({str(item).strip() for item in values or () if str(item).strip()})


def _normalize_query(query: str) -> tuple[str, list[str]]:
    normalized = normalize_page_text(query).text.strip()
    if not normalized:
        raise LegalAIError("查询不能为空")
    if len(normalized) > MAX_QUERY_CHARS:
        raise LegalAIError(
            f"查询过长：最多 {MAX_QUERY_CHARS} 个字符，实际 {len(normalized)}"
        )
    terms = [
        item
        for item in TERM_SPLIT_PATTERN.split(normalized)
        if item
    ]
    if not terms:
        raise LegalAIError("查询不包含可检索内容")
    if len(terms) > MAX_QUERY_TERMS:
        raise LegalAIError(
            f"查询词过多：最多 {MAX_QUERY_TERMS} 个，实际 {len(terms)}"
        )
    return normalized, list(dict.fromkeys(terms))


def _fts_literal(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _filter_clause(
    *,
    book_ids: Sequence[str],
    jurisdictions: Sequence[str],
    subjects: Sequence[str],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if book_ids:
        placeholders = ",".join("?" for _ in book_ids)
        clauses.append(f"b.book_id IN ({placeholders})")
        parameters.extend(book_ids)
    if jurisdictions:
        placeholders = ",".join("?" for _ in jurisdictions)
        clauses.append(
            "EXISTS (SELECT 1 FROM book_jurisdictions bj "
            "WHERE bj.book_id = b.book_id "
            f"AND bj.jurisdiction IN ({placeholders}))"
        )
        parameters.extend(jurisdictions)
    if subjects:
        placeholders = ",".join("?" for _ in subjects)
        clauses.append(
            "EXISTS (SELECT 1 FROM book_subjects bs "
            "WHERE bs.book_id = b.book_id "
            f"AND bs.subject IN ({placeholders}))"
        )
        parameters.extend(subjects)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), parameters


def _fts_candidates(
    connection: sqlite3.Connection,
    *,
    term: str,
    filter_sql: str,
    filter_parameters: Sequence[Any],
    candidate_limit: int,
) -> list[tuple[str, float]]:
    sql = f"""
        SELECT c.chunk_id,
               bm25(chunks_fts, 0.0, 1.0, 0.20, 0.20, 0.10, 0.10)
                   AS bm25_rank
        FROM chunks_fts
        JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
        JOIN pages p ON p.page_id = c.page_id
        JOIN books b ON b.book_id = p.book_id
        WHERE chunks_fts MATCH ?{filter_sql}
        ORDER BY bm25_rank ASC, c.chunk_id ASC
        LIMIT ?
    """
    parameters = [_fts_literal(term), *filter_parameters, candidate_limit]
    return [
        (str(row[0]), float(row[1]))
        for row in connection.execute(sql, parameters).fetchall()
    ]


def _substring_candidates(
    connection: sqlite3.Connection,
    *,
    term: str,
    filter_sql: str,
    filter_parameters: Sequence[Any],
    candidate_limit: int,
) -> list[str]:
    sql = f"""
        SELECT c.chunk_id,
               length(c.text) - length(replace(c.text, ?, '')) AS match_chars
        FROM chunks c
        JOIN pages p ON p.page_id = c.page_id
        JOIN books b ON b.book_id = p.book_id
        WHERE (
            instr(c.text, ?) > 0
            OR instr(b.title, ?) > 0
            OR instr(COALESCE(b.subtitle, ''), ?) > 0
            OR instr(p.chapter_path_json, ?) > 0
            OR instr(b.metadata_json, ?) > 0
        ){filter_sql}
        ORDER BY match_chars DESC, c.chunk_id ASC
        LIMIT ?
    """
    parameters = [
        term,
        term,
        term,
        term,
        term,
        term,
        *filter_parameters,
        candidate_limit,
    ]
    return [
        str(row[0])
        for row in connection.execute(sql, parameters).fetchall()
    ]


def _fetch_details(
    connection: sqlite3.Connection, chunk_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    batch_size = 400
    for start in range(0, len(chunk_ids), batch_size):
        batch = chunk_ids[start : start + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT c.chunk_id, c.text, c.char_count, c.page_id,
                   p.book_id, p.pdf_page, p.printed_page,
                   p.printed_page_status, p.chapter_path_json,
                   p.text_source, p.text_verification_status, p.ocr_status,
                   p.ocr_engine, p.ocr_avg_confidence, p.ocr_text_sha256,
                   b.title, b.subtitle, b.file_path, b.metadata_json
            FROM chunks c
            JOIN pages p ON p.page_id = c.page_id
            JOIN books b ON b.book_id = p.book_id
            WHERE c.chunk_id IN ({placeholders})
            """,
            list(batch),
        ).fetchall()
        for row in rows:
            metadata = json.loads(row[18])
            details[str(row[0])] = {
                "chunk_id": str(row[0]),
                "text": str(row[1]),
                "char_count": int(row[2]),
                "page_id": str(row[3]),
                "book_id": str(row[4]),
                "pdf_page": int(row[5]),
                "printed_page": row[6],
                "printed_page_status": str(row[7]),
                "chapter_path": json.loads(row[8]),
                "text_source": str(row[9]),
                "text_verification_status": str(row[10]),
                "ocr_status": str(row[11]),
                "ocr_engine": row[12],
                "ocr_avg_confidence": row[13],
                "ocr_text_sha256": row[14],
                "title": str(row[15]),
                "subtitle": row[16],
                "file_path": str(row[17]),
                "metadata": metadata,
            }
    return details


def _term_fraction(terms: Sequence[str], haystack: str) -> float:
    if not terms:
        return 0.0
    folded = haystack.casefold()
    return sum(term.casefold() in folded for term in terms) / len(terms)


def _exact_text_score(terms: Sequence[str], text: str) -> float:
    if not terms:
        return 0.0
    folded = text.casefold()
    contributions: list[float] = []
    for term in terms:
        count = folded.count(term.casefold())
        if count <= 0:
            contributions.append(0.0)
        else:
            contributions.append(0.70 + 0.30 * min(1.0, count / 3.0))
    return sum(contributions) / len(contributions)


def _snippet(text: str, terms: Sequence[str], max_chars: int = 240) -> str:
    flattened = " ".join(text.split())
    if len(flattened) <= max_chars:
        return flattened
    folded = flattened.casefold()
    positions = [
        folded.find(term.casefold())
        for term in terms
        if folded.find(term.casefold()) >= 0
    ]
    anchor = min(positions) if positions else 0
    start = max(0, anchor - max_chars // 3)
    end = min(len(flattened), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    return (
        ("…" if start else "")
        + flattened[start:end]
        + ("…" if end < len(flattened) else "")
    )


def _score_candidates(
    *,
    terms: Sequence[str],
    details: Mapping[str, dict[str, Any]],
    fts_contributions: Mapping[str, list[float]],
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for chunk_id, detail in details.items():
        chapter_text = " > ".join(
            str(item) for item in detail["chapter_path"]
        )
        metadata = detail["metadata"]
        metadata_text = " ".join(
            [
                detail["title"],
                str(detail.get("subtitle") or ""),
                *[str(item) for item in metadata.get("subjects", [])],
                *[str(item) for item in metadata.get("jurisdictions", [])],
            ]
        )
        fts_score = min(
            1.0,
            sum(fts_contributions.get(chunk_id, [])) / max(1, len(terms)),
        )
        exact_score = _exact_text_score(terms, detail["text"])
        chapter_score = _term_fraction(terms, chapter_text)
        metadata_score = _term_fraction(terms, metadata_text)
        final_score = (
            SCORE_WEIGHTS["fts"] * fts_score
            + SCORE_WEIGHTS["exact"] * exact_score
            + SCORE_WEIGHTS["chapter"] * chapter_score
            + SCORE_WEIGHTS["metadata"] * metadata_score
        )
        if final_score <= 0:
            continue
        matched_terms = [
            term
            for term in terms
            if term.casefold()
            in (detail["text"] + " " + chapter_text + " " + metadata_text).casefold()
        ]
        scored.append(
            {
                "chunk_id": chunk_id,
                "book_id": detail["book_id"],
                "title": detail["title"],
                "subtitle": detail["subtitle"],
                "source_type": metadata.get("source_type"),
                "authority_tier": metadata.get("authority_tier"),
                "jurisdictions": metadata.get("jurisdictions", []),
                "subjects": metadata.get("subjects", []),
                "page_id": detail["page_id"],
                "pdf_page": detail["pdf_page"],
                "printed_page": detail["printed_page"],
                "printed_page_status": detail["printed_page_status"],
                "text_source": detail["text_source"],
                "text_verification_status": detail[
                    "text_verification_status"
                ],
                "ocr_status": detail["ocr_status"],
                "ocr_avg_confidence": detail["ocr_avg_confidence"],
                "chapter_path": detail["chapter_path"],
                "retrieval_snippet": _snippet(detail["text"], terms),
                "matched_terms": matched_terms,
                "scores": {
                    "fts": round(fts_score, 6),
                    "exact": round(exact_score, 6),
                    "chapter": round(chapter_score, 6),
                    "metadata": round(metadata_score, 6),
                    "final": round(final_score, 6),
                },
                "file_path": detail["file_path"],
                "obsidian_pdf_link": (
                    f"[[{detail['file_path']}#page={detail['pdf_page']}\\|"
                    f"打开 PDF 第 {detail['pdf_page']} 页]]"
                ),
            }
        )
    scored.sort(
        key=lambda item: (
            -item["scores"]["final"],
            -item["scores"]["exact"],
            -item["scores"]["fts"],
            item["book_id"],
            item["pdf_page"],
            item["chunk_id"],
        )
    )
    return scored


def _deduplicate_and_diversify(
    scored: Sequence[dict[str, Any]],
    *,
    limit: int,
    book_filter_active: bool,
) -> list[dict[str, Any]]:
    if not scored:
        return []
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    page_counts: defaultdict[str, int] = defaultdict(int)

    def add(item: dict[str, Any]) -> bool:
        if item["chunk_id"] in selected_ids or page_counts[item["page_id"]] >= 2:
            return False
        selected.append(item)
        selected_ids.add(item["chunk_id"])
        page_counts[item["page_id"]] += 1
        return True

    if not book_filter_active:
        best_score = scored[0]["scores"]["final"]
        seen_books: set[str] = set()
        for item in scored:
            if len(selected) >= limit:
                break
            if item["book_id"] in seen_books:
                continue
            if item["scores"]["final"] < best_score * 0.60:
                continue
            if add(item):
                seen_books.add(item["book_id"])

    for item in scored:
        if len(selected) >= limit:
            break
        add(item)
    selected.sort(
        key=lambda item: (
            -item["scores"]["final"],
            -item["scores"]["exact"],
            -item["scores"]["fts"],
            item["book_id"],
            item["pdf_page"],
            item["chunk_id"],
        )
    )
    return selected


def _run_id(query: str, filters: Mapping[str, Any], created_at: str) -> str:
    digest = hashlib.sha256(
        (query + json.dumps(filters, ensure_ascii=False, sort_keys=True) + created_at).encode(
            "utf-8"
        )
    ).hexdigest()[:10]
    compact_time = created_at.replace("-", "").replace(":", "").replace("+00:00", "Z")
    return f"{compact_time}-{digest}"


def search_index(
    *,
    database_path: Path,
    query: str,
    limit: int = 10,
    book_ids: Iterable[str] | None = None,
    jurisdictions: Iterable[str] | None = None,
    subjects: Iterable[str] | None = None,
    record_run: bool = True,
    terms_override: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not database_path.exists():
        raise LegalAIError(
            f"索引不存在：{database_path}；请先运行 build-index --rebuild"
        )
    if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_RESULT_LIMIT:
        raise LegalAIError(f"limit 必须在 1-{MAX_RESULT_LIMIT} 之间")
    normalized_query, terms = _normalize_query(query)
    if terms_override is not None:
        cleaned = [
            str(item).strip() for item in terms_override if str(item).strip()
        ]
        if not cleaned:
            raise LegalAIError("terms 不能为空")
        if len(cleaned) > MAX_QUERY_TERMS:
            raise LegalAIError(
                f"terms 过多：最多 {MAX_QUERY_TERMS} 个，实际 {len(cleaned)}"
            )
        terms = cleaned
    filters = {
        "book_ids": _unique(book_ids),
        "jurisdictions": _unique(jurisdictions),
        "subjects": _unique(subjects),
    }
    filter_sql, filter_parameters = _filter_clause(
        book_ids=filters["book_ids"],
        jurisdictions=filters["jurisdictions"],
        subjects=filters["subjects"],
    )
    candidate_limit = max(100, int(limit) * 20)
    try:
        connection = sqlite3.connect(str(database_path))
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"无法打开 SQLite 索引：{exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        metadata = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key, value FROM index_metadata"
            ).fetchall()
        }
        expected_versions = {
            "index_version": INDEX_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "normalization_version": NORMALIZATION_VERSION,
            "segmentation_version": SEGMENTATION_VERSION,
        }
        mismatches = {
            key: {"expected": expected, "actual": metadata.get(key)}
            for key, expected in expected_versions.items()
            if metadata.get(key) != expected
        }
        if mismatches:
            raise LegalAIError(
                "索引版本不匹配："
                f"{json.dumps(mismatches, ensure_ascii=False, sort_keys=True)}；"
                "请运行 build-index --rebuild"
            )

        corpus_scope = [
            str(row[0])
            for row in connection.execute(
                f"SELECT b.book_id FROM books b WHERE 1=1{filter_sql} "
                "ORDER BY b.book_id",
                list(filter_parameters),
            ).fetchall()
        ]

        candidate_ids: set[str] = set()
        fts_contributions: defaultdict[str, list[float]] = defaultdict(list)
        for term in terms:
            if len(term) >= 3:
                fts_rows = _fts_candidates(
                    connection,
                    term=term,
                    filter_sql=filter_sql,
                    filter_parameters=filter_parameters,
                    candidate_limit=candidate_limit,
                )
                for rank_index, (chunk_id, _raw_bm25) in enumerate(
                    fts_rows, start=1
                ):
                    candidate_ids.add(chunk_id)
                    fts_contributions[chunk_id].append(1.0 / rank_index)
            substring_ids = _substring_candidates(
                connection,
                term=term,
                filter_sql=filter_sql,
                filter_parameters=filter_parameters,
                candidate_limit=candidate_limit,
            )
            candidate_ids.update(substring_ids)

        ordered_ids = sorted(candidate_ids)
        details = _fetch_details(connection, ordered_ids)
        scored = _score_candidates(
            terms=terms,
            details=details,
            fts_contributions=fts_contributions,
        )
        results = _deduplicate_and_diversify(
            scored,
            limit=int(limit),
            book_filter_active=bool(filters["book_ids"]),
        )
        created_at = datetime.now(timezone.utc).isoformat()
        run_id = _run_id(normalized_query, filters, created_at)
        response = {
            "query": normalized_query,
            "query_terms": terms,
            "run_id": run_id,
            "ranking_version": RANKING_VERSION,
            "score_weights": SCORE_WEIGHTS,
            "filters": filters,
            "corpus_scope": corpus_scope,
            "coverage_notice": (
                "当前仅检索本地第一阶段学理书籍，不代表现行法源已经核验。"
            ),
            "result_count": len(results),
            "results": results,
        }
        if record_run:
            candidates_for_log = [
                {
                    "chunk_id": item["chunk_id"],
                    "book_id": item["book_id"],
                    "page_id": item["page_id"],
                    "scores": item["scores"],
                }
                for item in scored
            ]
            results_for_log = [
                {
                    "chunk_id": item["chunk_id"],
                    "rank": rank,
                    "scores": item["scores"],
                }
                for rank, item in enumerate(results, start=1)
            ]
            run_parameters = {
                "filters": filters,
                "limit": int(limit),
                "ranking_version": RANKING_VERSION,
                "score_weights": SCORE_WEIGHTS,
                "query_terms": terms,
            }
            connection.execute(
                """
                INSERT INTO retrieval_runs (
                    run_id, query, filters_json, pipeline_version,
                    index_version, candidates_json, results_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    normalized_query,
                    json.dumps(run_parameters, ensure_ascii=False, sort_keys=True),
                    metadata["pipeline_version"],
                    metadata["index_version"],
                    json.dumps(candidates_for_log, ensure_ascii=False),
                    json.dumps(results_for_log, ensure_ascii=False),
                    created_at,
                ),
            )
            connection.commit()
        return response
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"SQLite 检索失败：{exc}") from exc
    finally:
        connection.close()


def render_search_markdown(response: Mapping[str, Any]) -> str:
    lines = [
        f"# 检索：{response['query']}",
        "",
        str(response["coverage_notice"]),
        "",
    ]
    results = response["results"]
    if not results:
        lines.append("未检索到匹配的本地语料。")
        return "\n".join(lines) + "\n"
    for rank, item in enumerate(results, start=1):
        display_title = (
            f"{item['title']} {item['subtitle']}"
            if item.get("subtitle")
            else item["title"]
        )
        status = item["printed_page_status"]
        if status == "verified" and item.get("printed_page"):
            location = (
                f"印刷页 {item['printed_page']}（已核验）；PDF 页 {item['pdf_page']}"
            )
        elif status == "detected" and item.get("printed_page"):
            location = (
                f"PDF 页 {item['pdf_page']}；印刷页候选 {item['printed_page']}"
                "（未核验）"
            )
        else:
            location = f"PDF 页 {item['pdf_page']}；印刷页待校准"
        chapter = " > ".join(item["chapter_path"]) or "未映射章节"
        text_source = str(item.get("text_source") or "embedded_text")
        verification = str(
            item.get("text_verification_status") or "embedded_text"
        )
        if text_source == "ocr" and verification == "ocr_unverified":
            source_notice = "OCR 转写，尚未人工对照 PDF 页面"
        elif text_source == "ocr" and verification == "ocr_verified":
            source_notice = "OCR 转写，已人工对照 PDF 页面"
        elif text_source == "hybrid":
            source_notice = f"混合文本；核验状态：{verification}"
        elif text_source == "empty":
            source_notice = "页面无可检索文本"
        else:
            source_notice = "PDF 原生文字层"
        scores = item["scores"]
        lines.extend(
            [
                f"## {rank}. 《{display_title}》检索片段",
                "",
                f"- 定位：{location}",
                f"- 章节：{chapter}",
                f"- 来源：{item.get('source_type') or '未知'}；法域："
                f"{', '.join(item.get('jurisdictions') or []) or '未标注'}",
                f"- 文本来源：{source_notice}",
                f"- 得分：final={scores['final']:.6f}；fts={scores['fts']:.6f}；"
                f"exact={scores['exact']:.6f}；chapter={scores['chapter']:.6f}；"
                f"metadata={scores['metadata']:.6f}",
                f"- 打开：{item['obsidian_pdf_link']}",
                "",
                f"> 检索片段（不是精确引文）：{item['retrieval_snippet']}",
                "",
            ]
        )
    return "\n".join(lines)

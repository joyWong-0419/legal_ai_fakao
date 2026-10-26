from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .domain import LegalAIError
from .page_map import load_page_maps, page_map_by_book, resolve_page
from .search import MAX_RESULT_LIMIT, search_index


EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_VERSION = "evidence-v2"
MAX_EVIDENCE_LIMIT = 20
MAX_SENTENCES_PER_QUOTE = 2
MAX_QUOTE_CHARS = 400
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$")


def _quote_sha256(quote: str) -> str:
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def _window_score(rows: Sequence[sqlite3.Row], terms: Sequence[str]) -> float:
    text = "\n".join(str(row["normalized_text"]) for row in rows).casefold()
    if not terms:
        return 0.0
    contributions: list[float] = []
    for term in terms:
        count = text.count(term.casefold())
        if count <= 0:
            contributions.append(0.0)
        else:
            contributions.append(0.75 + 0.25 * min(1.0, count / 3.0))
    return sum(contributions) / len(contributions)


def _select_sentence_window(
    rows: Sequence[sqlite3.Row], terms: Sequence[str]
) -> list[sqlite3.Row]:
    best_rows: list[sqlite3.Row] = []
    best_key: tuple[float, int, int] = (0.0, 0, 0)
    for start in range(len(rows)):
        for size in range(1, MAX_SENTENCES_PER_QUOTE + 1):
            window = list(rows[start : start + size])
            if len(window) != size:
                continue
            score = _window_score(window, terms)
            if score <= 0:
                continue
            char_count = len(
                "\n".join(str(row["normalized_text"]) for row in window)
            )
            if char_count > MAX_QUOTE_CHARS:
                continue
            key = (score, -char_count, -start)
            if key > best_key:
                best_rows = window
                best_key = key
    return best_rows


def _load_chunk_sentences(
    connection: sqlite3.Connection, chunk_id: str
) -> list[sqlite3.Row]:
    row = connection.execute(
        "SELECT sentence_ids_json FROM chunks WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    if row is None:
        raise LegalAIError(f"检索结果引用不存在的 chunk：{chunk_id}")
    try:
        sentence_ids = json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"{chunk_id}：sentence_ids_json 损坏") from exc
    if not isinstance(sentence_ids, list) or not sentence_ids:
        raise LegalAIError(f"{chunk_id}：sentence_ids_json 必须是非空数组")
    placeholders = ",".join("?" for _ in sentence_ids)
    rows = connection.execute(
        f"""
        SELECT sentence_id, page_id, seq, raw_text, normalized_text,
               start_offset, end_offset, text_sha256
        FROM sentences
        WHERE sentence_id IN ({placeholders})
        ORDER BY seq
        """,
        sentence_ids,
    ).fetchall()
    by_id = {str(item["sentence_id"]): item for item in rows}
    if set(by_id) != set(sentence_ids):
        raise LegalAIError(f"{chunk_id}：部分句子记录不存在")
    return [by_id[item] for item in sentence_ids]


def _load_chunk_context(
    connection: sqlite3.Connection, chunk_id: str
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT c.chunk_id, c.page_id, p.book_id, p.pdf_page,
               p.printed_page, p.printed_page_status,
               p.chapter_path_json, p.quality_flags_json,
               p.text_source, p.text_verification_status, p.ocr_status,
               p.ocr_avg_confidence, p.ocr_text_sha256,
               b.title, b.subtitle, b.file_path, b.metadata_json
        FROM chunks c
        JOIN pages p ON p.page_id = c.page_id
        JOIN books b ON b.book_id = p.book_id
        WHERE c.chunk_id = ?
        """,
        (chunk_id,),
    ).fetchone()
    if row is None:
        raise LegalAIError(f"证据引用不存在的 chunk：{chunk_id}")
    try:
        chapter_path = json.loads(row[6])
        quality_flags = json.loads(row[7])
        metadata = json.loads(row[16])
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"{chunk_id}：页面或书目 JSON 损坏") from exc
    if (
        not isinstance(chapter_path, list)
        or not isinstance(quality_flags, list)
        or not isinstance(metadata, dict)
    ):
        raise LegalAIError(f"{chunk_id}：页面或书目 JSON 结构无效")
    return {
        "chunk_id": str(row[0]),
        "page_id": str(row[1]),
        "book_id": str(row[2]),
        "pdf_page": int(row[3]),
        "printed_page": row[4],
        "printed_page_status": str(row[5]),
        "chapter_path": chapter_path,
        "quality_flags": quality_flags,
        "text_source": str(row[8]),
        "text_verification_status": str(row[9]),
        "ocr_status": str(row[10]),
        "ocr_avg_confidence": row[11],
        "ocr_text_sha256": row[12],
        "title": str(row[13]),
        "subtitle": row[14],
        "file_path": str(row[15]),
        "metadata": metadata,
    }


def _load_current_page_maps(
    connection: sqlite3.Connection, project_root: Path
) -> dict[str, Any]:
    page_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT book_id, pdf_page_count FROM books ORDER BY book_id"
        ).fetchall()
    }
    mappings = load_page_maps(
        project_root / "config" / "page_maps.json",
        page_counts,
        allow_unconfigured_books=True,
    )
    return page_map_by_book(mappings)


def _require_current_page_resolution(
    context: Mapping[str, Any], current_page_maps: Mapping[str, Any]
) -> None:
    resolution = resolve_page(
        current_page_maps.get(str(context["book_id"])),
        int(context["pdf_page"]),
    )
    indexed_page = context.get("printed_page")
    indexed_status = str(context.get("printed_page_status"))
    valid_value_pair = (
        indexed_status in {"detected", "verified"}
        and indexed_page is not None
    ) or (
        indexed_status in {"unknown", "rejected"}
        and indexed_page is None
    )
    if not valid_value_pair:
        raise LegalAIError(
            f"{context['page_id']}：索引中的印刷页值与四态状态不相容；"
            "请重新运行 process --all 和 build-index --rebuild"
        )
    if (
        indexed_status != resolution.status
        or indexed_page != resolution.printed_page
    ):
        raise LegalAIError(
            f"{context['page_id']}：SQLite 页码状态与当前 page_maps.json 不一致；"
            "为避免伪核验，已拒绝生成证据。请重新运行 process --all 和 "
            "build-index --rebuild"
        )


def _coverage_gaps(
    evidence: Sequence[Mapping[str, Any]],
    skipped_no_direct_match: int,
    skipped_overlong: int,
    skipped_quality: int,
) -> list[str]:
    gaps = ["当前语料均为学理来源，尚未核验现行法条和官方解释。"]
    if not evidence:
        gaps.append("当前查询未找到可由句子记录直接支持的证据。")
    if skipped_no_direct_match:
        gaps.append(
            f"有 {skipped_no_direct_match} 个检索块只在章节或元数据层命中，"
            "未生成直接引文。"
        )
    if skipped_overlong:
        gaps.append(
            f"有 {skipped_overlong} 个命中只落在超过 {MAX_QUOTE_CHARS} 字的"
            "长句中，为避免截断冒充完整引文而未输出。"
        )
    if skipped_quality:
        gaps.append(
            f"有 {skipped_quality} 个命中位于带质量标记的页面，"
            "未作为直接引文输出。"
        )
    partial_matches = sum(
        1 for item in evidence if item.get("unmatched_query_terms")
    )
    if partial_matches:
        gaps.append(
            f"有 {partial_matches} 条证据只直接命中查询中的部分术语；"
            "未命中的术语已在各条证据中列明。"
        )
    statuses = {str(item["printed_page_status"]) for item in evidence}
    if "detected" in statuses:
        gaps.append("部分印刷页仅为 detected 候选，尚未经用户核验。")
    if statuses & {"unknown", "rejected"}:
        gaps.append("部分证据没有可确认的印刷页，只能以 PDF 页定位。")
    if any(
        item.get("text_verification_status") == "ocr_unverified"
        for item in evidence
    ):
        gaps.append(
            "部分文本来自 OCR 转写，尚未人工对照 PDF 页面；引用前请核对原页。"
        )
    return gaps


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
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
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _verify_before_persist(
    database_path: Path, package: Mapping[str, Any]
) -> None:
    # Local import avoids an import cycle: verify uses the evidence contract
    # constants, while context needs the same verifier as the standalone CLI.
    from .verify import verify_evidence_package

    try:
        connection = sqlite3.connect(
            database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.execute("PRAGMA query_only = ON")
        issues = verify_evidence_package(connection, package)
    except sqlite3.DatabaseError as exc:
        raise LegalAIError("证据包落盘前的只读一致性校验失败") from exc
    finally:
        if "connection" in locals():
            connection.close()
    if issues:
        codes = sorted({str(issue.get("code")) for issue in issues})
        raise LegalAIError(
            "证据包落盘前自检未通过：" + ", ".join(codes[:8])
        )


def build_evidence_package(
    *,
    database_path: Path,
    project_root: Path,
    query: str,
    limit: int = 8,
    book_ids: Iterable[str] | None = None,
    jurisdictions: Iterable[str] | None = None,
    subjects: Iterable[str] | None = None,
    persist: bool = True,
    terms_override: Iterable[str] | None = None,
) -> dict[str, Any]:
    if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_EVIDENCE_LIMIT:
        raise LegalAIError(f"context limit 必须在 1-{MAX_EVIDENCE_LIMIT} 之间")
    search_limit = min(MAX_RESULT_LIMIT, max(10, int(limit) * 3))
    retrieval = search_index(
        database_path=database_path,
        query=query,
        limit=search_limit,
        book_ids=book_ids,
        jurisdictions=jurisdictions,
        subjects=subjects,
        record_run=True,
        terms_override=terms_override,
    )
    try:
        connection = sqlite3.connect(
            database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise LegalAIError(f"无法只读打开证据索引：{database_path}") from exc
    connection.row_factory = sqlite3.Row
    evidence: list[dict[str, Any]] = []
    seen_sentence_windows: set[tuple[str, ...]] = set()
    skipped_no_direct_match = 0
    skipped_overlong = 0
    skipped_quality = 0
    try:
        current_page_maps = _load_current_page_maps(connection, project_root)
        for result in retrieval["results"]:
            if len(evidence) >= int(limit):
                break
            chunk_id = str(result["chunk_id"])
            context = _load_chunk_context(connection, chunk_id)
            _require_current_page_resolution(context, current_page_maps)
            # `empty` and similar pypdf flags describe the preserved embedded
            # layer.  When canonical text is OCR, those flags must remain
            # visible without suppressing the OCR evidence itself.
            if context["quality_flags"] and context["text_source"] != "ocr":
                skipped_quality += 1
                continue
            chunk_sentences = _load_chunk_sentences(connection, chunk_id)
            if any(
                str(row["page_id"]) != context["page_id"]
                for row in chunk_sentences
            ):
                raise LegalAIError(f"{chunk_id}：句子与 chunk 不在同一页面")
            has_direct_match = any(
                term.casefold() in str(row["normalized_text"]).casefold()
                for row in chunk_sentences
                for term in retrieval["query_terms"]
            )
            if not has_direct_match:
                skipped_no_direct_match += 1
                continue
            selected = _select_sentence_window(
                chunk_sentences, retrieval["query_terms"]
            )
            if not selected:
                skipped_overlong += 1
                continue
            sentence_ids = [str(row["sentence_id"]) for row in selected]
            sentence_window = tuple(sentence_ids)
            if sentence_window in seen_sentence_windows:
                continue
            quote = "\n".join(str(row["normalized_text"]) for row in selected)
            matched_terms = [
                term
                for term in retrieval["query_terms"]
                if term.casefold() in quote.casefold()
            ]
            unmatched_terms = [
                term
                for term in retrieval["query_terms"]
                if term not in matched_terms
            ]
            metadata = context["metadata"]
            if context["text_source"] == "ocr":
                inference_level = (
                    "ocr_verified_quote"
                    if context["text_verification_status"] == "ocr_verified"
                    else "ocr_unverified_quote"
                )
            else:
                inference_level = "direct_quote"
            evidence_index = len(evidence) + 1
            evidence.append(
                {
                    "evidence_id": (
                        f"{retrieval['run_id']}:e{evidence_index:03d}"
                    ),
                    "chunk_id": context["chunk_id"],
                    "book_id": context["book_id"],
                    "title": context["title"],
                    "subtitle": context["subtitle"],
                    "authors": metadata.get("authors", []),
                    "translators": metadata.get("translators", []),
                    "edition": metadata.get("edition"),
                    "volume": metadata.get("volume"),
                    "source_type": metadata.get("source_type"),
                    "authority_tier": metadata.get("authority_tier"),
                    "jurisdictions": metadata.get("jurisdictions", []),
                    "subjects": metadata.get("subjects", []),
                    "chapter_path": context["chapter_path"],
                    "quality_flags": context["quality_flags"],
                    "page_id": context["page_id"],
                    "pdf_page": context["pdf_page"],
                    "printed_page": context["printed_page"],
                    "printed_page_status": context["printed_page_status"],
                    "text_source": context["text_source"],
                    "text_verification_status": context[
                        "text_verification_status"
                    ],
                    "ocr_status": context["ocr_status"],
                    "ocr_avg_confidence": context["ocr_avg_confidence"],
                    "ocr_text_sha256": context["ocr_text_sha256"],
                    "sentence_ids": sentence_ids,
                    "quote": quote,
                    "quote_source_field": "sentences.normalized_text",
                    "quote_sha256": _quote_sha256(quote),
                    "quote_char_count": len(quote),
                    "matched_query_terms": matched_terms,
                    "unmatched_query_terms": unmatched_terms,
                    "selection_basis": "lexical_match_in_sentence_record",
                    "retrieval_score": result["scores"]["final"],
                    "retrieval_scores": result["scores"],
                    "inference_level": inference_level,
                    "file_path": context["file_path"],
                    "obsidian_pdf_link": (
                        f"[[{context['file_path']}#page={context['pdf_page']}\\|"
                        f"打开 PDF 第 {context['pdf_page']} 页]]"
                    ),
                }
            )
            seen_sentence_windows.add(sentence_window)
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"读取证据索引失败：{exc}") from exc
    finally:
        connection.close()

    if not RUN_ID_PATTERN.fullmatch(str(retrieval["run_id"])):
        raise LegalAIError("检索 run_id 含有不安全字符，拒绝生成证据包路径")
    relative_path = (
        Path("data") / "evidence" / f"{retrieval['run_id']}.json"
    )
    package = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_version": EVIDENCE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": retrieval["query"],
        "query_terms": retrieval["query_terms"],
        "run_id": retrieval["run_id"],
        "filters": retrieval["filters"],
        "corpus_scope": retrieval["corpus_scope"],
        "coverage_notice": retrieval["coverage_notice"],
        "evidence_count": len(evidence),
        "evidence": evidence,
        "gaps": _coverage_gaps(
            evidence,
            skipped_no_direct_match,
            skipped_overlong,
            skipped_quality,
        ),
        "persisted": bool(persist),
        "saved_package": (
            str(Path("legal_ai") / relative_path).replace("\\", "/")
            if persist
            else None
        ),
    }
    if persist:
        _verify_before_persist(database_path, package)
        _atomic_write_json(project_root / relative_path, package)
    return package


def render_evidence_markdown(package: Mapping[str, Any]) -> str:
    lines = [
        f"# 证据包：{package['query']}",
        "",
        str(package["coverage_notice"]),
        "",
        "## 证据",
        "",
    ]
    evidence = package["evidence"]
    if not evidence:
        lines.extend(["未找到可由句子记录直接支持的证据。", ""])
    for index, item in enumerate(evidence, start=1):
        display_title = (
            f"{item['title']} {item['subtitle']}"
            if item.get("subtitle")
            else item["title"]
        )
        authors = "、".join(item.get("authors") or []) or "作者未标注"
        source_version = "；".join(
            str(value)
            for value in (item.get("edition"), item.get("volume"))
            if value
        ) or "版本未标注"
        status = item["printed_page_status"]
        if status == "verified" and item.get("printed_page"):
            location = (
                f"印刷页 {item['printed_page']}（已核验）；"
                f"PDF 页 {item['pdf_page']}"
            )
        elif status == "detected" and item.get("printed_page"):
            location = (
                f"PDF 页 {item['pdf_page']}；印刷页候选 "
                f"{item['printed_page']}（未核验）"
            )
        elif status == "rejected":
            location = (
                f"PDF 页 {item['pdf_page']}；候选印刷页映射已否决，"
                "不得作为印刷页引用"
            )
        else:
            location = f"PDF 页 {item['pdf_page']}；印刷页未知"
        chapter = " > ".join(item["chapter_path"]) or "未映射章节"
        if item.get("text_verification_status") == "ocr_unverified":
            text_source_notice = (
                "OCR 转写，尚未人工对照 PDF 页面；引用前请核对原页。"
            )
        elif item.get("text_verification_status") == "ocr_verified":
            text_source_notice = "OCR 转写，已人工对照 PDF 页面。"
        else:
            text_source_notice = "PDF 原生文字层。"
        sentence_range = (
            item["sentence_ids"][0]
            if len(item["sentence_ids"]) == 1
            else f"{item['sentence_ids'][0]} - {item['sentence_ids'][-1]}"
        )
        lines.extend(
            [
                f"### {index}. 《{display_title}》",
                "",
                *[
                    f"> {line}" for line in str(item["quote"]).splitlines()
                ],
                "",
                f"- 来源：{authors}；{source_version}；{item.get('source_type') or '来源类型未知'}",
                f"- 法域：{', '.join(item.get('jurisdictions') or []) or '未标注'}",
                (
                    "- 查询命中："
                    f"{', '.join(item.get('matched_query_terms') or []) or '无'}"
                    + (
                        "；未直接命中："
                        f"{', '.join(item.get('unmatched_query_terms') or [])}"
                        if item.get("unmatched_query_terms")
                        else ""
                    )
                ),
                f"- 定位：{location}；句子 {sentence_range}",
                f"- 章节：{chapter}",
                f"- 文本来源：{text_source_notice}",
                f"- 引用哈希：`{item['quote_sha256']}`",
                f"- 打开原书：{item['obsidian_pdf_link']}",
                "",
            ]
        )
    lines.extend(["## 覆盖缺口", ""])
    for gap in package["gaps"]:
        lines.append(f"- {gap}")
    if package.get("saved_package"):
        lines.extend(["", f"本地包：`{package['saved_package']}`", ""])
    return "\n".join(lines)

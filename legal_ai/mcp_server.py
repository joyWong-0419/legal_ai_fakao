"""legal_ai 的 MCP 服务器（stdio）。

四个只读工具，把「可溯源材料」直接交给模型：

- ``evidence_search``  学理书证（句子级短引文 + 成品引用串）
- ``books_catalog``    书架目录，先看目录再选书
- ``value_locate``     价值层材料的兼容别名（等价于 ``layer="value"``）
- ``law_article``      法条（条号 + 法律级版本标签）

分工约定：
1. **检索范围由模型决定**：传 ``subjects`` / ``book_ids`` / ``layer``；
   不传就不限定，由排序决定。词典只在 ``scope="auto"`` 时兜底。
2. 只读语料与法条库；唯一的写操作是服务端 receipt（``data/receipts/``）。
3. 下发给模型的内容只有 ``quote / citation / source / ref``，
   ``run_id / sentence_id / chunk_id / sha256`` 一律不出现在返回值里。
4. 状态三态：``ok`` / ``no_hit``（书在库里但没命中）/ ``not_covered``（语料缺口）。
   后两者语义不同，不得混用。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .citation import (
    book_citation,
    build_receipt,
    law_citation,
    public_item,
    write_receipt,
)
from .evidence import build_evidence_package
from .legal_source import LegalAIError, query_article
from .query_terms import split_terms
from .routing import classify_status, load_layer_map, resolve_routing, subjects_for_layer
from .scope import resolve_scope

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = REPO_ROOT / "legal_ai"
CORPUS_DB = PROJECT_ROOT / "data" / "index" / "legal_corpus.sqlite"
LAW_DB = PROJECT_ROOT / "data" / "index" / "legal_sources.sqlite"
RECEIPT_DIR = PROJECT_ROOT / "data" / "receipts"

# read_context 的边界：一次最多两页、六千字，避免把上下文撑爆
MAX_READ_PAGES = 2
MAX_READ_CHARS = 6000

INSTRUCTIONS = """\
legal_ai 是本地、离线、可溯源的法律语料检索服务。

三类材料，用法不同：
- evidence_search：碎片（1–2 句），**引用**只认它的 quote，逐字、带页。
- read_context：连续 1–2 页正文，用于**理解**论证；quotable=false，不可直接引用。
- law_article：法条，条号只能来自它的返回，不要凭记忆写。

写作规则：
- 引文：逐字 + 链接。
- 不要为了整齐而把推论写成引文.

状态语义：
- status="no_hit"：书在库里但这次没命中，可换词重试。
- status="not_covered"：语料里根本没有，才写「当前语料未覆盖」。
"""

mcp = MCPServer("legal-ai", instructions=INSTRUCTIONS)


def _display_edition(item: dict[str, Any]) -> str | None:
    subtitle = item.get("subtitle")
    if subtitle:
        return str(subtitle)
    edition = item.get("edition")
    return str(edition) if edition else None


def _book_items(
    package: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, evidence in enumerate(package.get("evidence", ()), start=1):
        citation = book_citation(
            file_path=str(evidence["file_path"]),
            title=str(evidence["title"]),
            edition=_display_edition(evidence),
            pdf_page=int(evidence["pdf_page"]),
            printed_page=evidence.get("printed_page"),
            printed_page_status=str(evidence.get("printed_page_status") or "unknown"),
        )
        authors = evidence.get("authors") or []
        source = {
            "kind": "book",
            "book_id": evidence.get("book_id"),
            "title": evidence["title"],
            "author": authors[0] if authors else None,
        }
        evidence["citation"] = citation
        evidence["source"] = source
        items.append(public_item(evidence, ref=f"ev_{index:03d}"))
    return items


def _search_books(
    *,
    query: str,
    limit: int,
    book_ids: tuple[str, ...] | None,
    subjects: tuple[str, ...] | None,
    tool: str,
    terms: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    try:
        package = build_evidence_package(
            database_path=CORPUS_DB,
            project_root=PROJECT_ROOT,
            query=query,
            limit=limit,
            book_ids=book_ids,
            subjects=subjects,
            persist=False,
            terms_override=terms,
        )
    except LegalAIError as exc:
        return {"status": "error", "message": str(exc)}

    items = _book_items(package)
    corpus_scope = [str(item) for item in package.get("corpus_scope") or ()]
    if not items:
        return {
            "status": "not_covered",
            "items": [],
            "corpus_scope": corpus_scope,
            "coverage_note": package.get("coverage_notice"),
            "gaps": package.get("gaps", []),
        }

    receipt = build_receipt(
        tool=tool,
        query=query,
        run_id=str(package["run_id"]),
        items=package.get("evidence", ()),
    )
    receipt_path = write_receipt(RECEIPT_DIR, receipt)

    return {
        "status": "ok",
        "items": items,
        "corpus_scope": corpus_scope,
        "retrieval": {"evidence_count": len(items)},
        "coverage_note": package.get("coverage_notice"),
        "gaps": package.get("gaps", []),
        "receipt": receipt_path.name,
    }


def _book_ids_of(result: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item.get("source", {}).get("book_id"))
            for item in result.get("items", ())
            if item.get("source", {}).get("book_id")
        }
    )


@mcp.tool()
def evidence_search(
    query: str,
    limit: int = 6,
    terms: list[str] | None = None,
    book_ids: list[str] | None = None,
    subjects: list[str] | None = None,
    layer: str | None = None,
    scope: str | None = None,
    control: bool = True,
) -> dict[str, Any]:
    """检索学理书籍，返回句子级短引文与可直接粘贴的引用串。

    **检索范围由你决定**：先判断问题属于哪个部门法或哪一层，再显式传
    ``subjects`` / ``book_ids`` / ``layer``。不传就不限定范围，由排序决定。
    只有 ``scope="auto"`` 才启用服务端的词典兜底。

    Args:
        query: 原始问题（用于日志与对照展示）。
        limit: 返回条数，1-10。
        terms: 检索词；不传则服务端切分（词典种子 → 零命中再补二元组）。
        book_ids: 指定书目，优先级最高。
        subjects: 学科过滤（民法 / 刑法 / 法哲学 / 民诉法 …）。
        layer: doctrine / value / genealogy / norm，按层映射到学科。
        scope: "auto" 走词典兜底；传部门法名强制该域；"all" 或省略表示不限定。
        control: 用了过滤时，是否额外跑一次无过滤对照（limit=3）。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}

    text = query.strip()
    auto_subjects: tuple[str, ...] = ()
    if scope and scope.strip().lower() == "auto":
        try:
            auto_subjects = resolve_scope(text).subjects
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
    elif scope and scope.strip() and scope.strip().lower() != "all":
        try:
            auto_subjects = resolve_scope(text, override=scope.strip()).subjects
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    try:
        routing = resolve_routing(
            book_ids=book_ids,
            subjects=subjects,
            layer=layer,
            auto_subjects=auto_subjects,
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    explicit_terms = tuple(
        dict.fromkeys(str(item).strip() for item in (terms or []) if str(item).strip())
    )
    seed_terms = explicit_terms or tuple(split_terms(text, include_bigrams=False))
    page_limit = max(1, min(10, int(limit)))

    result = _search_books(
        query=text,
        limit=page_limit,
        book_ids=routing.book_ids or None,
        subjects=routing.subjects or None,
        tool="evidence.search",
        terms=seed_terms,
    )

    # 第一段零命中：补二元组再试一次（调用方显式给词时不干预）
    if (
        not explicit_terms
        and result.get("status") != "ok"
        and len(text) >= 4
    ):
        widened_terms = tuple(split_terms(text, include_bigrams=True))
        if widened_terms and widened_terms != seed_terms:
            retry = _search_books(
                query=text,
                limit=page_limit,
                book_ids=routing.book_ids or None,
                subjects=routing.subjects or None,
                tool="evidence.search",
                terms=widened_terms,
            )
            result = retry
            if retry.get("status") == "ok":
                result["terms_expanded"] = True
                seed_terms = widened_terms

    if result.get("status") == "error":
        return result

    status = classify_status(
        len(result.get("items", ())), result.get("corpus_scope") or ()
    )
    result["status"] = status
    result["routing"] = routing.to_dict()
    result["terms_used"] = list(seed_terms)

    if control and (routing.book_ids or routing.subjects):
        unfiltered = _search_books(
            query=text,
            limit=3,
            book_ids=None,
            subjects=None,
            tool="evidence.search.control",
            terms=tuple(seed_terms),
        )
        control_books = _book_ids_of(unfiltered)
        main_books = _book_ids_of(result)
        result["routing"]["control"] = {
            "ran": True,
            "books": control_books,
            "new_books": [book for book in control_books if book not in main_books],
        }

    if status == "not_covered":
        result["gap"] = (
            "过滤后的书库里没有可检索的书——这是语料缺口，不是检索失败。"
        )
    elif status == "no_hit":
        result["gap"] = (
            "书在库里但本次检索未命中：可能检索词不对，也可能语料确实没有这段论述。"
        )
    return result


@mcp.tool()
def books_catalog() -> dict[str, Any]:
    """列出书架：书目、版本、学科、页数、是否来自 OCR。

    先看目录再决定 ``evidence_search`` 的 ``book_ids`` / ``subjects``，
    不要靠检索结果反推书架。
    """
    connection = sqlite3.connect(CORPUS_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT book_id, title, edition, volume, pdf_page_count, metadata_json, "
            "(SELECT COUNT(*) FROM pages p WHERE p.book_id = b.book_id) AS pages "
            "FROM books b ORDER BY book_id"
        ).fetchall()
    finally:
        connection.close()

    books = []
    for book_id, title, edition, volume, pdf_pages, metadata_json, pages in rows:
        metadata = json.loads(metadata_json or "{}")
        books.append(
            {
                "book_id": str(book_id),
                "title": str(title),
                "edition": edition,
                "volume": volume,
                "subjects": list(metadata.get("subjects") or []),
                "pages": int(pages),
                "pdf_pages": int(pdf_pages or 0),
                "ocr": str(metadata.get("ocr_policy") or "off") != "off",
            }
        )
    return {"status": "ok", "book_count": len(books), "books": books}


@mcp.tool()
def read_context(
    book_id: str,
    pdf_page_start: int,
    pdf_page_end: int | None = None,
) -> dict[str, Any]:
    """读取连续 1–2 页正文，用于**理解论证**，不可直接当引文。

    ``evidence_search`` 给的是碎片（1–2 句），只够引用、不够理解。
    这个工具给你一段连续的原文，让你看清作者在反驳谁、论证怎么走。
    返回值里 ``quotable`` 恒为 false：要写进笔记的引文，仍然只能来自
    ``evidence_search`` 的 ``quote``。

    Args:
        book_id: 书目 ID，可从 books_catalog 或 citation 的链接里看到。
        pdf_page_start: 起始 PDF 页（citation 里的 #page=N）。
        pdf_page_end: 结束页；省略时只读一页，最多跨 2 页。
    """
    start = int(pdf_page_start)
    end = int(pdf_page_end) if pdf_page_end is not None else start
    if start < 1 or end < start:
        return {"status": "error", "message": "页码范围不合法"}
    if end - start + 1 > MAX_READ_PAGES:
        return {
            "status": "error",
            "message": f"一次最多读 {MAX_READ_PAGES} 页；请缩小范围分次读。",
        }

    connection = sqlite3.connect(CORPUS_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT p.page_id, p.pdf_page, p.printed_page, p.printed_page_status, "
            "p.chapter_path_json, p.normalized_text, b.title, b.edition "
            "FROM pages p JOIN books b ON b.book_id = p.book_id "
            "WHERE p.book_id = ? AND p.pdf_page BETWEEN ? AND ? "
            "ORDER BY p.pdf_page",
            (book_id.strip(), start, end),
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        return {
            "status": "not_covered",
            "gap": f"{book_id} 没有第 {start}-{end} 页；先用 books_catalog 确认书目与页数。",
        }

    blocks: list[str] = []
    pages: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for (
        page_id,
        pdf_page,
        printed_page,
        printed_status,
        chapter_json,
        text,
        title,
        edition,
    ) in rows:
        body = str(text or "").strip()
        pages.append(
            {
                "page_id": str(page_id),
                "pdf_page": int(pdf_page),
                "printed_page": printed_page,
                "printed_page_status": printed_status,
                "chapter_path": json.loads(chapter_json or "[]"),
                "chars": len(body),
            }
        )
        if not body:
            blocks.append(f"—— PDF 第 {pdf_page} 页（无文字：空白页或抽取失败）")
            continue
        if total + len(body) > MAX_READ_CHARS:
            body = body[: max(0, MAX_READ_CHARS - total)]
            truncated = True
        total += len(body)
        blocks.append(f"—— PDF 第 {pdf_page} 页 ——\n{body}")
        if truncated:
            break

    edition_text = f"（{edition}）" if edition else ""
    return {
        "status": "ok",
        "book_id": book_id.strip(),
        "title": str(rows[0][6]),
        "edition": edition_text,
        "pages": pages,
        "text": "\n\n".join(blocks),
        "quotable": False,
        "truncated": truncated,
        "note": (
            "这段正文用于理解论证，不能直接引用；"
            "要写进笔记的引文，请用 evidence_search 返回的 quote 与 citation。"
        ),
        "quote_hint": "（《" + str(rows[0][6]) + "》p.N）",
    }


@mcp.tool()
def value_locate(
    topic: str,
    limit: int = 5,
    source: str = "book",
) -> dict[str, Any]:
    """价值层材料的兼容别名，等价于 ``evidence_search(layer="value")``。

    Args:
        topic: 价值主题（例：正当性 服从 规则）。
        limit: 返回条数，1-10。
        source: book（默认）| classic | all；classic 在古籍入库后可用。
    """
    if not topic.strip():
        return {"status": "error", "message": "topic 不能为空"}
    if source not in {"book", "classic", "all"}:
        return {"status": "error", "message": "source 只能是 book / classic / all"}
    if source == "classic":
        return {
            "status": "not_covered",
            "gap": "古籍尚未入库；当前价值层仅覆盖法哲学书籍。",
        }
    result = evidence_search(
        topic.strip(), limit=limit, layer="value", control=False
    )
    if result.get("status") == "ok" and source == "all":
        result["gap_classic"] = "古籍尚未入库，本次未检索古籍。"
    return result


def _resolve_law(law: str) -> tuple[str, str, str] | None:
    """把「刑法 / 中华人民共和国民法典 / criminal-law-cn」解析为配置行。"""
    text = law.strip()
    if not text:
        return None
    connection = sqlite3.connect(LAW_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT law_id, title, COALESCE(short_title, '') FROM legal_sources"
        ).fetchall()
    finally:
        connection.close()
    for law_id, title, short_title in rows:
        if text in {law_id, title, short_title}:
            return str(law_id), str(title), str(short_title)
    for law_id, title, short_title in rows:
        if text in title or (short_title and short_title in text):
            return str(law_id), str(title), str(short_title)
    return None


def _version_label(law_id: str) -> str:
    """取该法最近一次入库的版本标签（存于 legal_source_ingestions）。"""
    connection = sqlite3.connect(LAW_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT COALESCE(current_version_label, '') "
            "FROM legal_source_ingestions WHERE law_id = ? "
            "ORDER BY ingested_at DESC, rowid DESC LIMIT 1",
            (law_id,),
        ).fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else ""


@mcp.tool()
def law_article(law: str, article: str) -> dict[str, Any]:
    """按法名与条号取条文，返回带版本标签的引用串。

    Args:
        law: 法名，如 刑法 / 民法典 / 公司法 / 民诉法，或 law_id。
        article: 条号，如 20 或 17-1（“之一”型）。
    """
    resolved = _resolve_law(law)
    if resolved is None:
        return {
            "status": "not_covered",
            "gap": f"未登记该法源：{law}；当前仅收录刑法、民法典、公司法、民诉法。",
        }
    law_id, title, _short = resolved
    version_label = _version_label(law_id)
    try:
        view = query_article(
            database_path=LAW_DB, law_id=law_id, article_no=str(article)
        )
    except LegalAIError as exc:
        return {"status": "error", "message": str(exc)}
    if view is None:
        return {
            "status": "not_covered",
            "gap": f"{title} 未找到第 {article} 条（可能条号不存在或超出文本时点）。",
        }
    citation = law_citation(
        title=view.law_title,
        article_no=view.article_no,
        version_label=version_label or None,
        as_of=view.text_as_of,
    )
    return {
        "status": "ok",
        "text": view.text,
        "citation": citation,
        "version": {"label": version_label or None, "as_of": view.text_as_of},
        "chapter_path": list(view.chapter_path),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

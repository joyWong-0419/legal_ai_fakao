from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import LegalAIError


ACCEPTANCE_SCHEMA_VERSION = 1
MAX_ACCEPTANCE_QUOTE_CHARS = 400
PREFERRED_QUOTE_MIN_CHARS = 20
PREFERRED_QUOTE_MAX_CHARS = 220
PRINTED_PAGE_STATUSES = {"unknown", "detected", "verified", "rejected"}

_CONTENT_CHARACTER = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]")
_CHAPTER_HEADING_PREFIX = re.compile(
    r"^\s*第[0-9一二三四五六七八九十百千万零〇两]+[编篇章节部]"
)

__all__ = [
    "build_citation_acceptance",
    "render_citation_acceptance_markdown",
    "write_citation_acceptance_markdown",
]


def _load_audit(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LegalAIError(f"无法读取页码审计报告：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"页码审计报告不是合法 JSON：{path}") from exc
    if not isinstance(loaded, dict):
        raise LegalAIError(f"页码审计报告根节点必须是对象：{path}")
    return loaded


def _validate_audit(audit: Mapping[str, Any]) -> tuple[int, int, list[Any]]:
    policy = audit.get("sample_policy")
    books = audit.get("books")
    if not isinstance(policy, dict) or not isinstance(books, list) or not books:
        raise LegalAIError("页码审计报告缺少 sample_policy 或 books")

    per_book = policy.get("per_book")
    seed = policy.get("seed")
    if isinstance(per_book, bool) or per_book not in (1, 2):
        raise LegalAIError("P5 人工验收必须复用每本书 1 或 2 个审计页面")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise LegalAIError("页码审计报告缺少整数随机种子")
    if policy.get("deterministic") is not True:
        raise LegalAIError("P5 人工验收只能复用确定性的页码审计样本")

    declared_count = audit.get("book_count")
    if declared_count is not None and declared_count != len(books):
        raise LegalAIError("页码审计报告的 book_count 与 books 数量不一致")
    return int(per_book), seed, books


def _is_meaningful_sentence(text: str) -> bool:
    compact = "".join(text.split())
    if not compact:
        return False
    content_count = len(_CONTENT_CHARACTER.findall(compact))
    return content_count >= 8 and content_count / len(compact) >= 0.3


def _select_sentence(rows: Sequence[sqlite3.Row], page_id: str) -> sqlite3.Row:
    eligible = []
    for row in rows:
        text = str(row["normalized_text"])
        if not text.strip() or len(text) > MAX_ACCEPTANCE_QUOTE_CHARS:
            continue
        if _is_meaningful_sentence(text):
            eligible.append(row)
    if not eligible:
        raise LegalAIError(
            f"{page_id}：没有可用于人工验收的完整短句（上限 "
            f"{MAX_ACCEPTANCE_QUOTE_CHARS} 字）"
        )

    preferred = [
        row
        for row in eligible
        if PREFERRED_QUOTE_MIN_CHARS
        <= len(str(row["normalized_text"]))
        <= PREFERRED_QUOTE_MAX_CHARS
    ]
    pool = preferred or eligible
    return min(
        pool,
        key=lambda row: (
            bool(
                _CHAPTER_HEADING_PREFIX.match(
                    str(row["normalized_text"])
                )
            ),
            len(str(row["normalized_text"])),
            int(row["seq"]),
            str(row["sentence_id"]),
        ),
    )


def _printed_page_label(
    *, pdf_page: int, printed_page: Any, status: str
) -> str:
    printed = None if printed_page is None else str(printed_page).strip()
    if status == "verified" and printed:
        return f"印刷页 {printed}（已核验）；PDF 页 {pdf_page}"
    if status == "verified":
        return (
            f"PDF 页 {pdf_page}；印刷页状态异常（verified 但无页码），"
            "不得视为已核验"
        )
    if status == "detected" and printed:
        return f"PDF 页 {pdf_page}；印刷页候选 {printed}（未核验）"
    if status == "detected":
        return f"PDF 页 {pdf_page}；印刷页仅为 detected（无候选，未核验）"
    if status == "rejected":
        return f"PDF 页 {pdf_page}；候选印刷页映射已否决"
    return f"PDF 页 {pdf_page}；印刷页未知"


def _normalized_printed_page(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _validate_page_status_value(
    *, page_id: str, status: str, printed_page: str | None
) -> None:
    if status in {"detected", "verified"} and printed_page is None:
        raise LegalAIError(
            f"{page_id}：{status} 状态必须具有印刷页候选值"
        )
    if status in {"unknown", "rejected"} and printed_page is not None:
        raise LegalAIError(
            f"{page_id}：{status} 状态不得携带印刷页值"
        )


def _read_page_sample(
    connection: sqlite3.Connection,
    *,
    book_id: str,
    pdf_page: int,
    audit_sample: Mapping[str, Any],
) -> dict[str, Any]:
    page = connection.execute(
        """
        SELECT p.page_id, p.printed_page, p.printed_page_status,
               p.quality_flags_json, b.title, b.subtitle, b.file_path
        FROM pages p
        JOIN books b ON b.book_id = p.book_id
        WHERE p.book_id = ? AND p.pdf_page = ?
        """,
        (book_id, pdf_page),
    ).fetchone()
    if page is None:
        raise LegalAIError(f"审计页面不在索引中：{book_id} PDF 页 {pdf_page}")

    status = str(page["printed_page_status"])
    if status not in PRINTED_PAGE_STATUSES:
        raise LegalAIError(f"{page['page_id']}：印刷页状态无效：{status}")
    printed_page = _normalized_printed_page(page["printed_page"])
    _validate_page_status_value(
        page_id=str(page["page_id"]),
        status=status,
        printed_page=printed_page,
    )

    audit_status = audit_sample.get("status")
    audit_candidate = _normalized_printed_page(
        audit_sample.get("candidate_printed_page")
    )
    if audit_status != status or audit_candidate != printed_page:
        raise LegalAIError(
            f"{page['page_id']}：页码审计报告已过期；请重新运行 page-map audit"
        )
    try:
        quality_flags = json.loads(page["quality_flags_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise LegalAIError(f"{page['page_id']}：quality_flags_json 损坏") from exc
    if not isinstance(quality_flags, list):
        raise LegalAIError(f"{page['page_id']}：quality_flags_json 必须是数组")
    if quality_flags:
        raise LegalAIError(
            f"{page['page_id']}：页面带质量标记，不能进入人工引用验收"
        )

    sentence_rows = connection.execute(
        """
        SELECT sentence_id, seq, normalized_text
        FROM sentences
        WHERE page_id = ?
          AND length(trim(normalized_text)) > 0
          AND length(normalized_text) <= ?
        ORDER BY seq, sentence_id
        """,
        (page["page_id"], MAX_ACCEPTANCE_QUOTE_CHARS),
    ).fetchall()
    sentence = _select_sentence(sentence_rows, str(page["page_id"]))
    quote = str(sentence["normalized_text"])

    snapshot_path = audit_sample.get("snapshot_path")
    if not isinstance(snapshot_path, str) or not snapshot_path.strip():
        raise LegalAIError(
            f"{book_id} PDF 页 {pdf_page}：审计样本缺少快照路径"
        )
    normalized_snapshot_path = snapshot_path.replace("\\", "/")
    expected_snapshot_path = (
        f"legal_ai/reports/private/page_map_acceptance/{book_id}/"
        f"pdf-{pdf_page:04d}.png"
    )
    if normalized_snapshot_path != expected_snapshot_path:
        raise LegalAIError(
            f"{book_id} PDF 页 {pdf_page}：审计样本快照路径无效"
        )

    subtitle = page["subtitle"]
    file_path = str(page["file_path"])
    return {
        "book_id": book_id,
        "title": str(page["title"]),
        "subtitle": None if subtitle is None else str(subtitle),
        "display_title": (
            f"{page['title']} {subtitle}" if subtitle else str(page["title"])
        ),
        "file_path": file_path,
        "page_id": str(page["page_id"]),
        "pdf_page": pdf_page,
        "printed_page": printed_page,
        "printed_page_status": status,
        "printed_page_label": _printed_page_label(
            pdf_page=pdf_page,
            printed_page=printed_page,
            status=status,
        ),
        "quality_flags": quality_flags,
        "sentence_id": str(sentence["sentence_id"]),
        "quote": quote,
        "quote_char_count": len(quote),
        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        "quote_source_field": "sentences.normalized_text",
        "obsidian_pdf_link": (
            f"[[{file_path}#page={pdf_page}\\|打开 PDF 第 {pdf_page} 页]]"
        ),
        "snapshot_path": normalized_snapshot_path,
        "snapshot_exists_at_audit": bool(audit_sample.get("snapshot_exists")),
    }


def build_citation_acceptance(
    *, database_path: Path, audit_path: Path
) -> dict[str, Any]:
    """Build a deterministic P5 human-audit package without writing files."""

    database_path = Path(database_path)
    audit_path = Path(audit_path)
    if not database_path.is_file():
        raise LegalAIError(f"SQLite 索引不存在：{database_path}")
    audit = _load_audit(audit_path)
    per_book, seed, audit_books = _validate_audit(audit)

    uri = database_path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise LegalAIError(f"无法只读打开 SQLite 索引：{database_path}") from exc
    connection.row_factory = sqlite3.Row

    books: list[dict[str, Any]] = []
    seen_books: set[str] = set()
    try:
        connection.execute("PRAGMA query_only = ON")
        for audit_book in audit_books:
            if not isinstance(audit_book, dict):
                raise LegalAIError("页码审计报告包含无效书籍记录")
            book_id = audit_book.get("book_id")
            samples = audit_book.get("acceptance_samples")
            if not isinstance(book_id, str) or not book_id.strip():
                raise LegalAIError("页码审计报告包含无效 book_id")
            if book_id in seen_books:
                raise LegalAIError(f"页码审计报告包含重复书籍：{book_id}")
            seen_books.add(book_id)
            if not isinstance(samples, list) or len(samples) != per_book:
                raise LegalAIError(
                    f"{book_id}：必须恰好复用审计中的 {per_book} 个页面"
                )

            selected_pages: set[int] = set()
            built_samples: list[dict[str, Any]] = []
            for sample in samples:
                if not isinstance(sample, dict):
                    raise LegalAIError(f"{book_id}：acceptance_samples 记录无效")
                pdf_page = sample.get("pdf_page")
                if (
                    isinstance(pdf_page, bool)
                    or not isinstance(pdf_page, int)
                    or pdf_page <= 0
                ):
                    raise LegalAIError(f"{book_id}：验收样本 PDF 页无效")
                if pdf_page in selected_pages:
                    raise LegalAIError(
                        f"{book_id}：验收样本重复 PDF 页 {pdf_page}"
                    )
                selected_pages.add(pdf_page)
                built_samples.append(
                    _read_page_sample(
                        connection,
                        book_id=book_id,
                        pdf_page=pdf_page,
                        audit_sample=sample,
                    )
                )

            first = built_samples[0]
            books.append(
                {
                    "book_id": book_id,
                    "display_title": first["display_title"],
                    "samples": built_samples,
                }
            )
    except sqlite3.Error as exc:
        raise LegalAIError("读取 P5 人工验收数据时 SQLite 查询失败") from exc
    finally:
        connection.close()

    return {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "source": "reports/page_map_audit.json acceptance_samples",
        "sample_policy": {
            "per_book": per_book,
            "seed": seed,
            "deterministic": True,
        },
        "book_count": len(books),
        "sample_count": sum(len(book["samples"]) for book in books),
        "books": books,
    }


def _snapshot_link(sample: Mapping[str, Any]) -> str:
    path = str(sample["snapshot_path"])
    escaped = path.replace("|", "\\|")
    if sample.get("snapshot_exists_at_audit"):
        return f"[[{escaped}\\|打开渲染快照]]"
    return "审计时未生成快照，请使用上方原书链接"


def render_citation_acceptance_markdown(
    acceptance: Mapping[str, Any],
) -> str:
    """Render a private, short-quote P5 human citation acceptance report."""

    policy = acceptance["sample_policy"]
    lines = [
        "# P5 人工引用验收清单",
        "",
        "本清单复用 P3 页码审计已经随机抽出的页面，不再增加人工工作量。",
        f"每本书 {policy['per_book']} 页，共 {acceptance['sample_count']} 页；",
        f"固定随机种子 `{policy['seed']}`。引文完整复制自 SQLite 的",
        "`sentences.normalized_text`，不截断、不访问网络，也不重新读取 PDF。",
        "",
        "只有 `verified` 才表示印刷页已核验；`detected` 只是候选，",
        "`unknown` 只使用 PDF 页，`rejected` 表示候选映射已否决。",
        "",
        "## 如何核验",
        "",
        "1. 每条样本点击“打开原书”或“打开渲染快照”。",
        "2. 确认短引文能在指定 PDF 页找到，文字和标点一致。",
        "3. 确认印刷页状态没有把候选或未知页写成已核验。",
        "4. 在对应复选框打勾；任何不一致按文末模板回复。",
        "",
        "## 抽查样本",
        "",
    ]
    number = 0
    for book in acceptance["books"]:
        lines.extend(
            [
                f"### 《{book['display_title']}》 (`{book['book_id']}`)",
                "",
            ]
        )
        for sample in book["samples"]:
            number += 1
            lines.extend(
                [
                    f"#### {number}. PDF 页 {sample['pdf_page']}",
                    "",
                    *[
                        f"> {line}"
                        for line in str(sample["quote"]).splitlines()
                    ],
                    "",
                    f"- 书籍：《{sample['display_title']}》",
                    f"- 定位：{sample['printed_page_label']}",
                    f"- 句子 ID：`{sample['sentence_id']}`",
                    f"- 引文哈希：`{sample['quote_sha256']}`",
                    f"- 打开原书：{sample['obsidian_pdf_link']}",
                    f"- 渲染快照：{_snapshot_link(sample)}",
                    f"- 快照路径：`{sample['snapshot_path']}`",
                    "- [ ] 引文与该 PDF 页原文一致",
                    "- [ ] PDF 页和印刷页状态显示正确",
                    "",
                ]
            )

    lines.extend(
        [
            "## 回复模板",
            "",
            "全部一致时回复：",
            "",
            "```text",
            f"P5 人工抽查：每本书 {policy['per_book']} 页，共 "
            f"{acceptance['sample_count']} 页，短引文、PDF 定位和印刷页状态全部一致。",
            "```",
            "",
            "发现问题时回复：",
            "",
            "```text",
            "书名：",
            "PDF 页：",
            "句子 ID：",
            "问题：引文不一致 / PDF 页错误 / 印刷页状态错误",
            "实际看到的内容或页码：",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write_unique(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_citation_acceptance_markdown(
    *, database_path: Path, audit_path: Path, output_path: Path
) -> dict[str, Any]:
    """Build and atomically write the deterministic P5 Markdown checklist.

    The returned mapping is the same structured package produced by
    :func:`build_citation_acceptance`, plus the resolved ``output_path``.
    """

    acceptance = build_citation_acceptance(
        database_path=database_path, audit_path=audit_path
    )
    output_path = Path(output_path)
    markdown = render_citation_acceptance_markdown(acceptance)
    _atomic_write_unique(output_path, markdown)
    return {**acceptance, "output_path": str(output_path.resolve())}

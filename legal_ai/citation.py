"""把结构化证据变成「可直接粘贴的引用串」。

设计要点：

1. 对外只暴露 ``quote`` / ``citation`` / ``source`` / ``ref``；
   ``run_id``、``sentence_id``、``chunk_id``、``sha256`` 一律留在服务端
   receipt 里，不下发给模型（避免转录错误，也避免笔记正文变脏）。
2. 引用串必须能直接粘进 Markdown，不需要模型二次拼接。
3. 三类材料三套定位符，不混用：
   - 现代著作：``——[[books/x.pdf#page=307|《书名》版次 p.307]]``
   - 法条：``《法名》第二十条（版本标签，截至 YYYY-MM-DD）``
   - 古籍：``——《庄子·骈拇》``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

CITATION_VERSION = "citation-v1"

# 只允许出现在服务端 receipt 里的字段名（出现即视为泄漏）
INTERNAL_FIELD_NAMES = frozenset(
    {
        "run_id",
        "sentence_id",
        "sentence_ids",
        "chunk_id",
        "quote_sha256",
        "text_sha256",
        "source_sha256",
        "file_path",
        "char_offset_start",
        "char_offset_end",
    }
)

# 笔记正文里不允许出现的字符串（大小写不敏感匹配）
FORBIDDEN_IN_TEXT = ("run_id", "sentence_id", "chunk_id", "sha256")

_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ("", "十", "百", "千")


def int_to_chinese(value: int) -> str:
    """1234 → 一千二百三十四；10 → 十；101 → 一百零一。"""
    if value < 0:
        raise ValueError("条号不能为负")
    if value == 0:
        return "零"
    digits = str(value)
    out: list[str] = []
    pending_zero = False
    for index, char in enumerate(digits):
        digit = int(char)
        unit = _CN_UNITS[len(digits) - 1 - index]
        if digit == 0:
            pending_zero = bool(out)
            continue
        if pending_zero:
            out.append("零")
            pending_zero = False
        out.append(_CN_DIGITS[digit] + unit)
    text = "".join(out)
    if text.startswith("一十"):
        text = text[1:]
    return text


def format_article_label(article_no: str) -> str:
    """``20`` → ``第二十条``；``17-1`` → ``第十七条之一``。"""
    article_no = str(article_no).strip()
    if not article_no:
        raise ValueError("条号为空")
    if "-" in article_no:
        head, tail = article_no.split("-", 1)
        return f"第{int_to_chinese(int(head))}条之{int_to_chinese(int(tail))}"
    return f"第{int_to_chinese(int(article_no))}条"


def book_citation(
    *,
    file_path: str,
    title: str,
    edition: str | None,
    pdf_page: int,
    printed_page: str | None = None,
    printed_page_status: str = "unknown",
) -> str:
    """现代著作引用串。

    印刷页已核验时显示 ``p.278（PDF 307）``，否则只显示 PDF 页。
    """
    label = f"《{title}》{edition}" if edition else f"《{title}》"
    verified = printed_page_status == "verified" and printed_page
    page_text = (
        f"p.{printed_page}（PDF {pdf_page}）" if verified else f"p.{pdf_page}"
    )
    return f"——[[{file_path}#page={pdf_page}|{label}{page_text}]]"


def law_citation(
    *,
    title: str,
    article_no: str,
    version_label: str | None,
    as_of: str,
) -> str:
    """法条引用串：法名 + 条号 + 法律级版本标签。"""
    label = format_article_label(article_no)
    if version_label:
        return f"《{title}》{label}（{version_label}，截至 {as_of}）"
    return f"《{title}》{label}（截至 {as_of}）"


def classic_citation(*, book: str, chapter: str) -> str:
    """古籍引用串：书名 + 篇名，不造页码。"""
    book = book.strip().strip("《》")
    chapter = chapter.strip()
    if not book or not chapter:
        raise ValueError("古籍引用的书名与篇名都不能为空")
    return f"——《{book}·{chapter}》"


def _strip_internal(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in mapping.items()
        if key not in INTERNAL_FIELD_NAMES
    }


def public_item(payload: Mapping[str, Any], *, ref: str) -> dict[str, Any]:
    """构造下发给模型/写进笔记的条目。

    只保留 quote / citation / source / kind / retrieval / ref。
    ``ref`` 是服务端 receipt 的句柄（形如 ``ev_001``），不是数据库主键。
    """
    allowed = ("quote", "citation", "source", "kind", "retrieval")
    item = {key: payload[key] for key in allowed if key in payload}
    item["ref"] = ref
    return item


def assert_public_text(text: str) -> None:
    """笔记正文自检：出现内部字段名就报错。"""
    lowered = text.lower()
    leaked = [token for token in FORBIDDEN_IN_TEXT if token in lowered]
    if leaked:
        raise ValueError(f"笔记正文出现内部字段：{', '.join(sorted(leaked))}")


def build_receipt(
    *,
    tool: str,
    query: str,
    run_id: str,
    items: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """服务端 receipt：记录本次调用用了哪些证据（不下发给模型）。"""
    evidence = []
    for item in items:
        evidence.append(
            {
                key: item[key]
                for key in (
                    "sentence_ids",
                    "chunk_id",
                    "quote_sha256",
                    "file_path",
                )
                if key in item
            }
        )
    return {
        "schema_version": CITATION_VERSION,
        "tool": tool,
        "query": query,
        "run_id": run_id,
        "evidence": evidence,
    }


def write_receipt(directory: Path, receipt: Mapping[str, Any]) -> Path:
    """把 receipt 落到 ``data/receipts/<run_id>.json``，同名不覆盖。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = str(receipt.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("receipt 缺少 run_id")
    safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_+.")
    if not safe:
        raise ValueError("run_id 含不安全字符")
    path = directory / f"{safe}.json"
    if path.exists():
        raise FileExistsError(f"receipt 已存在，拒绝覆盖：{path}")
    path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return path

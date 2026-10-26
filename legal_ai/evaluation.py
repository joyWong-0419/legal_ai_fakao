from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import PIPELINE_VERSION
from .domain import LegalAIError
from .index import INDEX_VERSION
from .normalize import NORMALIZATION_VERSION
from .search import RANKING_VERSION, SCORE_WEIGHTS, search_index
from .segment import SEGMENTATION_VERSION


GOLDEN_SCHEMA_VERSION = "golden-v1"
EVALUATION_SCHEMA_VERSION = "retrieval-evaluation-v1"
EVALUATION_VERSION = "offline-page-eval-v1"

RETRIEVAL_LIMIT = 10
PRECISION_K = 5
RECALL_K = 10

MINIMUM_QUESTION_COUNT = 30
MINIMUM_CROSS_BOOK_QUESTION_COUNT = 5
MINIMUM_SINGLE_BOOK_QUESTIONS_PER_BOOK = 3
MINIMUM_RECALL_AT_10 = 0.85
MINIMUM_PRECISION_AT_5 = 0.70

REQUIRED_COVERAGE_TAGS = {
    "china_criminal_general",
    "china_criminal_special",
    "german_civil_general",
    "hart_rule_theory",
    "dworkin_interpretation",
    "rousseau_sovereignty_law",
    "cross_book_comparison",
}
BOOK_COVERAGE_TAGS = {
    "hart-concept-of-law-3e-zh": "hart_rule_theory",
    "dworkin-laws-empire-zh": "dworkin_interpretation",
    "rousseau-social-contract-zh": "rousseau_sovereignty_law",
    "klunzinger-bgb-general-zh": "german_civil_general",
    "zhang-criminal-law-6e-v1": "china_criminal_general",
    "zhang-criminal-law-6e-v2": "china_criminal_special",
}

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CATEGORIES = {"single_book", "cross_book"}
_RELEVANCE_LEVELS = {0, 1, 2}
_FILTER_FIELDS = ("book_ids", "jurisdictions", "subjects")

_TOP_LEVEL_REQUIRED = {
    "schema_version",
    "dataset_id",
    "frozen_at",
    "ranking_version",
    "questions",
}
_TOP_LEVEL_ALLOWED = _TOP_LEVEL_REQUIRED | {"description"}
_QUESTION_REQUIRED = {
    "question_id",
    "question",
    "search_query",
    "category",
    "coverage_tags",
    "filters",
    "expected_book_ids",
    "candidate_scopes",
    "judgments",
}
_QUESTION_ALLOWED = _QUESTION_REQUIRED | {"split"}
_SCOPE_REQUIRED = {
    "book_id",
    "pdf_page_range",
    "chapter_path",
    "basis",
}
_JUDGMENT_REQUIRED = {
    "page_id",
    "book_id",
    "relevance",
    "supporting_sentence_ids",
    "supporting_sentence_sha256",
    "label_method",
    "review_status",
    "reviewed_at",
    "reason_code",
}

_REQUIRED_DATABASE_TABLES = {
    "index_metadata",
    "books",
    "book_jurisdictions",
    "book_subjects",
    "pages",
    "sentences",
    "chunks",
    "chunks_fts",
}

__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "EVALUATION_VERSION",
    "GOLDEN_SCHEMA_VERSION",
    "MINIMUM_PRECISION_AT_5",
    "MINIMUM_RECALL_AT_10",
    "PRECISION_K",
    "RECALL_K",
    "RETRIEVAL_LIMIT",
    "REQUIRED_COVERAGE_TAGS",
    "build_index_fingerprint",
    "evaluate_dataset",
    "evaluate_question",
    "load_golden_dataset",
    "render_evaluation_text",
    "validate_golden_dataset",
    "write_evaluation_report",
]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    path = Path(database_path)
    if not path.is_file():
        raise LegalAIError(f"索引不存在：{path}")
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"无法只读打开 SQLite 索引：{exc}") from exc


def _require_object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LegalAIError(f"{label} 必须是 JSON 对象")
    return value


def _require_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    label: str,
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise LegalAIError(f"{label} 缺少字段：{', '.join(missing)}")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LegalAIError(f"{label} 含未知字段：{', '.join(unknown)}")


def _required_string(value: Any, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegalAIError(f"{label} 必须是非空字符串")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise LegalAIError(f"{label} 过长：最多 {maximum} 个字符")
    return normalized


def _identifier(value: Any, label: str) -> str:
    result = _required_string(value, label, maximum=128)
    if not _ID_PATTERN.fullmatch(result):
        raise LegalAIError(
            f"{label} 只能包含英文字母、数字、点、下划线和连字符"
        )
    return result


def _string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool,
    maximum_items: int = 100,
) -> list[str]:
    if not isinstance(value, list):
        raise LegalAIError(f"{label} 必须是数组")
    if len(value) > maximum_items:
        raise LegalAIError(f"{label} 项目过多：最多 {maximum_items} 项")
    result = [
        _required_string(item, f"{label}[{index}]", maximum=500)
        for index, item in enumerate(value)
    ]
    if not allow_empty and not result:
        raise LegalAIError(f"{label} 不能为空")
    if len(set(result)) != len(result):
        raise LegalAIError(f"{label} 不能包含重复值")
    return result


def _require_iso_datetime(value: Any, label: str) -> str:
    result = _required_string(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegalAIError(f"{label} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise LegalAIError(f"{label} 必须包含时区")
    return result


def _round_metric(value: float) -> float:
    return round(float(value), 6)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _database_catalog(
    connection: sqlite3.Connection, *, verify_integrity: bool = True
) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }
    missing = sorted(_REQUIRED_DATABASE_TABLES - tables)
    if missing:
        raise LegalAIError(f"评测索引缺少表：{', '.join(missing)}")

    if verify_integrity:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]).lower() != "ok":
            raise LegalAIError("SQLite quick_check 未通过，拒绝执行黄金评测")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LegalAIError("SQLite 外键校验未通过，拒绝执行黄金评测")

    metadata = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT key, value FROM index_metadata ORDER BY key"
        ).fetchall()
    }
    expected_metadata = {
        "index_version": INDEX_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "segmentation_version": SEGMENTATION_VERSION,
    }
    mismatches = {
        key: {"expected": expected, "actual": metadata.get(key)}
        for key, expected in expected_metadata.items()
        if metadata.get(key) != expected
    }
    if mismatches:
        raise LegalAIError(
            "评测索引版本不匹配："
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )

    counts = {
        table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in ("books", "pages", "sentences", "chunks", "chunks_fts")
    }
    if counts["chunks"] != counts["chunks_fts"]:
        raise LegalAIError("chunks 与 chunks_fts 数量不一致")
    # A correlated NOT EXISTS against an FTS virtual table can degenerate into
    # repeated full scans.  These sets are small enough to compare directly
    # and preserve the same one-to-one invariant deterministically.
    chunk_ids = {
        str(row[0])
        for row in connection.execute("SELECT chunk_id FROM chunks").fetchall()
    }
    fts_chunk_ids = {
        str(row[0])
        for row in connection.execute("SELECT chunk_id FROM chunks_fts").fetchall()
    }
    if chunk_ids != fts_chunk_ids:
        raise LegalAIError("chunks 与 chunks_fts 的 ID 集合不一致")

    book_rows = connection.execute(
        """
        SELECT book_id, sha256, pdf_page_count, pipeline_version
        FROM books ORDER BY book_id
        """
    ).fetchall()
    if not book_rows:
        raise LegalAIError("评测索引不包含书籍")
    books: dict[str, dict[str, Any]] = {}
    for row in book_rows:
        book_id = str(row["book_id"])
        digest = str(row["sha256"])
        if not _SHA256_PATTERN.fullmatch(digest):
            raise LegalAIError(f"{book_id}：书籍 SHA-256 无效")
        if str(row["pipeline_version"]) != PIPELINE_VERSION:
            raise LegalAIError(f"{book_id}：书籍处理版本不一致")
        books[book_id] = {
            "sha256": digest,
            "pdf_page_count": int(row["pdf_page_count"]),
            "jurisdictions": sorted(
                str(item[0])
                for item in connection.execute(
                    """
                    SELECT jurisdiction FROM book_jurisdictions
                    WHERE book_id = ? ORDER BY jurisdiction
                    """,
                    (book_id,),
                ).fetchall()
            ),
            "subjects": sorted(
                str(item[0])
                for item in connection.execute(
                    """
                    SELECT subject FROM book_subjects
                    WHERE book_id = ? ORDER BY subject
                    """,
                    (book_id,),
                ).fetchall()
            ),
        }
    return {
        "metadata": expected_metadata,
        "counts": counts,
        "books": books,
    }


def _build_index_fingerprint(
    database_path: Path, *, verify_integrity: bool
) -> dict[str, Any]:
    connection = _read_only_connection(Path(database_path))
    try:
        catalog = _database_catalog(
            connection, verify_integrity=verify_integrity
        )
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"读取评测索引失败：{exc}") from exc
    finally:
        connection.close()

    components = {
        "index_metadata": catalog["metadata"],
        "counts": catalog["counts"],
        "books": {
            book_id: {
                "sha256": value["sha256"],
                "pdf_page_count": value["pdf_page_count"],
            }
            for book_id, value in sorted(catalog["books"].items())
        },
        "ranking_version": RANKING_VERSION,
        "score_weights": dict(sorted(SCORE_WEIGHTS.items())),
    }
    return {
        "sha256": _sha256_json(components),
        "components": components,
    }


def build_index_fingerprint(database_path: Path) -> dict[str, Any]:
    """Return a validated identity that excludes mutable retrieval history."""

    return _build_index_fingerprint(
        Path(database_path), verify_integrity=True
    )


def load_golden_dataset(path: Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LegalAIError(f"无法读取黄金问题集：{source}") from exc
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"黄金问题集不是合法 JSON：{source}") from exc
    if not isinstance(value, dict):
        raise LegalAIError("黄金问题集根节点必须是对象")
    return value


def _validate_scope(
    value: Any,
    *,
    label: str,
    books: Mapping[str, Mapping[str, Any]],
    allowed_book_ids: set[str] | None,
) -> dict[str, Any]:
    scope = _require_object(value, label)
    _require_fields(
        scope,
        required=_SCOPE_REQUIRED,
        allowed=_SCOPE_REQUIRED,
        label=label,
    )
    book_id = _identifier(scope["book_id"], f"{label}.book_id")
    if book_id not in books:
        raise LegalAIError(f"{label}.book_id 不在索引中：{book_id}")
    if allowed_book_ids is not None and book_id not in allowed_book_ids:
        raise LegalAIError(f"{label} 超出 book_ids 过滤范围：{book_id}")

    page_range = scope["pdf_page_range"]
    normalized_range: list[int] | None
    if page_range is None:
        normalized_range = None
    else:
        if (
            not isinstance(page_range, list)
            or len(page_range) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in page_range)
        ):
            raise LegalAIError(f"{label}.pdf_page_range 必须是 [起页, 止页] 或 null")
        start, end = page_range
        maximum = int(books[book_id]["pdf_page_count"])
        if not 1 <= start <= end <= maximum:
            raise LegalAIError(
                f"{label}.pdf_page_range 超出 {book_id} 的 PDF 页范围"
            )
        normalized_range = [start, end]

    chapter_path = _string_list(
        scope["chapter_path"],
        f"{label}.chapter_path",
        allow_empty=True,
        maximum_items=20,
    )
    basis = _string_list(
        scope["basis"],
        f"{label}.basis",
        allow_empty=False,
        maximum_items=10,
    )
    if normalized_range is None and not chapter_path:
        raise LegalAIError(
            f"{label} 必须至少给出 pdf_page_range 或 chapter_path"
        )
    return {
        "book_id": book_id,
        "pdf_page_range": normalized_range,
        "chapter_path": chapter_path,
        "basis": basis,
    }


def _validate_judgment(
    value: Any,
    *,
    label: str,
    connection: sqlite3.Connection,
    books: Mapping[str, Mapping[str, Any]],
    allowed_book_ids: set[str] | None,
) -> dict[str, Any]:
    judgment = _require_object(value, label)
    _require_fields(
        judgment,
        required=_JUDGMENT_REQUIRED,
        allowed=_JUDGMENT_REQUIRED,
        label=label,
    )
    page_id = _required_string(judgment["page_id"], f"{label}.page_id", 200)
    book_id = _identifier(judgment["book_id"], f"{label}.book_id")
    if book_id not in books:
        raise LegalAIError(f"{label}.book_id 不在索引中：{book_id}")
    if allowed_book_ids is not None and book_id not in allowed_book_ids:
        raise LegalAIError(f"{label} 超出 book_ids 过滤范围：{book_id}")

    relevance = judgment["relevance"]
    if (
        isinstance(relevance, bool)
        or not isinstance(relevance, int)
        or relevance not in _RELEVANCE_LEVELS
    ):
        raise LegalAIError(f"{label}.relevance 只能是 0、1 或 2")

    sentence_ids = _string_list(
        judgment["supporting_sentence_ids"],
        f"{label}.supporting_sentence_ids",
        allow_empty=True,
        maximum_items=20,
    )
    sentence_hashes = _string_list(
        judgment["supporting_sentence_sha256"],
        f"{label}.supporting_sentence_sha256",
        allow_empty=True,
        maximum_items=20,
    )
    if len(sentence_ids) != len(sentence_hashes):
        raise LegalAIError(f"{label} 的句子 ID 与 SHA-256 数量不一致")
    for digest in sentence_hashes:
        if not _SHA256_PATTERN.fullmatch(digest):
            raise LegalAIError(f"{label} 包含无效的句子 SHA-256")

    label_method = _required_string(
        judgment["label_method"], f"{label}.label_method", 64
    )
    if label_method != "source_inspection":
        raise LegalAIError(f"{label}.label_method 必须是 source_inspection")
    review_status = _required_string(
        judgment["review_status"], f"{label}.review_status", 32
    )
    if review_status != "verified":
        raise LegalAIError(f"{label}.review_status 必须是 verified")
    reviewed_at = _require_iso_datetime(
        judgment["reviewed_at"], f"{label}.reviewed_at"
    )
    reason_code = _required_string(
        judgment["reason_code"], f"{label}.reason_code", 128
    )

    page = connection.execute(
        """
        SELECT book_id, quality_flags_json
        FROM pages WHERE page_id = ?
        """,
        (page_id,),
    ).fetchone()
    if page is None:
        raise LegalAIError(f"{label}.page_id 不在索引中：{page_id}")
    if str(page["book_id"]) != book_id:
        raise LegalAIError(f"{label} 的 page_id 与 book_id 不一致")

    try:
        quality_flags = json.loads(str(page["quality_flags_json"]))
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"{page_id} 的质量标记不是合法 JSON") from exc
    if not isinstance(quality_flags, list):
        raise LegalAIError(f"{page_id} 的质量标记必须是数组")

    if relevance > 0:
        if not sentence_ids:
            raise LegalAIError(f"{label} 的正例必须绑定至少一个支持句子")
        if quality_flags:
            raise LegalAIError(f"{label} 的正例不能引用带质量标记的页面")
        placeholders = ",".join("?" for _ in sentence_ids)
        rows = connection.execute(
            f"""
            SELECT sentence_id, page_id, normalized_text, text_sha256
            FROM sentences WHERE sentence_id IN ({placeholders})
            """,
            sentence_ids,
        ).fetchall()
        by_id = {str(row["sentence_id"]): row for row in rows}
        if len(by_id) != len(sentence_ids):
            missing = sorted(set(sentence_ids) - set(by_id))
            raise LegalAIError(
                f"{label} 引用了不存在的句子：{', '.join(missing)}"
            )
        for sentence_id, expected_hash in zip(sentence_ids, sentence_hashes):
            row = by_id[sentence_id]
            if str(row["page_id"]) != page_id:
                raise LegalAIError(f"{label} 的支持句子不属于所标页面")
            calculated_hash = hashlib.sha256(
                str(row["normalized_text"]).encode("utf-8")
            ).hexdigest()
            if (
                calculated_hash != str(row["text_sha256"])
                or str(row["text_sha256"]) != expected_hash
            ):
                raise LegalAIError(f"{label} 的支持句子 SHA-256 与索引不一致")
    elif sentence_ids or sentence_hashes:
        raise LegalAIError(f"{label} 的负例不得携带支持句子")

    return {
        "page_id": page_id,
        "book_id": book_id,
        "relevance": relevance,
        "supporting_sentence_ids": sentence_ids,
        "supporting_sentence_sha256": sentence_hashes,
        "label_method": label_method,
        "review_status": review_status,
        "reviewed_at": reviewed_at,
        "reason_code": reason_code,
    }


def _validate_question(
    value: Any,
    *,
    index: int,
    connection: sqlite3.Connection,
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    label = f"questions[{index}]"
    question = _require_object(value, label)
    _require_fields(
        question,
        required=_QUESTION_REQUIRED,
        allowed=_QUESTION_ALLOWED,
        label=label,
    )
    question_id = _identifier(question["question_id"], f"{label}.question_id")
    display_question = _required_string(
        question["question"], f"{label}.question", maximum=1000
    )
    search_query = _required_string(
        question["search_query"], f"{label}.search_query", maximum=200
    )
    category = _required_string(question["category"], f"{label}.category", 32)
    if category not in _CATEGORIES:
        raise LegalAIError(
            f"{label}.category 只能是 single_book 或 cross_book"
        )
    coverage_tags = _string_list(
        question["coverage_tags"],
        f"{label}.coverage_tags",
        allow_empty=False,
        maximum_items=20,
    )

    filters_value = _require_object(question["filters"], f"{label}.filters")
    _require_fields(
        filters_value,
        required=set(_FILTER_FIELDS),
        allowed=set(_FILTER_FIELDS),
        label=f"{label}.filters",
    )
    filters = {
        field: _string_list(
            filters_value[field],
            f"{label}.filters.{field}",
            allow_empty=True,
            maximum_items=50,
        )
        for field in _FILTER_FIELDS
    }
    books = catalog["books"]
    unknown_filtered_books = sorted(set(filters["book_ids"]) - set(books))
    if unknown_filtered_books:
        raise LegalAIError(
            f"{label} 过滤了不存在的书籍：{', '.join(unknown_filtered_books)}"
        )

    expected_book_ids = _string_list(
        question["expected_book_ids"],
        f"{label}.expected_book_ids",
        allow_empty=False,
        maximum_items=20,
    )
    unknown_expected = sorted(set(expected_book_ids) - set(books))
    if unknown_expected:
        raise LegalAIError(
            f"{label} 预期了不存在的书籍：{', '.join(unknown_expected)}"
        )
    if category == "single_book" and len(expected_book_ids) != 1:
        raise LegalAIError(f"{label} 的单书问题必须且只能预期一本书")
    if category == "cross_book" and len(expected_book_ids) < 2:
        raise LegalAIError(f"{label} 的跨书问题必须预期至少两本书")

    if category == "cross_book" and "cross_book_comparison" not in coverage_tags:
        raise LegalAIError(f"{label} 的跨书问题必须标注 cross_book_comparison")
    if category == "single_book":
        required_tag = BOOK_COVERAGE_TAGS.get(expected_book_ids[0])
        if required_tag is not None and required_tag not in coverage_tags:
            raise LegalAIError(
                f"{label} 缺少与代表书籍对应的覆盖标签 {required_tag}"
            )
    for book_id, domain_tag in BOOK_COVERAGE_TAGS.items():
        if domain_tag in coverage_tags and book_id not in expected_book_ids:
            raise LegalAIError(
                f"{label} 的覆盖标签 {domain_tag} 与预期书籍不一致"
            )

    if filters["book_ids"] and not set(expected_book_ids).issubset(
        filters["book_ids"]
    ):
        raise LegalAIError(f"{label} 的预期书籍超出 book_ids 过滤范围")
    for book_id in expected_book_ids:
        metadata = books[book_id]
        if filters["jurisdictions"] and not set(filters["jurisdictions"]).intersection(
            metadata["jurisdictions"]
        ):
            raise LegalAIError(f"{label} 的法域过滤会排除预期书籍 {book_id}")
        if filters["subjects"] and not set(filters["subjects"]).intersection(
            metadata["subjects"]
        ):
            raise LegalAIError(f"{label} 的学科过滤会排除预期书籍 {book_id}")

    allowed_book_ids = set(filters["book_ids"]) if filters["book_ids"] else None
    scopes_value = question["candidate_scopes"]
    if not isinstance(scopes_value, list) or not scopes_value:
        raise LegalAIError(f"{label}.candidate_scopes 必须是非空数组")
    candidate_scopes = [
        _validate_scope(
            item,
            label=f"{label}.candidate_scopes[{scope_index}]",
            books=books,
            allowed_book_ids=allowed_book_ids,
        )
        for scope_index, item in enumerate(scopes_value)
    ]
    unexpected_scope_books = sorted(
        {scope["book_id"] for scope in candidate_scopes}
        - set(expected_book_ids)
    )
    if unexpected_scope_books:
        raise LegalAIError(
            f"{label}.candidate_scopes 包含非预期书籍："
            + ", ".join(unexpected_scope_books)
        )
    scoped_books = {scope["book_id"] for scope in candidate_scopes}
    missing_scoped_books = sorted(set(expected_book_ids) - scoped_books)
    if missing_scoped_books:
        raise LegalAIError(
            f"{label}.candidate_scopes 未覆盖预期书籍："
            + ", ".join(missing_scoped_books)
        )

    judgments_value = question["judgments"]
    if not isinstance(judgments_value, list):
        raise LegalAIError(f"{label}.judgments 必须是数组")
    judgments = [
        _validate_judgment(
            item,
            label=f"{label}.judgments[{judgment_index}]",
            connection=connection,
            books=books,
            allowed_book_ids=allowed_book_ids,
        )
        for judgment_index, item in enumerate(judgments_value)
    ]
    judged_page_ids = [item["page_id"] for item in judgments]
    if len(set(judged_page_ids)) != len(judged_page_ids):
        raise LegalAIError(f"{label}.judgments 不能重复判断同一页面")

    source_scoped_positive_page_ids: list[str] = []
    for judgment_index, judgment in enumerate(judgments):
        book_metadata = books[judgment["book_id"]]
        if filters["jurisdictions"] and not set(
            filters["jurisdictions"]
        ).intersection(book_metadata["jurisdictions"]):
            raise LegalAIError(
                f"{label}.judgments[{judgment_index}] 超出法域过滤范围"
            )
        if filters["subjects"] and not set(filters["subjects"]).intersection(
            book_metadata["subjects"]
        ):
            raise LegalAIError(
                f"{label}.judgments[{judgment_index}] 超出学科过滤范围"
            )
        if (
            judgment["relevance"] > 0
            and judgment["book_id"] not in expected_book_ids
        ):
            raise LegalAIError(
                f"{label}.judgments[{judgment_index}] 的正例来自非预期书籍"
            )

    for judgment_index, judgment in enumerate(judgments):
        if judgment["relevance"] <= 0:
            continue
        page_row = connection.execute(
            "SELECT pdf_page, chapter_path_json FROM pages WHERE page_id = ?",
            (judgment["page_id"],),
        ).fetchone()
        assert page_row is not None
        try:
            page_chapter = json.loads(str(page_row["chapter_path_json"]))
        except json.JSONDecodeError as exc:
            raise LegalAIError(
                f"{label}.judgments[{judgment_index}] 页面章节 JSON 损坏"
            ) from exc
        if not isinstance(page_chapter, list):
            raise LegalAIError(
                f"{label}.judgments[{judgment_index}] 页面章节必须是数组"
            )
        pdf_page = int(page_row["pdf_page"])
        matching_scope = False
        for scope in candidate_scopes:
            if scope["book_id"] != judgment["book_id"]:
                continue
            page_range = scope["pdf_page_range"]
            range_matches = page_range is not None and (
                page_range[0] <= pdf_page <= page_range[1]
            )
            chapter_matches = bool(scope["chapter_path"]) and all(
                part in page_chapter for part in scope["chapter_path"]
            )
            # Page ranges and outline paths are parallel provenance anchors.
            # PDF outline boundaries can lag a heading by one page, so either
            # independently inspected anchor is sufficient.
            if range_matches or chapter_matches:
                matching_scope = True
                break
        if matching_scope:
            source_scoped_positive_page_ids.append(judgment["page_id"])

    normalized: dict[str, Any] = {
        "question_id": question_id,
        "question": display_question,
        "search_query": search_query,
        "category": category,
        "coverage_tags": coverage_tags,
        "filters": filters,
        "expected_book_ids": expected_book_ids,
        "candidate_scopes": candidate_scopes,
        "judgments": judgments,
        "source_scoped_positive_page_ids": source_scoped_positive_page_ids,
    }
    if "split" in question:
        split = _required_string(question["split"], f"{label}.split", 32)
        if split not in {"development", "acceptance"}:
            raise LegalAIError(
                f"{label}.split 只能是 development 或 acceptance"
            )
        normalized["split"] = split
    return normalized


def validate_golden_dataset(
    dataset: Mapping[str, Any], database_path: Path
) -> dict[str, Any]:
    """Strictly validate golden-v1 structure and every persisted qrel anchor."""

    value = _require_object(dataset, "黄金问题集")
    _require_fields(
        value,
        required=_TOP_LEVEL_REQUIRED,
        allowed=_TOP_LEVEL_ALLOWED,
        label="黄金问题集",
    )
    if value["schema_version"] != GOLDEN_SCHEMA_VERSION:
        raise LegalAIError(
            "黄金问题集 schema_version 必须是 " + GOLDEN_SCHEMA_VERSION
        )
    dataset_id = _identifier(value["dataset_id"], "dataset_id")
    frozen_at = _require_iso_datetime(value["frozen_at"], "frozen_at")
    pool_origin_ranking_version = _required_string(
        value["ranking_version"], "ranking_version", maximum=64
    )
    questions_value = value["questions"]
    if not isinstance(questions_value, list) or not questions_value:
        raise LegalAIError("黄金问题集 questions 必须是非空数组")

    connection = _read_only_connection(Path(database_path))
    try:
        catalog = _database_catalog(connection)
        questions = [
            _validate_question(
                item,
                index=index,
                connection=connection,
                catalog=catalog,
            )
            for index, item in enumerate(questions_value)
        ]
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"验证黄金问题集时读取索引失败：{exc}") from exc
    finally:
        connection.close()

    question_ids = [item["question_id"] for item in questions]
    if len(set(question_ids)) != len(question_ids):
        raise LegalAIError("黄金问题集 question_id 必须全局唯一")
    normalized_questions = [
        " ".join(item["question"].casefold().split()) for item in questions
    ]
    if len(set(normalized_questions)) != len(normalized_questions):
        raise LegalAIError("黄金问题集不能复制同一自然语言问题来刷数量")
    normalized_queries = [
        " ".join(item["search_query"].casefold().split()) for item in questions
    ]
    if len(set(normalized_queries)) != len(normalized_queries):
        raise LegalAIError("黄金问题集 search_query 必须全局唯一")

    normalized: dict[str, Any] = {
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "frozen_at": frozen_at,
        "ranking_version": pool_origin_ranking_version,
        "questions": questions,
    }
    if "description" in value:
        normalized["description"] = _required_string(
            value["description"], "description", maximum=2000
        )
    return normalized


def _deduplicate_result_pages(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_rank, result in enumerate(results, start=1):
        page_id = str(result["page_id"])
        if page_id in seen:
            continue
        seen.add(page_id)
        scores = result.get("scores")
        final_score = None
        if isinstance(scores, Mapping) and isinstance(scores.get("final"), (int, float)):
            candidate = float(scores["final"])
            if math.isfinite(candidate):
                final_score = round(candidate, 6)
        unique.append(
            {
                "page_rank": len(unique) + 1,
                "first_result_rank": raw_rank,
                "page_id": page_id,
                "book_id": str(result["book_id"]),
                "pdf_page": int(result["pdf_page"]),
                "representative_chunk_id": str(result["chunk_id"]),
                "final_score": final_score,
            }
        )
    return unique


def _evaluate_validated_question(
    *, database_path: Path, question: Mapping[str, Any]
) -> dict[str, Any]:
    retrieval = search_index(
        database_path=Path(database_path),
        query=str(question["search_query"]),
        limit=RETRIEVAL_LIMIT,
        book_ids=question["filters"]["book_ids"],
        jurisdictions=question["filters"]["jurisdictions"],
        subjects=question["filters"]["subjects"],
        record_run=False,
    )
    if retrieval.get("ranking_version") != RANKING_VERSION:
        raise LegalAIError("检索结果 ranking_version 与评测器不一致")
    if retrieval.get("score_weights") != SCORE_WEIGHTS:
        raise LegalAIError("检索结果 score_weights 与评测器不一致")
    raw_results = retrieval.get("results")
    if not isinstance(raw_results, list):
        raise LegalAIError("检索结果缺少 results 数组")
    if len(raw_results) > RETRIEVAL_LIMIT:
        raise LegalAIError("检索结果超过固定评测上限")

    unique_pages = _deduplicate_result_pages(raw_results)
    top_five = unique_pages[:PRECISION_K]
    judgments = {
        str(item["page_id"]): item for item in question["judgments"]
    }
    positive_pages = {
        page_id
        for page_id, judgment in judgments.items()
        if int(judgment["relevance"]) > 0
    }
    retrieved_page_ids = {item["page_id"] for item in unique_pages}
    recall_hits = len(positive_pages.intersection(retrieved_page_ids))
    precision_hits = sum(
        item["page_id"] in positive_pages for item in top_five
    )
    unjudged_top_five = [
        item["page_id"]
        for item in top_five
        if item["page_id"] not in judgments
    ]
    top_five_page_ids = {item["page_id"] for item in top_five}
    source_scoped_positive_pages = set(
        question.get("source_scoped_positive_page_ids", [])
    )
    independent_positive_pages = sorted(
        (positive_pages - top_five_page_ids) & source_scoped_positive_pages
    )

    positive_books = {
        str(judgment["book_id"])
        for judgment in judgments.values()
        if int(judgment["relevance"]) > 0
    }
    missing_expected_positive = sorted(
        set(question["expected_book_ids"]) - positive_books
    )
    incomplete_reasons: list[str] = []
    if not positive_pages:
        incomplete_reasons.append("no_verified_positive_pages")
    if missing_expected_positive:
        incomplete_reasons.append("expected_books_without_positive_qrels")
    if unjudged_top_five:
        incomplete_reasons.append("unjudged_top_five_pages")
    if not independent_positive_pages:
        incomplete_reasons.append(
            "no_source_scoped_positive_qrel_outside_top_five"
        )
    complete = not incomplete_reasons

    book_counts = Counter(item["book_id"] for item in unique_pages)
    result_total = len(unique_pages)
    dominant_share = (
        _round_metric(max(book_counts.values()) / result_total)
        if result_total
        else 0.0
    )
    expected_books = set(question["expected_book_ids"])
    present_expected_books = sorted(expected_books.intersection(book_counts))
    relevant_expected_books = sorted(
        {
            judgments[item["page_id"]]["book_id"]
            for item in unique_pages
            if item["page_id"] in positive_pages
            and judgments[item["page_id"]]["book_id"] in expected_books
        }
    )
    expected_count = len(expected_books)
    source_distribution = {
        "top_10_book_counts": dict(sorted(book_counts.items())),
        "top_10_distinct_book_count": len(book_counts),
        "top_10_dominant_book_share": dominant_share,
        "expected_books_present_at_10": present_expected_books,
        "expected_book_presence_at_10": _round_metric(
            len(present_expected_books) / expected_count
        ),
        "relevant_expected_books_hit_at_10": relevant_expected_books,
        "relevant_expected_book_coverage_at_10": _round_metric(
            len(relevant_expected_books) / expected_count
        ),
    }

    metrics = {
        "recall_at_10": (
            _round_metric(recall_hits / len(positive_pages))
            if complete
            else None
        ),
        "precision_at_5": (
            _round_metric(precision_hits / PRECISION_K)
            if complete
            else None
        ),
        "positive_qrel_count": len(positive_pages),
        "recall_hit_count": recall_hits,
        "precision_hit_count": precision_hits,
        "precision_denominator": PRECISION_K,
        "known_positive_precision_lower_bound": _round_metric(
            precision_hits / PRECISION_K
        ),
    }
    return {
        "question_id": question["question_id"],
        "question": question["question"],
        "search_query": question["search_query"],
        "category": question["category"],
        "split": question.get("split"),
        "coverage_tags": list(question["coverage_tags"]),
        "filters": question["filters"],
        "expected_book_ids": list(question["expected_book_ids"]),
        "complete": complete,
        "incomplete_reasons": incomplete_reasons,
        "missing_expected_positive_book_ids": missing_expected_positive,
        "unjudged_top_five_page_ids": unjudged_top_five,
        "source_scoped_positive_page_ids_outside_top_five": (
            independent_positive_pages
        ),
        "raw_result_count": len(raw_results),
        "unique_page_result_count": len(unique_pages),
        "unique_page_results": unique_pages,
        "metrics": metrics,
        "source_distribution": source_distribution,
    }


def evaluate_question(
    *, database_path: Path, question: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and evaluate one golden-v1 question against the real top ten."""

    connection = _read_only_connection(Path(database_path))
    try:
        catalog = _database_catalog(connection)
        normalized = _validate_question(
            question,
            index=0,
            connection=connection,
            catalog=catalog,
        )
    except sqlite3.DatabaseError as exc:
        raise LegalAIError(f"验证黄金问题时读取索引失败：{exc}") from exc
    finally:
        connection.close()
    return _evaluate_validated_question(
        database_path=Path(database_path), question=normalized
    )


def _aggregate_report(
    *,
    dataset: Mapping[str, Any],
    question_reports: Sequence[Mapping[str, Any]],
    index_fingerprint: Mapping[str, Any],
) -> dict[str, Any]:
    all_complete = all(bool(item["complete"]) for item in question_reports)
    completed_count = sum(bool(item["complete"]) for item in question_reports)

    recall_values = [
        float(item["metrics"]["recall_at_10"])
        for item in question_reports
        if item["metrics"]["recall_at_10"] is not None
    ]
    precision_values = [
        float(item["metrics"]["precision_at_5"])
        for item in question_reports
        if item["metrics"]["precision_at_5"] is not None
    ]
    total_positive = sum(
        int(item["metrics"]["positive_qrel_count"])
        for item in question_reports
    )
    total_recall_hits = sum(
        int(item["metrics"]["recall_hit_count"])
        for item in question_reports
    )
    total_precision_hits = sum(
        int(item["metrics"]["precision_hit_count"])
        for item in question_reports
    )
    official_macro_recall = (
        _round_metric(_mean(recall_values)) if all_complete else None
    )
    official_macro_precision = (
        _round_metric(_mean(precision_values)) if all_complete else None
    )
    official_micro_recall = (
        _round_metric(total_recall_hits / total_positive)
        if all_complete and total_positive
        else None
    )
    official_micro_precision = (
        _round_metric(
            total_precision_hits / (PRECISION_K * len(question_reports))
        )
        if all_complete
        else None
    )

    all_book_ids = sorted(index_fingerprint["components"]["books"])
    dataset_book_ids = sorted(
        {
            book_id
            for question in dataset["questions"]
            for book_id in question["expected_book_ids"]
        }
    )
    uncovered_indexed_books = sorted(set(all_book_ids) - set(dataset_book_ids))
    aggregate_book_counts: Counter[str] = Counter()
    single_book_question_counts: Counter[str] = Counter()
    cross_reports: list[Mapping[str, Any]] = []
    for question, item in zip(dataset["questions"], question_reports):
        aggregate_book_counts.update(
            item["source_distribution"]["top_10_book_counts"]
        )
        if question["category"] == "single_book":
            for book_id in question["expected_book_ids"]:
                single_book_question_counts[book_id] += 1
        else:
            cross_reports.append(item)

    aggregate_result_count = sum(aggregate_book_counts.values())
    aggregate_book_shares = {
        book_id: _round_metric(count / aggregate_result_count)
        for book_id, count in sorted(aggregate_book_counts.items())
    } if aggregate_result_count else {}

    cross_expected_total = sum(
        len(item["expected_book_ids"]) for item in cross_reports
    )
    cross_relevant_hit_total = sum(
        len(
            item["source_distribution"][
                "relevant_expected_books_hit_at_10"
            ]
        )
        for item in cross_reports
    )
    cross_with_two_relevant_books = sum(
        len(
            item["source_distribution"][
                "relevant_expected_books_hit_at_10"
            ]
        )
        >= 2
        for item in cross_reports
    )
    cross_source_gate = bool(cross_reports) and (
        cross_with_two_relevant_books == len(cross_reports)
    )

    question_count = len(question_reports)
    cross_count = len(cross_reports)
    single_counts_complete = all(
        single_book_question_counts.get(book_id, 0)
        >= MINIMUM_SINGLE_BOOK_QUESTIONS_PER_BOOK
        for book_id in dataset_book_ids
    )
    gates = {
        "labels_complete": all_complete,
        "question_count_at_least_30": question_count >= MINIMUM_QUESTION_COUNT,
        "dataset_books_all_indexed": set(dataset_book_ids)
        <= set(all_book_ids),
        "each_dataset_book_has_at_least_3_single_book_questions": (
            single_counts_complete
        ),
        "cross_book_question_count_at_least_5": (
            cross_count >= MINIMUM_CROSS_BOOK_QUESTION_COUNT
        ),
        "all_required_coverage_tags_present": REQUIRED_COVERAGE_TAGS.issubset(
            {
                tag
                for question in dataset["questions"]
                for tag in question["coverage_tags"]
            }
        ),
        "macro_recall_at_10_at_least_0_85": (
            official_macro_recall is not None
            and official_macro_recall >= MINIMUM_RECALL_AT_10
        ),
        "macro_precision_at_5_at_least_0_70": (
            official_macro_precision is not None
            and official_macro_precision >= MINIMUM_PRECISION_AT_5
        ),
        "every_cross_book_question_hits_2_relevant_books_at_10": (
            cross_source_gate
        ),
    }
    passed = all(gates.values())

    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if all_complete else "incomplete",
        "complete": all_complete,
        "passed": passed,
        "dataset": {
            "schema_version": dataset["schema_version"],
            "dataset_id": dataset["dataset_id"],
            "frozen_at": dataset["frozen_at"],
            "pool_origin_ranking_version": dataset["ranking_version"],
            "target_ranking_version": RANKING_VERSION,
            "sha256": _sha256_json(dataset),
            "question_count": question_count,
        },
        "index_fingerprint": index_fingerprint,
        "corpus_coverage": {
            "dataset_book_count": len(dataset_book_ids),
            "dataset_book_ids": dataset_book_ids,
            "indexed_book_count": len(all_book_ids),
            "uncovered_indexed_books": uncovered_indexed_books,
            "note": (
                "指标只覆盖数据集包含的书；uncovered_indexed_books 里的书"
                "尚未出题，不代表其检索质量。"
            ),
        },
        "policy": {
            "retrieval_limit": RETRIEVAL_LIMIT,
            "retrieval_result_unit": "chunk",
            "evaluation_unit": "unique_page_within_top_10_chunk_results",
            "duplicate_page_policy": "keep_first_result_rank",
            "recall_candidate_policy": (
                "deduplicate the actual user-facing top 10 chunk results; "
                "do not enlarge the candidate pool to fill 10 unique pages"
            ),
            "recall_k": RECALL_K,
            "precision_k": PRECISION_K,
            "relevant_levels": [1, 2],
            "unjudged_top_five_policy": "incomplete",
            "qrel_pool_policy": (
                "every positive qrel must be inside a declared source-inspected "
                "candidate scope, and every question must include at least one "
                "positive qrel outside the evaluated top five"
            ),
            "precision_denominator_policy": "fixed_k",
            "thresholds": {
                "minimum_question_count": MINIMUM_QUESTION_COUNT,
                "minimum_cross_book_question_count": (
                    MINIMUM_CROSS_BOOK_QUESTION_COUNT
                ),
                "minimum_single_book_questions_per_book": (
                    MINIMUM_SINGLE_BOOK_QUESTIONS_PER_BOOK
                ),
                "minimum_macro_recall_at_10": MINIMUM_RECALL_AT_10,
                "minimum_macro_precision_at_5": MINIMUM_PRECISION_AT_5,
                "required_coverage_tags": sorted(REQUIRED_COVERAGE_TAGS),
            },
        },
        "label_completeness": {
            "complete_question_count": completed_count,
            "incomplete_question_count": question_count - completed_count,
            "unjudged_top_five_page_count": sum(
                len(item["unjudged_top_five_page_ids"])
                for item in question_reports
            ),
            "missing_expected_positive_book_reference_count": sum(
                len(item["missing_expected_positive_book_ids"])
                for item in question_reports
            ),
        },
        "metrics": {
            "macro": {
                "recall_at_10": official_macro_recall,
                "precision_at_5": official_macro_precision,
            },
            "micro": {
                "recall_at_10": official_micro_recall,
                "precision_at_5": official_micro_precision,
            },
            "diagnostic_known_labels": {
                "positive_qrel_count": total_positive,
                "recall_hit_count": total_recall_hits,
                "precision_hit_count": total_precision_hits,
                "precision_denominator": PRECISION_K * question_count,
            },
        },
        "source_distribution": {
            "top_10_page_counts_by_book": dict(
                sorted(aggregate_book_counts.items())
            ),
            "top_10_page_shares_by_book": aggregate_book_shares,
            "single_book_question_counts_by_book": {
                book_id: single_book_question_counts.get(book_id, 0)
                for book_id in all_book_ids
            },
            "cross_book": {
                "question_count": cross_count,
                "expected_book_reference_count": cross_expected_total,
                "relevant_expected_book_hit_count_at_10": (
                    cross_relevant_hit_total
                ),
                "relevant_expected_book_coverage_at_10": (
                    _round_metric(
                        cross_relevant_hit_total / cross_expected_total
                    )
                    if cross_expected_total
                    else 0.0
                ),
                "questions_hitting_at_least_2_relevant_books_at_10": (
                    cross_with_two_relevant_books
                ),
            },
        },
        "acceptance": {
            "passed": passed,
            "gates": gates,
        },
        "questions": list(question_reports),
    }


def evaluate_dataset(
    *,
    database_path: Path,
    golden_path: Path | None = None,
    dataset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a frozen golden-v1 set without network, models, or DB writes."""

    if (golden_path is None) == (dataset is None):
        raise LegalAIError("golden_path 与 dataset 必须且只能提供一个")
    loaded = load_golden_dataset(Path(golden_path)) if golden_path else dataset
    assert loaded is not None
    normalized = validate_golden_dataset(loaded, Path(database_path))
    # validate_golden_dataset has already performed the expensive quick/FK
    # checks once.  Fingerprints still validate schema, versions and FTS ID
    # parity, but do not repeat the full-file check before and after ranking.
    before = _build_index_fingerprint(
        Path(database_path), verify_integrity=False
    )
    question_reports = [
        _evaluate_validated_question(
            database_path=Path(database_path), question=question
        )
        for question in normalized["questions"]
    ]
    after = _build_index_fingerprint(
        Path(database_path), verify_integrity=False
    )
    if before["sha256"] != after["sha256"]:
        raise LegalAIError("评测期间索引指纹发生变化，拒绝输出指标")
    return _aggregate_report(
        dataset=normalized,
        question_reports=question_reports,
        index_fingerprint=before,
    )


def write_evaluation_report(report: Mapping[str, Any], path: Path) -> Path:
    """Atomically write a text-free statistical evaluation report as JSON."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                report,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise LegalAIError(f"无法写入检索评测报告：{target}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def _metric_text(value: Any) -> str:
    return "未生成（标签不完整）" if value is None else f"{float(value):.3f}"


def render_evaluation_text(report: Mapping[str, Any]) -> str:
    """Render a concise Chinese summary without copying corpus text."""

    metrics = report.get("metrics", {})
    macro = metrics.get("macro", {})
    micro = metrics.get("micro", {})
    completeness = report.get("label_completeness", {})
    source = report.get("source_distribution", {})
    cross = source.get("cross_book", {})
    lines = [
        "P6 离线检索评测",
        f"状态：{report.get('status', 'unknown')}；验收："
        + ("通过" if report.get("passed") else "未通过"),
        f"问题数：{report.get('dataset', {}).get('question_count', 0)}",
        (
            "标签完整："
            f"{completeness.get('complete_question_count', 0)}；"
            "标签不完整："
            f"{completeness.get('incomplete_question_count', 0)}；"
            "前五未判断页："
            f"{completeness.get('unjudged_top_five_page_count', 0)}"
        ),
        f"Macro Recall@10：{_metric_text(macro.get('recall_at_10'))}",
        f"Macro Precision@5：{_metric_text(macro.get('precision_at_5'))}",
        f"Micro Recall@10：{_metric_text(micro.get('recall_at_10'))}",
        f"Micro Precision@5：{_metric_text(micro.get('precision_at_5'))}",
        (
            "跨书来源覆盖："
            f"{cross.get('relevant_expected_book_coverage_at_10', 0):.3f}；"
            "至少命中两本相关书的问题："
            f"{cross.get('questions_hitting_at_least_2_relevant_books_at_10', 0)}"
            f"/{cross.get('question_count', 0)}"
        ),
        "来源页分布：",
    ]
    book_counts = source.get("top_10_page_counts_by_book", {})
    if isinstance(book_counts, Mapping) and book_counts:
        for book_id, count in sorted(book_counts.items()):
            lines.append(f"- {book_id}: {count}")
    else:
        lines.append("- 无结果")

    gates = report.get("acceptance", {}).get("gates", {})
    lines.append("验收门：")
    if isinstance(gates, Mapping):
        for name, passed in gates.items():
            lines.append(f"- {'通过' if passed else '未通过'}：{name}")

    incomplete = [
        item
        for item in report.get("questions", [])
        if isinstance(item, Mapping) and not item.get("complete")
    ]
    if incomplete:
        lines.append("待补标问题：")
        for item in incomplete:
            reasons = ", ".join(item.get("incomplete_reasons", []))
            unjudged = len(item.get("unjudged_top_five_page_ids", []))
            lines.append(
                f"- {item.get('question_id')}: {reasons}；"
                f"前五未判断页 {unjudged}"
            )
    return "\n".join(lines) + "\n"

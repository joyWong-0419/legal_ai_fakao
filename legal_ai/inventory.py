from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader

from .domain import BookConfig, InventoryRecord, LegalAIError


def load_books(config_path: Path) -> list[BookConfig]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LegalAIError(f"找不到书目配置：{config_path}") from exc
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"书目配置不是合法 JSON：{exc}") from exc
    if not isinstance(raw, list):
        raise LegalAIError("books.json 顶层必须是数组")

    books = [BookConfig.from_dict(item) for item in raw]
    book_ids = [book.book_id for book in books]
    duplicates = sorted(
        book_id for book_id in set(book_ids) if book_ids.count(book_id) > 1
    )
    if duplicates:
        raise LegalAIError(f"book_id 重复：{', '.join(duplicates)}")
    return books


def resolve_book_path(repo_root: Path, relative_path: str) -> Path:
    candidate = (repo_root / relative_path).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise LegalAIError(f"书籍路径越出仓库：{relative_path}") from exc
    return candidate


def file_sha256(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def inspect_book(book: BookConfig, repo_root: Path) -> InventoryRecord:
    path = resolve_book_path(repo_root, book.file_path)
    if not path.is_file():
        raise LegalAIError(f"{book.book_id}：文件不存在：{book.file_path}")
    try:
        reader = PdfReader(path)
        actual_pages = len(reader.pages)
    except Exception as exc:
        raise LegalAIError(f"{book.book_id}：PDF 无法读取：{exc}") from exc
    if actual_pages != book.expected_pdf_pages:
        raise LegalAIError(
            f"{book.book_id}：页数不一致，"
            f"配置 {book.expected_pdf_pages}，实际 {actual_pages}"
        )
    return InventoryRecord(
        book=book,
        absolute_path=path,
        sha256=file_sha256(path),
        actual_pdf_pages=actual_pages,
    )


def inspect_books(
    books: Iterable[BookConfig], repo_root: Path
) -> list[InventoryRecord]:
    records: list[InventoryRecord] = []
    errors: list[str] = []
    for book in books:
        if not book.enabled:
            continue
        try:
            records.append(inspect_book(book, repo_root))
        except LegalAIError as exc:
            errors.append(str(exc))
    if errors:
        detail = "\n- ".join(errors)
        raise LegalAIError(f"书目审计失败：\n- {detail}")
    return records


def write_inventory_report(
    records: Iterable[InventoryRecord], report_path: Path
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "book_count": 0,
        "books": [],
    }
    for record in records:
        payload["books"].append(record.to_dict())
    payload["book_count"] = len(payload["books"])
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def format_inventory(records: Iterable[InventoryRecord]) -> str:
    lines: list[str] = []
    for record in records:
        book = record.book
        lines.extend(
            [
                f"{book.book_id} | {book.title}",
                f"  路径：{book.file_path}",
                (
                    f"  页数：{record.actual_pdf_pages} | "
                    f"SHA-256：{record.sha256}"
                ),
                (
                    f"  法域：{', '.join(book.jurisdictions)} | "
                    f"学科：{', '.join(book.subjects)}"
                ),
                (
                    f"  来源：{book.source_type} "
                    f"({book.authority_tier})"
                ),
            ]
        )
    return "\n".join(lines)


from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .domain import BookConfig, LegalAIError


PAGE_MAP_SCHEMA_VERSION = 1
PAGE_MAP_STATUSES = {"unknown", "detected", "verified", "rejected"}
PAGE_LABEL_TYPES = {"arabic", "roman", "roman_lower", "roman_upper"}


@dataclass(frozen=True)
class PageMapSegment:
    pdf_start: int
    pdf_end: int
    printed_start: int | None
    label_type: str | None
    status: str
    verified_samples: tuple[int, ...] = ()
    inspection_samples: tuple[int, ...] = ()
    note: str | None = None

    def contains(self, pdf_page: int) -> bool:
        return self.pdf_start <= pdf_page <= self.pdf_end

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_start": self.pdf_start,
            "pdf_end": self.pdf_end,
            "printed_start": self.printed_start,
            "label_type": self.label_type,
            "status": self.status,
            "verified_samples": list(self.verified_samples),
            "inspection_samples": list(self.inspection_samples),
            "note": self.note,
        }


@dataclass(frozen=True)
class BookPageMap:
    book_id: str
    segments: tuple[PageMapSegment, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "segments": [segment.to_dict() for segment in self.segments],
        }


@dataclass(frozen=True)
class PageResolution:
    pdf_page: int
    printed_page: str | None
    status: str
    segment: PageMapSegment | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_page": self.pdf_page,
            "printed_page": self.printed_page,
            "printed_page_status": self.status,
        }


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LegalAIError(f"页码映射字段 {field} 必须是整数")
    return value


def _parse_sample_pages(
    value: Any, field: str, pdf_start: int, pdf_end: int
) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LegalAIError(f"页码映射字段 {field} 必须是数组")
    samples = tuple(_require_int(item, field) for item in value)
    if len(set(samples)) != len(samples):
        raise LegalAIError(f"页码映射字段 {field} 不能包含重复 PDF 页")
    if any(item < pdf_start or item > pdf_end for item in samples):
        raise LegalAIError(
            f"页码映射字段 {field} 的样本必须位于映射段内"
        )
    return samples


def _parse_segment(value: Any, book_id: str) -> PageMapSegment:
    if not isinstance(value, dict):
        raise LegalAIError(f"{book_id}：segments 中的每一项必须是对象")
    required = {"pdf_start", "pdf_end", "status"}
    missing = sorted(required - value.keys())
    if missing:
        raise LegalAIError(
            f"{book_id}：页码映射段缺少字段：{', '.join(missing)}"
        )
    pdf_start = _require_int(value["pdf_start"], "pdf_start")
    pdf_end = _require_int(value["pdf_end"], "pdf_end")
    if pdf_start <= 0 or pdf_end < pdf_start:
        raise LegalAIError(
            f"{book_id}：非法 PDF 页范围 {pdf_start}-{pdf_end}"
        )
    status = str(value["status"])
    if status not in PAGE_MAP_STATUSES:
        raise LegalAIError(f"{book_id}：未知页码状态 {status}")

    printed_start_value = value.get("printed_start")
    label_type_value = value.get("label_type")
    if status in {"detected", "verified"}:
        printed_start = _require_int(printed_start_value, "printed_start")
        if printed_start <= 0:
            raise LegalAIError(f"{book_id}：printed_start 必须是正整数")
        label_type = str(label_type_value)
        if label_type not in PAGE_LABEL_TYPES:
            raise LegalAIError(f"{book_id}：未知页码标签类型 {label_type}")
    else:
        if printed_start_value is not None or label_type_value is not None:
            raise LegalAIError(
                f"{book_id}：{status} 段不得声明 printed_start/label_type"
            )
        printed_start = None
        label_type = None

    verified_samples = _parse_sample_pages(
        value.get("verified_samples", []),
        "verified_samples",
        pdf_start,
        pdf_end,
    )
    inspection_samples = _parse_sample_pages(
        value.get("inspection_samples", []),
        "inspection_samples",
        pdf_start,
        pdf_end,
    )
    if status == "verified":
        if len(verified_samples) < 3:
            raise LegalAIError(
                f"{book_id}：verified 段至少需要 3 个核验样本"
            )
        if pdf_start not in verified_samples or pdf_end not in verified_samples:
            raise LegalAIError(
                f"{book_id}：verified 段必须核验起点和终点"
            )
        if not any(pdf_start < item < pdf_end for item in verified_samples):
            raise LegalAIError(
                f"{book_id}：verified 段必须至少核验一个段内页面"
            )
    elif verified_samples:
        raise LegalAIError(
            f"{book_id}：只有 verified 段可以保存 verified_samples"
        )

    note_value = value.get("note")
    if note_value is not None and not isinstance(note_value, str):
        raise LegalAIError(f"{book_id}：note 必须是字符串或 null")
    return PageMapSegment(
        pdf_start=pdf_start,
        pdf_end=pdf_end,
        printed_start=printed_start,
        label_type=label_type,
        status=status,
        verified_samples=verified_samples,
        inspection_samples=inspection_samples,
        note=note_value,
    )


def load_page_maps(
    path: Path,
    page_counts: Mapping[str, int] | None = None,
    *,
    allow_unconfigured_books: bool = False,
) -> list[BookPageMap]:
    if not path.exists():
        raise LegalAIError(f"页码映射配置不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"页码映射配置不是合法 JSON：{path}") from exc
    if not isinstance(raw, list):
        raise LegalAIError("page_maps.json 顶层必须是数组")

    seen: set[str] = set()
    result: list[BookPageMap] = []
    for value in raw:
        if not isinstance(value, dict):
            raise LegalAIError("page_maps.json 的每本书配置必须是对象")
        book_id = str(value.get("book_id") or "").strip()
        if not book_id:
            raise LegalAIError("页码映射缺少 book_id")
        if book_id in seen:
            raise LegalAIError(f"重复的页码映射 book_id：{book_id}")
        seen.add(book_id)
        if page_counts is not None and book_id not in page_counts:
            if allow_unconfigured_books:
                continue
            raise LegalAIError(f"页码映射包含未知 book_id：{book_id}")
        segment_values = value.get("segments")
        if not isinstance(segment_values, list):
            raise LegalAIError(f"{book_id}：segments 必须是数组")
        segments = sorted(
            (_parse_segment(item, book_id) for item in segment_values),
            key=lambda item: (item.pdf_start, item.pdf_end),
        )
        previous_end = 0
        for segment in segments:
            if segment.pdf_start <= previous_end:
                raise LegalAIError(f"{book_id}：页码映射段存在重叠")
            previous_end = segment.pdf_end
            if page_counts is not None and segment.pdf_end > page_counts[book_id]:
                raise LegalAIError(
                    f"{book_id}：映射结束页 {segment.pdf_end} 超出 PDF 页数 "
                    f"{page_counts[book_id]}"
                )
        result.append(BookPageMap(book_id=book_id, segments=tuple(segments)))
    return result


def _roman(number: int) -> str:
    if number <= 0 or number >= 4000:
        raise LegalAIError("罗马页码仅支持 1-3999")
    values = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    remaining = number
    parts: list[str] = []
    for value, label in values:
        while remaining >= value:
            parts.append(label)
            remaining -= value
    return "".join(parts)


def format_printed_page(number: int, label_type: str) -> str:
    if label_type == "arabic":
        return str(number)
    roman = _roman(number)
    if label_type in {"roman", "roman_lower"}:
        return roman.lower()
    if label_type == "roman_upper":
        return roman
    raise LegalAIError(f"未知页码标签类型 {label_type}")


def resolve_page(
    book_map: BookPageMap | None,
    pdf_page: int,
) -> PageResolution:
    if pdf_page <= 0:
        raise LegalAIError("PDF 页必须从 1 开始")
    if book_map is None:
        return PageResolution(pdf_page, None, "unknown", None)
    for segment in book_map.segments:
        if not segment.contains(pdf_page):
            continue
        if segment.status not in {"detected", "verified"}:
            return PageResolution(pdf_page, None, segment.status, segment)
        assert segment.printed_start is not None
        assert segment.label_type is not None
        number = segment.printed_start + pdf_page - segment.pdf_start
        return PageResolution(
            pdf_page,
            format_printed_page(number, segment.label_type),
            segment.status,
            segment,
        )
    return PageResolution(pdf_page, None, "unknown", None)


def page_map_by_book(
    page_maps: Iterable[BookPageMap],
) -> dict[str, BookPageMap]:
    return {item.book_id: item for item in page_maps}


def _eligible_pages(
    project_root: Path, book_id: str, total_pages: int
) -> list[int]:
    path = project_root / "data" / "processed" / "pages" / f"{book_id}.jsonl"
    if not path.exists():
        return list(range(1, total_pages + 1))
    eligible: list[int] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                pdf_page = row.get("pdf_page")
                if (
                    isinstance(pdf_page, int)
                    and 1 <= pdf_page <= total_pages
                    and not row.get("extraction_error")
                    and int(row.get("char_count") or 0) >= 20
                ):
                    eligible.append(pdf_page)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return list(range(1, total_pages + 1))
    return eligible or list(range(1, total_pages + 1))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def select_acceptance_pages(
    *,
    book_id: str,
    book_map: BookPageMap | None,
    total_pages: int,
    eligible_pages: Sequence[int],
    per_book: int,
    seed: int,
) -> list[int]:
    if per_book not in {1, 2}:
        raise LegalAIError("人工抽样数必须是每本书 1 或 2 页")
    mapped = [
        page
        for page in eligible_pages
        if resolve_page(book_map, page).status in {"detected", "verified"}
        and (
            resolve_page(book_map, page).segment is None
            or len(resolve_page(book_map, page).segment.inspection_samples) >= 3
            or resolve_page(book_map, page).status == "verified"
        )
    ]
    population = mapped or list(eligible_pages)
    if not population:
        return []
    digest = hashlib.sha256(f"{seed}:{book_id}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    count = min(per_book, len(population))
    return sorted(rng.sample(population, count))


def build_page_map_audit(
    *,
    books: Sequence[BookConfig],
    page_maps: Sequence[BookPageMap],
    page_counts: Mapping[str, int],
    project_root: Path,
    per_book: int = 2,
    seed: int = 20260710,
) -> dict[str, Any]:
    maps = page_map_by_book(page_maps)
    book_reports: list[dict[str, Any]] = []
    totals = {status: 0 for status in sorted(PAGE_MAP_STATUSES)}
    for book in books:
        total_pages = page_counts[book.book_id]
        book_map = maps.get(book.book_id)
        counts = {status: 0 for status in sorted(PAGE_MAP_STATUSES)}
        for page in range(1, total_pages + 1):
            counts[resolve_page(book_map, page).status] += 1
        for status, count in counts.items():
            totals[status] += count
        eligible = _eligible_pages(project_root, book.book_id, total_pages)
        sample_pages = select_acceptance_pages(
            book_id=book.book_id,
            book_map=book_map,
            total_pages=total_pages,
            eligible_pages=eligible,
            per_book=per_book,
            seed=seed,
        )
        samples: list[dict[str, Any]] = []
        for pdf_page in sample_pages:
            resolution = resolve_page(book_map, pdf_page)
            snapshot_relative = (
                Path("reports")
                / "private"
                / "page_map_acceptance"
                / book.book_id
                / f"pdf-{pdf_page:04d}.png"
            )
            snapshot_absolute = project_root / snapshot_relative
            if resolution.printed_page is None:
                command = (
                    "python -m legal_ai page-map confirm "
                    f"--book-id {book.book_id} --pdf-page {pdf_page} "
                    "--no-visible-page"
                )
            else:
                command = (
                    "python -m legal_ai page-map confirm "
                    f"--book-id {book.book_id} --pdf-page {pdf_page} "
                    f"--printed-page {resolution.printed_page}"
                )
            samples.append(
                {
                    "pdf_page": pdf_page,
                    "candidate_printed_page": resolution.printed_page,
                    "status": resolution.status,
                    "obsidian_pdf_link": (
                        f"[[{book.file_path}#page={pdf_page}\\|"
                        f"打开 PDF 第 {pdf_page} 页]]"
                    ),
                    "snapshot_path": str(
                        Path("legal_ai") / snapshot_relative
                    ).replace("\\", "/"),
                    "snapshot_exists": snapshot_absolute.is_file(),
                    "snapshot_sha256": (
                        _file_sha256(snapshot_absolute)
                        if snapshot_absolute.is_file()
                        else None
                    ),
                    "confirmation_command": command,
                }
            )
        segment_reports: list[dict[str, Any]] = []
        for segment in book_map.segments if book_map else ():
            start = resolve_page(book_map, segment.pdf_start).printed_page
            end = resolve_page(book_map, segment.pdf_end).printed_page
            segment_reports.append(
                {
                    **segment.to_dict(),
                    "printed_end": end,
                    "printed_range": (
                        f"{start}-{end}" if start is not None and end is not None else None
                    ),
                }
            )
        book_reports.append(
            {
                "book_id": book.book_id,
                "title": book.title,
                "display_title": (
                    f"{book.title} {book.subtitle}"
                    if book.subtitle
                    else book.title
                ),
                "file_path": book.file_path,
                "pdf_page_count": total_pages,
                "status_counts": counts,
                "segments": segment_reports,
                "acceptance_samples": samples,
            }
        )
    return {
        "schema_version": PAGE_MAP_SCHEMA_VERSION,
        "sample_policy": {
            "per_book": per_book,
            "seed": seed,
            "deterministic": True,
            "candidate_labels_require_visual_confirmation": True,
        },
        "book_count": len(book_reports),
        "total_pdf_pages": sum(page_counts[book.book_id] for book in books),
        "status_totals": totals,
        "books": book_reports,
    }


def render_page_map_audit_markdown(audit: Mapping[str, Any]) -> str:
    policy = audit["sample_policy"]
    lines = [
        "# P3 印刷页码映射审计",
        "",
        "本报告只把 `verified` 当作已确认印刷页；`detected` 是候选映射，",
        "`unknown` 表示当前版本没有可可靠显示的印刷页码。",
        "",
        f"人工验收抽样：每本书 {policy['per_book']} 页；固定随机种子 "
        f"`{policy['seed']}`，重复运行得到相同页面。",
        "",
        "## 汇总",
        "",
        "| 书名 | PDF 页 | verified | detected | unknown | rejected |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for book in audit["books"]:
        counts = book["status_counts"]
        lines.append(
            f"| {book.get('display_title', book['title'])} | "
            f"{book['pdf_page_count']} | "
            f"{counts['verified']} | {counts['detected']} | "
            f"{counts['unknown']} | {counts['rejected']} |"
        )

    lines.extend(["", "## 分段映射", ""])
    for book in audit["books"]:
        lines.extend(
            [
                f"### {book.get('display_title', book['title'])} "
                f"(`{book['book_id']}`)",
                "",
                "| PDF 范围 | 印刷页候选 | 状态 | 实施检查页 | 说明 |",
                "|---|---|---|---|---|",
            ]
        )
        segments = book["segments"]
        if not segments:
            lines.append(
                f"| 1-{book['pdf_page_count']} | - | unknown | - | 未配置映射 |"
            )
        for segment in segments:
            inspection = ", ".join(
                str(item) for item in segment["inspection_samples"]
            ) or "-"
            note = (segment.get("note") or "-").replace("|", "\\|")
            lines.append(
                f"| {segment['pdf_start']}-{segment['pdf_end']} | "
                f"{segment['printed_range'] or '-'} | {segment['status']} | "
                f"{inspection} | {note} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 你的随机抽查页",
            "",
            "打开 PDF 指定页，只核对页面上可见的印刷页码。候选值一致时执行",
            "对应命令；当前无候选且页面确实无可见页码时，才执行",
            "`--no-visible-page` 命令。若不一致，不要执行命令；把书名、PDF 页",
            "和实际看到的页码交给实施 Agent 重新分段。",
            "",
            "| 书名 | PDF 页 | 候选印刷页 | 当前状态 | 打开原书 | 渲染快照 |",
            "|---|---:|---:|---|---|---|",
        ]
    )
    commands: list[str] = []
    for book in audit["books"]:
        for sample in book["acceptance_samples"]:
            candidate = sample["candidate_printed_page"] or "待观察（当前无候选）"
            snapshot = (
                f"[查看](private/page_map_acceptance/{book['book_id']}/"
                f"pdf-{sample['pdf_page']:04d}.png)"
                if sample.get("snapshot_exists")
                else "未生成（请打开原书）"
            )
            lines.append(
                f"| {book.get('display_title', book['title'])} | "
                f"{sample['pdf_page']} | {candidate} | "
                f"{sample['status']} | {sample['obsidian_pdf_link']} | "
                f"{snapshot} |"
            )
            commands.append(sample["confirmation_command"])
    lines.extend(["", "### 确认命令", "", "```powershell", *commands, "```", ""])
    return "\n".join(lines)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def write_page_map_audit(
    audit: Mapping[str, Any], json_path: Path, markdown_path: Path
) -> None:
    _atomic_write_text(
        json_path,
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write_text(markdown_path, render_page_map_audit_markdown(audit))


def write_page_maps(path: Path, page_maps: Sequence[BookPageMap]) -> None:
    _atomic_write_text(
        path,
        json.dumps(
            [item.to_dict() for item in page_maps],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _load_confirmation_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": PAGE_MAP_SCHEMA_VERSION,
            "confirmations": [],
        }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"人工确认日志不是合法 JSON：{path}") from exc
    if not isinstance(loaded, dict) or not isinstance(
        loaded.get("confirmations"), list
    ):
        raise LegalAIError(f"人工确认日志结构无效：{path}")
    seen: set[tuple[str, int]] = set()
    for item in loaded["confirmations"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("book_id"), str)
            or isinstance(item.get("pdf_page"), bool)
            or not isinstance(item.get("pdf_page"), int)
            or item["pdf_page"] <= 0
            or not isinstance(item.get("result"), str)
        ):
            raise LegalAIError(f"人工确认日志包含无效记录：{path}")
        key = (item["book_id"], item["pdf_page"])
        if key in seen:
            raise LegalAIError(f"人工确认日志包含重复页面：{key}")
        seen.add(key)
    return loaded


def _merge_confirmation(
    existing: Mapping[str, Any], confirmation: Mapping[str, Any]
) -> dict[str, Any]:
    keyed = {
        (item["book_id"], item["pdf_page"]): dict(item)
        for item in existing["confirmations"]
    }
    keyed[(confirmation["book_id"], confirmation["pdf_page"])] = dict(
        confirmation
    )
    return {
        "schema_version": PAGE_MAP_SCHEMA_VERSION,
        "confirmations": [
            keyed[key]
            for key in sorted(keyed, key=lambda item: (item[0], item[1]))
        ],
    }


def _write_confirmation_log(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    )


def _replace_book_segment(
    *,
    page_maps: Sequence[BookPageMap],
    book_map: BookPageMap,
    old_segment: PageMapSegment,
    new_segment: PageMapSegment,
) -> list[BookPageMap]:
    updated_book = replace(
        book_map,
        segments=tuple(
            new_segment if item == old_segment else item
            for item in book_map.segments
        ),
    )
    return [
        updated_book if item.book_id == book_map.book_id else item
        for item in page_maps
    ]


def _required_acceptance_pages(
    *,
    audit_path: Path | None,
    book_id: str,
    segment: PageMapSegment,
) -> set[int]:
    if audit_path is None or not audit_path.exists():
        return set()
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"页码审计报告不是合法 JSON：{audit_path}") from exc
    books = audit.get("books") if isinstance(audit, dict) else None
    if not isinstance(books, list):
        raise LegalAIError(f"页码审计报告结构无效：{audit_path}")
    for book in books:
        if not isinstance(book, dict) or book.get("book_id") != book_id:
            continue
        samples = book.get("acceptance_samples")
        if not isinstance(samples, list):
            raise LegalAIError(f"页码审计报告缺少抽样页：{book_id}")
        pages = {
            item["pdf_page"]
            for item in samples
            if isinstance(item, dict)
            and isinstance(item.get("pdf_page"), int)
            and item.get("candidate_printed_page") is not None
            and segment.contains(item["pdf_page"])
        }
        return pages
    raise LegalAIError(f"页码审计报告中不存在 book_id：{book_id}")


def _confirmed_matching_pages(
    *,
    confirmation_log: Mapping[str, Any],
    book_map: BookPageMap,
    segment: PageMapSegment,
) -> tuple[set[int], bool]:
    matches: set[int] = set()
    has_contradiction = False
    for item in confirmation_log["confirmations"]:
        if item["book_id"] != book_map.book_id or not segment.contains(
            item["pdf_page"]
        ):
            continue
        current = resolve_page(book_map, item["pdf_page"])
        if (
            item["result"] == "match"
            and current.printed_page is not None
            and str(item.get("observed_printed_page") or "").casefold()
            == current.printed_page.casefold()
        ):
            matches.add(item["pdf_page"])
        elif item["result"] in {
            "mismatch",
            "candidate_but_no_visible_page",
            "unexpected_printed_page",
        }:
            has_contradiction = True
    return matches, has_contradiction


def confirm_page_sample(
    *,
    maps_path: Path,
    confirmation_path: Path,
    audit_path: Path | None = None,
    page_counts: Mapping[str, int],
    book_id: str,
    pdf_page: int,
    observed_printed_page: str | None,
    no_visible_page: bool,
) -> dict[str, Any]:
    if (observed_printed_page is None) == (not no_visible_page):
        raise LegalAIError(
            "必须且只能提供 --printed-page 或 --no-visible-page 其中一项"
        )
    if book_id not in page_counts:
        raise LegalAIError(f"未知 book_id：{book_id}")
    if not 1 <= pdf_page <= page_counts[book_id]:
        raise LegalAIError(
            f"{book_id}：PDF 页 {pdf_page} 超出 1-{page_counts[book_id]}"
        )

    confirmation_log = _load_confirmation_log(confirmation_path)
    page_maps = load_page_maps(maps_path, page_counts)
    maps = page_map_by_book(page_maps)
    book_map = maps.get(book_id)
    resolution = resolve_page(book_map, pdf_page)
    promoted = False
    demoted = False
    observed: str | None
    result: str
    error_message: str | None = None
    if no_visible_page:
        observed = None
        if resolution.printed_page is not None:
            result = "candidate_but_no_visible_page"
            error_message = (
                f"{book_id} PDF 页 {pdf_page} 的候选印刷页为 "
                f"{resolution.printed_page}，但人工观察为无可见页码；"
                "已记录反例，映射不会升级"
            )
        else:
            result = "no_visible_page_confirmed"
    else:
        observed = str(observed_printed_page).strip()
        if not observed:
            raise LegalAIError("--printed-page 不能为空")
        if resolution.printed_page is None:
            result = "unexpected_printed_page"
            error_message = (
                f"{book_id} PDF 页 {pdf_page} 当前没有候选映射，"
                f"但人工观察到印刷页 {observed}；已记录，等待重新分段"
            )
        elif observed.casefold() != resolution.printed_page.casefold():
            result = "mismatch"
            error_message = (
                f"核验不一致：候选印刷页 {resolution.printed_page}，"
                f"人工观察为 {observed}；已记录反例，映射不会升级"
            )
        else:
            result = "match"

    confirmation = {
        "book_id": book_id,
        "pdf_page": pdf_page,
        "observed_printed_page": observed,
        "candidate_printed_page": resolution.printed_page,
        "result": result,
        "source": "user",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    updated_log = _merge_confirmation(confirmation_log, confirmation)

    segment = resolution.segment
    if (
        error_message is not None
        and segment is not None
        and segment.status == "verified"
        and book_map is not None
    ):
        demoted_segment = replace(
            segment, status="detected", verified_samples=()
        )
        page_maps = _replace_book_segment(
            page_maps=page_maps,
            book_map=book_map,
            old_segment=segment,
            new_segment=demoted_segment,
        )
        write_page_maps(maps_path, page_maps)
        demoted = True

    _write_confirmation_log(confirmation_path, updated_log)

    if error_message is None and result == "match" and segment is not None:
        if segment.status == "detected" and book_map is not None:
            required_pages = _required_acceptance_pages(
                audit_path=audit_path,
                book_id=book_id,
                segment=segment,
            )
            matches, has_contradiction = _confirmed_matching_pages(
                confirmation_log=updated_log,
                book_map=book_map,
                segment=segment,
            )
            implementation_samples = set(segment.inspection_samples)
            implementation_gate = (
                len(implementation_samples) >= 3
                and segment.pdf_start in implementation_samples
                and segment.pdf_end in implementation_samples
                and any(
                    segment.pdf_start < item < segment.pdf_end
                    for item in implementation_samples
                )
            )
            if (
                implementation_gate
                and required_pages
                and required_pages <= matches
                and not has_contradiction
            ):
                promoted_segment = replace(
                    segment,
                    status="verified",
                    verified_samples=tuple(
                        sorted(implementation_samples | required_pages)
                    ),
                )
                page_maps = _replace_book_segment(
                    page_maps=page_maps,
                    book_map=book_map,
                    old_segment=segment,
                    new_segment=promoted_segment,
                )
                write_page_maps(maps_path, page_maps)
                promoted = True

    response = dict(confirmation)
    response["segment_promoted_to_verified"] = promoted
    response["segment_demoted_to_detected"] = demoted
    if error_message is not None:
        raise LegalAIError(error_message)
    return response

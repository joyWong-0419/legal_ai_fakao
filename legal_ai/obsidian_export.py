from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .domain import REPO_ROOT, LegalAIError
from .evidence import build_evidence_package
from .verify import verify_evidence_package


OBSIDIAN_NOTE_SCHEMA_VERSION = 1
DEFAULT_NOTE_TAG = "法学攻克系统/证据笔记"
PROTECTED_VAULT_PREFIXES = (
    (".obsidian",),
    ("book",),
    ("legal_ai", "data"),
)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
INVALID_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _yaml_string(value: object) -> str:
    """Return a dependency-free YAML-safe double-quoted scalar."""

    return (
        json.dumps(str(value), ensure_ascii=False)
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _inline_text(value: object, fallback: str = "未标注") -> str:
    text = " ".join(str(value).split())
    return text or fallback


def _list_text(value: object, fallback: str = "未标注") -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rendered = "、".join(
            _inline_text(item, "") for item in value if str(item).strip()
        )
        return rendered or fallback
    return _inline_text(value, fallback)


def _quote_lines(quote: str) -> list[str]:
    lines = quote.splitlines() or [""]
    return [f"> {line}" if line else ">" for line in lines]


def _note_pdf_link(value: object) -> str:
    """Render canonical Obsidian alias syntax outside Markdown tables."""

    link = str(value).replace("\\|", "|")
    if not link.startswith("[[") or not link.endswith("]]" ) or "|" not in link:
        raise LegalAIError("Obsidian 证据缺少有效 PDF wiki-link")
    return link


def _page_location(item: Mapping[str, Any]) -> str:
    pdf_page = item.get("pdf_page")
    if isinstance(pdf_page, bool) or not isinstance(pdf_page, int):
        raise LegalAIError("Obsidian 证据缺少有效 PDF 页码")
    status = str(item.get("printed_page_status") or "unknown")
    printed_page = item.get("printed_page")
    if status == "verified" and printed_page not in (None, ""):
        return f"印刷页 {_inline_text(printed_page)}（已核验）；PDF 页 {pdf_page}"
    if status == "detected" and printed_page not in (None, ""):
        return (
            f"PDF 页 {pdf_page}；印刷页候选 {_inline_text(printed_page)}"
            "（未核验）"
        )
    if status == "rejected":
        return f"PDF 页 {pdf_page}；候选印刷页映射已否决"
    if status == "unknown":
        return f"PDF 页 {pdf_page}；印刷页未知"
    raise LegalAIError(f"Obsidian 证据含未知印刷页状态：{status}")


def _validate_evidence(item: Mapping[str, Any], index: int) -> None:
    required = (
        "evidence_id",
        "book_id",
        "title",
        "pdf_page",
        "printed_page_status",
        "sentence_ids",
        "quote",
        "quote_sha256",
        "quote_source_field",
        "inference_level",
        "text_source",
        "text_verification_status",
        "obsidian_pdf_link",
    )
    missing = [name for name in required if name not in item]
    if missing:
        raise LegalAIError(
            f"Obsidian 第 {index} 条证据缺少字段：{', '.join(missing)}"
        )
    if item["quote_source_field"] != "sentences.normalized_text":
        raise LegalAIError(
            f"Obsidian 第 {index} 条证据不是从句子记录生成"
        )
    if item["inference_level"] not in {
        "direct_quote",
        "ocr_unverified_quote",
        "ocr_verified_quote",
    }:
        raise LegalAIError(
            f"Obsidian 第 {index} 条证据不是可核验的句子级引文"
        )
    text_source = item["text_source"]
    verification = item["text_verification_status"]
    if text_source == "ocr" and verification not in {
        "ocr_unverified",
        "ocr_verified",
    }:
        raise LegalAIError(f"Obsidian 第 {index} 条 OCR 证据缺少核验状态")
    if text_source == "embedded_text" and verification != "embedded_text":
        raise LegalAIError(f"Obsidian 第 {index} 条原生文本核验状态异常")
    sentence_ids = item["sentence_ids"]
    if (
        not isinstance(sentence_ids, list)
        or not sentence_ids
        or any(not isinstance(value, str) or not value for value in sentence_ids)
    ):
        raise LegalAIError(
            f"Obsidian 第 {index} 条证据缺少有效句子 ID"
        )
    quote = item["quote"]
    if not isinstance(quote, str) or not quote:
        raise LegalAIError(f"Obsidian 第 {index} 条证据引文为空")
    expected_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    if str(item["quote_sha256"]) != expected_hash:
        raise LegalAIError(
            f"Obsidian 第 {index} 条证据引文哈希不一致"
        )
    _page_location(item)


def _render_frontmatter(package: Mapping[str, Any]) -> list[str]:
    corpus_scope = package.get("corpus_scope")
    if not isinstance(corpus_scope, list):
        raise LegalAIError("Obsidian 证据包 corpus_scope 必须是数组")
    source_package = package.get("saved_package")
    lines = [
        "---",
        "type: legal-evidence-note",
        f"schema_version: {OBSIDIAN_NOTE_SCHEMA_VERSION}",
        "status: draft",
        f"query: {_yaml_string(package['query'])}",
        f"created: {_yaml_string(package['generated_at'])}",
        f"evidence_run_id: {_yaml_string(package['run_id'])}",
        (
            f"source_package: {_yaml_string(source_package)}"
            if source_package
            else "source_package: null"
        ),
        f"evidence_count: {len(package['evidence'])}",
    ]
    if corpus_scope:
        lines.append("corpus_scope:")
        lines.extend(f"  - {_yaml_string(book_id)}" for book_id in corpus_scope)
    else:
        lines.append("corpus_scope: []")
    lines.extend(
        [
            "current_law_verified: false",
            "tags:",
            f"  - {_yaml_string(DEFAULT_NOTE_TAG)}",
            "---",
            "",
        ]
    )
    return lines


def render_obsidian_note(package: Mapping[str, Any]) -> str:
    """Render a verified evidence package as a P6 Obsidian study note.

    This presentation function assumes the evidence layer has already checked
    the package against SQLite.  It defensively rechecks quote hashes, sentence
    IDs and provenance labels, but it does not itself open the database.

    The definition and synthesis fields intentionally remain user/agent work
    areas.  This local exporter never invents a definition and never marks
    current law as verified.
    """

    required = (
        "query",
        "generated_at",
        "run_id",
        "corpus_scope",
        "coverage_notice",
        "evidence",
        "gaps",
    )
    missing = [name for name in required if name not in package]
    if missing:
        raise LegalAIError(
            "Obsidian 证据包缺少字段：" + ", ".join(missing)
        )
    evidence = package["evidence"]
    gaps = package["gaps"]
    if not isinstance(evidence, list) or not isinstance(gaps, list):
        raise LegalAIError("Obsidian 证据包 evidence/gaps 必须是数组")
    if (
        "evidence_count" in package
        and package["evidence_count"] != len(evidence)
    ):
        raise LegalAIError("Obsidian 证据包 evidence_count 与 evidence 不一致")
    for index, item in enumerate(evidence, start=1):
        if not isinstance(item, Mapping):
            raise LegalAIError(f"Obsidian 第 {index} 条证据必须是对象")
        _validate_evidence(item, index)

    heading = _inline_text(package["query"], "未命名问题")
    lines = _render_frontmatter(package)
    lines.extend(
        [
            f"# 证据笔记：{heading}",
            "",
            "> [!warning] 学习用途与证据边界",
            f"> {_inline_text(package['coverage_notice'])}",
            "> 本笔记不是法律意见。“定义”和“我的归纳与比较”可继续整理；",
            "> “证据”和“引用索引”来自本地存证包，如需变更应重新导出。",
            "",
            "## 问题",
            "",
            *_quote_lines(str(package["query"])),
            "",
            "## 定义",
            "",
            "> [!todo] 待整理",
            "> 仅依据下列证据形成工作定义；不得把学理观点写成已核验的现行法。",
            "",
            "<!-- legal-ai:provenance:start -->",
            "## 证据",
            "",
        ]
    )
    if not evidence:
        lines.extend(
            [
                "未找到可由数据库句子记录直接支持的证据。",
                "",
            ]
        )
    for index, item in enumerate(evidence, start=1):
        label = f"E{index:03d}"
        title = _inline_text(item["title"])
        subtitle = _inline_text(item.get("subtitle"), "")
        display_title = f"{title} {subtitle}".strip()
        chapter = " > ".join(
            _inline_text(part, "")
            for part in (item.get("chapter_path") or [])
            if str(part).strip()
        ) or "未映射章节"
        source_version = "；".join(
            _inline_text(value, "")
            for value in (item.get("edition"), item.get("volume"))
            if value
        ) or "版本未标注"
        sentence_ids = [str(value) for value in item["sentence_ids"]]
        extra_provenance: list[str] = []
        if item.get("chunk_id"):
            extra_provenance.append(f"- Chunk ID：`{item['chunk_id']}`")
        if item.get("page_id"):
            extra_provenance.append(f"- 页面 ID：`{item['page_id']}`")
        if item.get("text_verification_status") == "ocr_unverified":
            source_notice = (
                "OCR 转写，尚未人工对照 PDF 页面；引用前请核对原页。"
            )
        elif item.get("text_verification_status") == "ocr_verified":
            source_notice = "OCR 转写，已人工对照 PDF 页面。"
        else:
            source_notice = "PDF 原生文字层。"
        lines.extend(
            [
                f"### {label} · 《{display_title}》",
                "",
                *_quote_lines(str(item["quote"])),
                "",
                f"- 证据 ID：`{item['evidence_id']}`",
                f"- 书籍 ID：`{item['book_id']}`",
                *extra_provenance,
                (
                    f"- 来源：{_list_text(item.get('authors'), '作者未标注')}；"
                    f"{source_version}；"
                    f"{_inline_text(item.get('source_type'), '来源类型未知')}"
                ),
                f"- 法域：{_list_text(item.get('jurisdictions'))}",
                f"- 章节：{chapter}",
                f"- 定位：{_page_location(item)}",
                "- 句子 ID：" + "、".join(f"`{value}`" for value in sentence_ids),
                f"- 引文来源字段：`{item['quote_source_field']}`",
                f"- 引文 SHA-256：`{item['quote_sha256']}`",
                f"- 推理层级：`{item['inference_level']}`",
                f"- 文本来源：{source_notice}",
                f"- 打开原书：{_note_pdf_link(item['obsidian_pdf_link'])}",
                "",
                f"^{label.casefold()}",
                "",
            ]
        )

    lines.extend(["## 引用索引", ""])
    if not evidence:
        lines.extend(["当前没有可引用证据。", ""])
    for index, item in enumerate(evidence, start=1):
        label = f"E{index:03d}"
        sentence_ids = [str(value) for value in item["sentence_ids"]]
        lines.extend(
            [
                (
                    f"- [[#^{label.casefold()}|{label}]] 《{_inline_text(item['title'])}》；"
                    f"{_page_location(item)}；句子 "
                    + "、".join(f"`{value}`" for value in sentence_ids)
                    + f"；SHA-256 `{item['quote_sha256']}`"
                ),
                f"  - 原书：{_note_pdf_link(item['obsidian_pdf_link'])}",
            ]
        )
    lines.extend(
        [
            "",
            "<!-- legal-ai:provenance:end -->",
            "",
            "## 覆盖缺口",
            "",
        ]
    )
    if gaps:
        lines.extend(f"- {_inline_text(gap)}" for gap in gaps)
    else:
        lines.append("- 当前证据包未登记其他覆盖缺口。")
    lines.extend(
        [
            "",
            "## 待核验现行法",
            "",
            "**当前状态：未核验。** `current_law_verified: false`",
            "",
            "- [ ] 确定需要核验的法域与时间点",
            "- [ ] 查询正式法源、现行条文及官方解释",
            "- [ ] 记录法源名称、条文、版本、生效日期和访问日期",
            "- [ ] 区分正式规范结论与上文学理观点",
            "",
            "## 我的归纳与比较",
            "",
            "> [!todo] 待整理",
            "> 归纳时用 E001、E002 等证据标签绑定关键判断，并明确标注“原文、归纳或比较”。",
            "",
            "## 存证信息",
            "",
            f"- 检索运行：`{package['run_id']}`",
            (
                f"- 原始证据包：`{package['saved_package']}`"
                if package.get("saved_package")
                else "- 原始证据包：未落盘"
            ),
            f"- 导出格式版本：`obsidian-note-v{OBSIDIAN_NOTE_SCHEMA_VERSION}`",
            "",
        ]
    )
    return "\n".join(lines)


def _safe_generated_filename(query: object, run_id: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(query))
    normalized = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    normalized = INVALID_FILENAME_PATTERN.sub(" ", normalized)
    normalized = "-".join(normalized.split()).strip(" .-_")
    normalized = normalized[:48].rstrip(" .-_") or "证据笔记"
    if normalized.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        normalized = f"问题-{normalized}"
    run_token = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:10]
    return f"{normalized}-{run_token}.md"


def _validate_custom_filename(filename: str) -> str:
    path = Path(filename)
    if path.is_absolute() or path.name != filename or len(path.parts) != 1:
        raise LegalAIError("Obsidian 文件名不能包含目录或绝对路径")
    if path.suffix.casefold() != ".md":
        raise LegalAIError("Obsidian 导出文件必须使用 .md 后缀")
    if INVALID_FILENAME_PATTERN.search(filename):
        raise LegalAIError("Obsidian 文件名含 Windows 不允许的字符")
    if any(
        unicodedata.category(character).startswith("C")
        for character in filename
    ):
        raise LegalAIError("Obsidian 文件名含不可见控制字符")
    if filename != filename.rstrip(" .") or not path.stem.strip(" ."):
        raise LegalAIError("Obsidian 文件名不能以空格/点结尾或使用空名称")
    if path.stem.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise LegalAIError("Obsidian 文件名是 Windows 保留名称")
    if len(filename.encode("utf-8")) > 240:
        raise LegalAIError("Obsidian 文件名过长")
    return filename


def _resolve_output_directory(
    vault_root: Path, output_dir: Path, *, create: bool = True
) -> Path:
    root = vault_root.resolve()
    if not root.is_dir():
        raise LegalAIError(f"Obsidian vault 不存在或不是目录：{root}")
    requested = output_dir if output_dir.is_absolute() else root / output_dir
    resolved = requested.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise LegalAIError("Obsidian 输出目录必须位于当前 vault 内") from exc
    folded_parts = tuple(part.casefold() for part in relative.parts)
    for protected in PROTECTED_VAULT_PREFIXES:
        if folded_parts[: len(protected)] == protected:
            raise LegalAIError(
                "Obsidian 输出目录不能位于受保护目录："
                + "/".join(protected)
            )
    if resolved.exists() and not resolved.is_dir():
        raise LegalAIError(f"Obsidian 输出路径不是目录：{resolved}")
    if not create:
        return resolved
    resolved.mkdir(parents=True, exist_ok=True)
    # Resolve once more after creation so an existing linked parent cannot
    # silently redirect the write outside the vault.
    final = resolved.resolve()
    try:
        final.relative_to(root)
    except ValueError as exc:
        raise LegalAIError("Obsidian 输出目录解析后逃逸 vault") from exc
    return final


def _write_exclusive(path: Path, markdown: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(markdown)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def write_obsidian_note(
    *,
    package: Mapping[str, Any],
    database_path: Path,
    output_dir: Path,
    vault_root: Path = REPO_ROOT,
    filename: str | None = None,
) -> Path:
    """Render a verified package and create a new note inside a vault.

    Existing notes are never replaced.  A deterministic numeric suffix is
    used if the preferred path (including a custom filename) already exists;
    the original file is left byte-for-byte unchanged.  Callers providing a
    package directly are responsible for first verifying it against SQLite.
    """

    try:
        connection = sqlite3.connect(
            Path(database_path).resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.execute("PRAGMA query_only = ON")
        issues = verify_evidence_package(connection, package)
    except sqlite3.DatabaseError as exc:
        raise LegalAIError("Obsidian 写入前无法只读校验证据包") from exc
    finally:
        if "connection" in locals():
            connection.close()
    if issues:
        codes = sorted({str(item.get("code")) for item in issues})
        raise LegalAIError(
            "Obsidian 写入前证据包校验失败：" + ", ".join(codes[:8])
        )
    markdown = render_obsidian_note(package)
    directory = _resolve_output_directory(Path(vault_root), Path(output_dir))
    preferred = (
        _validate_custom_filename(filename)
        if filename is not None
        else _safe_generated_filename(package["query"], package["run_id"])
    )
    preferred_path = Path(preferred)
    for sequence in range(1, 10_001):
        name = (
            preferred_path.name
            if sequence == 1
            else f"{preferred_path.stem}-{sequence}{preferred_path.suffix}"
        )
        candidate = directory / name
        try:
            candidate.resolve().relative_to(Path(vault_root).resolve())
        except ValueError as exc:
            raise LegalAIError("Obsidian 输出文件解析后逃逸 vault") from exc
        try:
            _write_exclusive(candidate, markdown)
        except FileExistsError:
            continue
        except OSError as exc:
            raise LegalAIError(f"无法写入 Obsidian 笔记：{candidate}") from exc
        return candidate
    raise LegalAIError("Obsidian 同名笔记过多，无法生成唯一文件名")


def export_query_to_obsidian(
    *,
    database_path: Path,
    output_dir: Path,
    query: str,
    project_root: Path | None = None,
    vault_root: Path = REPO_ROOT,
    limit: int = 8,
    book_ids: Iterable[str] | None = None,
    jurisdictions: Iterable[str] | None = None,
    subjects: Iterable[str] | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Build a verified local evidence package and export a new note."""

    vault_root = Path(vault_root).resolve()
    # Reject unsafe destinations and custom names before creating a retrieval
    # run or persisting an evidence package.
    resolved_output_dir = _resolve_output_directory(
        vault_root, Path(output_dir), create=False
    )
    if filename is not None:
        _validate_custom_filename(filename)
    resolved_project_root = (
        Path(project_root)
        if project_root is not None
        else vault_root / "legal_ai"
    )
    package = build_evidence_package(
        database_path=Path(database_path),
        project_root=resolved_project_root,
        query=query,
        limit=limit,
        book_ids=book_ids,
        jurisdictions=jurisdictions,
        subjects=subjects,
        persist=True,
    )
    if package.get("persisted") is not True or not package.get("saved_package"):
        raise LegalAIError(
            "Obsidian 导出要求先通过证据层校验并持久化原始证据包"
        )
    output_path = write_obsidian_note(
        package=package,
        database_path=Path(database_path),
        output_dir=resolved_output_dir,
        vault_root=vault_root,
        filename=filename,
    )
    return {
        "schema_version": OBSIDIAN_NOTE_SCHEMA_VERSION,
        "output_path": str(output_path.resolve()),
        "vault_relative_path": output_path.resolve().relative_to(
            vault_root
        ).as_posix(),
        "run_id": package["run_id"],
        "source_package": package.get("saved_package"),
        "evidence_count": len(package["evidence"]),
        "current_law_verified": False,
    }

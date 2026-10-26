"""法源层：append-only 的法条版本库（批次 0.5）。

设计纪律（见 CLAUDE.md §五.1 与 06-Agent体系方案 v2 §二）：

1. 条文正文、条号、生效日期一律不可修改。写错了也不改，追加一条修正版本。
2. 表上唯一允许的 UPDATE 是关闭 ``effective_to``。摄取历史单独存表，不覆盖。
3. 证据单元是「条」，不是「句」——本层与书语料的 ``sentences``/``chunks``
   粒度不同，因此使用独立数据库 ``data/index/legal_sources.sqlite``，
   不改动既有的 ``legal_corpus.sqlite`` 与其 schema 版本。

已知限制（必须在报告里显式声明，不得静默）：

- ``lawbook/*.md`` 是**合并文本**，只包含现行有效版本。逐条修正历史未获取。
  因此 v1 版本的 ``effective_from`` 记为**法律整体生效日**，而不是该条最近
  一次修正的生效日；``amendment`` 保持 NULL。
- ``text_as_of`` 由 ``config/legal_sources.json`` 人工声明，属于待核验事实。
- 行内交叉引用一律按「同法引用」（``本法第X条``）处理，不解析跨法引用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .domain import LegalAIError, REPO_ROOT


LEGAL_SOURCE_SCHEMA_VERSION = 1
LEGAL_SOURCE_INDEX_VERSION = "legal-source-v1"
LEXICON_VERSION = "cn-numerals-v1"

DEFAULT_LEGAL_SOURCES_CONFIG_PATH = (
    REPO_ROOT / "legal_ai" / "config" / "legal_sources.json"
)
DEFAULT_LEGAL_SOURCE_DATABASE_RELATIVE_PATH = (
    Path("data") / "index" / "legal_sources.sqlite"
)

ARTICLE_NO_KINDS = frozenset({"base", "supplemental"})
VERSION_STATUSES = frozenset({"current", "superseded", "repealed"})


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE legal_source_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 法源身份：这些字段一经登记不得修改
CREATE TABLE legal_sources (
    law_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    short_title TEXT,
    enacting_body TEXT,
    enacted_on TEXT,
    base_effective_from TEXT NOT NULL
);

-- 摄取历史（append-only，一次摄取一行，不覆盖）
CREATE TABLE legal_source_ingestions (
    ingestion_id TEXT PRIMARY KEY,
    law_id TEXT NOT NULL REFERENCES legal_sources(law_id),
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    text_as_of TEXT NOT NULL,
    current_version_label TEXT,
    ingested_at TEXT NOT NULL,
    lexicon_version TEXT NOT NULL,
    article_count INTEGER NOT NULL CHECK (article_count >= 0),
    event_count INTEGER NOT NULL CHECK (event_count >= 0),
    cross_ref_count INTEGER NOT NULL CHECK (cross_ref_count >= 0)
);

-- 条文版本（append-only 主体）
CREATE TABLE legal_article_versions (
    version_id TEXT PRIMARY KEY,
    law_id TEXT NOT NULL REFERENCES legal_sources(law_id),
    article_no TEXT NOT NULL,
    article_no_kind TEXT NOT NULL
        CHECK (article_no_kind IN ('base','supplemental')),
    article_no_sort INTEGER NOT NULL CHECK (article_no_sort > 0),
    supplement_index INTEGER CHECK (supplement_index IS NULL OR supplement_index > 0),
    text TEXT NOT NULL,
    chapter_path_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    text_as_of TEXT NOT NULL,
    amendment TEXT,
    char_offset_start INTEGER NOT NULL CHECK (char_offset_start >= 0),
    char_offset_end INTEGER NOT NULL CHECK (char_offset_end >= char_offset_start),
    text_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('current','superseded','repealed')),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    CHECK ((effective_to IS NULL) = (status = 'current'))
);

CREATE INDEX idx_article_lookup
    ON legal_article_versions(law_id, article_no, effective_from);

CREATE INDEX idx_article_current
    ON legal_article_versions(law_id, effective_to);

-- 修正案沿革事件（从源文件头部解析，只记录「发生过什么」，不声称改了哪条）
CREATE TABLE legal_version_events (
    event_id TEXT PRIMARY KEY,
    law_id TEXT NOT NULL REFERENCES legal_sources(law_id),
    label TEXT NOT NULL,
    adopted_on TEXT,
    body TEXT,
    note TEXT
);

-- 法条互相引用：体系图的骨架
CREATE TABLE legal_cross_refs (
    ref_id TEXT PRIMARY KEY,
    from_version_id TEXT NOT NULL
        REFERENCES legal_article_versions(version_id) ON DELETE CASCADE,
    to_law_id TEXT NOT NULL,
    to_article_no TEXT NOT NULL,
    char_offset INTEGER NOT NULL CHECK (char_offset >= 0),
    ref_text TEXT NOT NULL
);

CREATE INDEX idx_cross_ref_target
    ON legal_cross_refs(to_law_id, to_article_no);
"""


# --------------------------------------------------------------------------
# 中文数字
# --------------------------------------------------------------------------

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_NUMERAL = "[一二三四五六七八九十百千零〇两]+"


def chinese_numeral_to_int(value: str) -> int:
    """把条约编号用的中文数字转成整数。仅支持到千位（法条最大 1260）。"""

    if not value:
        raise LegalAIError("空的条约编号")
    total = 0
    pending = 0
    for char in value:
        if char in _CN_DIGITS:
            pending = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            total += (pending or 1) * unit
            pending = 0
        else:
            raise LegalAIError(f"无法识别的中文数字字符：{char}（来自 {value}）")
    total += pending
    if total <= 0:
        raise LegalAIError(f"无法解析的条约编号：{value}")
    return total


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{2,6})\s+(\S.*?)\s*$")
_ARTICLE_RE = re.compile(
    rf"^第({_CN_NUMERAL})条(?:之({_CN_NUMERAL}))?(?=[\s\u3000]|$)"
)
_REF_RE = re.compile(rf"第({_CN_NUMERAL})条(?:之({_CN_NUMERAL}))?")
_EVENT_DATE_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日\s*(.*)$")
_INFO_END_MARKER = "<!-- INFO END -->"


@dataclass(frozen=True)
class ParsedArticle:
    article_no: str
    article_no_kind: str
    article_no_sort: int
    supplement_index: int | None
    text: str
    chapter_path: tuple[str, ...]
    char_offset_start: int
    char_offset_end: int
    text_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_no": self.article_no,
            "article_no_kind": self.article_no_kind,
            "article_no_sort": self.article_no_sort,
            "supplement_index": self.supplement_index,
            "chapter_path": list(self.chapter_path),
            "char_offset_start": self.char_offset_start,
            "char_offset_end": self.char_offset_end,
            "text_sha256": self.text_sha256,
        }


@dataclass(frozen=True)
class ParsedCrossRef:
    to_article_no: str
    char_offset: int
    ref_text: str


@dataclass(frozen=True)
class ParsedVersionEvent:
    label: str
    adopted_on: str | None
    body: str | None
    note: str | None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _format_article_no(base: int, supplement: int | None) -> str:
    if supplement is None:
        return str(base)
    return f"{base}-{supplement}"


def article_sort_key(article_no: str) -> tuple[int, int]:
    """稳定排序键：``17`` -> (17, 0)，``17-1`` -> (17, 1)。"""

    if "-" in article_no:
        head, tail = article_no.split("-", 1)
        return int(head), int(tail)
    return int(article_no), 0


def parse_articles(text: str) -> tuple[ParsedArticle, ...]:
    """按行首锚定解析条文。

    绝不能裸搜 ``第.*条``：正文里的交叉引用（例如「依照本法第五百条的
    规定」）会被误判成新条文，导致条数虚高、条号错位。
    """

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    chapter_stack: list[tuple[int, str]] = []
    # 条文与标题都是边界：条文正文到下一个边界为止，标题不会被吞进条文里。
    boundaries: list[int] = []
    starts: list[tuple[int, re.Match[str], tuple[str, ...]]] = []

    for index, line in enumerate(lines):
        offset = offsets[index]
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            chapter_stack = [
                entry for entry in chapter_stack if entry[0] < level
            ]
            chapter_stack.append((level, title))
            boundaries.append(offset)
            continue
        match = _ARTICLE_RE.match(line)
        if match:
            boundaries.append(offset)
            starts.append(
                (
                    index,
                    match,
                    tuple(title for _, title in chapter_stack),
                )
            )

    boundary_set = sorted(set(boundaries))
    position = 0
    articles: list[ParsedArticle] = []
    for index, match, chapter_path in starts:
        start = offsets[index]
        while (
            position < len(boundary_set)
            and boundary_set[position] <= start
        ):
            position += 1
        end = len(text)
        if position < len(boundary_set):
            end = boundary_set[position]
        stripped = text[start:end].rstrip()
        if not stripped:
            continue
        end = start + len(stripped)

        base = chinese_numeral_to_int(match.group(1))
        supplement = (
            chinese_numeral_to_int(match.group(2))
            if match.group(2) is not None
            else None
        )
        articles.append(
            ParsedArticle(
                article_no=_format_article_no(base, supplement),
                article_no_kind=(
                    "supplemental" if supplement is not None else "base"
                ),
                article_no_sort=base,
                supplement_index=supplement,
                text=stripped,
                chapter_path=chapter_path,
                char_offset_start=start,
                char_offset_end=end,
                text_sha256=_sha256_text(stripped),
            )
        )
    return tuple(articles)


def parse_version_events(text: str) -> tuple[ParsedVersionEvent, ...]:
    """从源文件头部解析修正案沿革。

    民法典的合并文本里有 8 个 ``<!-- INFO END -->``（7 编 + 附则各自带文件头），
    所以不能假设只有一个文件头。
    """

    events: list[ParsedVersionEvent] = []
    seen: set[tuple[str, str | None]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == _INFO_END_MARKER:
            continue
        if stripped.startswith("#") or stripped.startswith("<!--"):
            continue
        match = _EVENT_DATE_RE.match(stripped)
        if not match:
            continue
        year, month, day, remainder = match.groups()
        adopted_on = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        title_match = re.search(r"《(.+?)》", remainder)
        if title_match:
            label = title_match.group(1)
            body = remainder[: title_match.start()].strip() or None
        else:
            label = remainder.strip()
            body = None
        if not label:
            continue
        key = (label, adopted_on)
        if key in seen:
            continue
        seen.add(key)
        events.append(
            ParsedVersionEvent(
                label=label,
                adopted_on=adopted_on,
                body=body,
                note=None,
            )
        )
    return tuple(events)


def extract_cross_refs(article_text: str) -> tuple[ParsedCrossRef, ...]:
    """抽取条文正文里对本法其他条的引用（不含条文自身的编号）。"""

    refs: list[ParsedCrossRef] = []
    for match in _REF_RE.finditer(article_text):
        if match.start() == 0:
            # 条文自身的「第X条」标题，不是引用
            continue
        base = chinese_numeral_to_int(match.group(1))
        supplement = (
            chinese_numeral_to_int(match.group(2))
            if match.group(2) is not None
            else None
        )
        refs.append(
            ParsedCrossRef(
                to_article_no=_format_article_no(base, supplement),
                char_offset=match.start(),
                ref_text=match.group(0),
            )
        )
    return tuple(refs)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LegalSourceConfig:
    law_id: str
    title: str
    short_title: str | None
    enacting_body: str | None
    source_file: str
    enacted_on: str | None
    base_effective_from: str
    text_as_of: str
    current_version_label: str | None
    enabled: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LegalSourceConfig":
        required = {
            "law_id",
            "title",
            "source_file",
            "base_effective_from",
            "text_as_of",
        }
        missing = sorted(field for field in required if field not in value)
        if missing:
            raise LegalAIError(f"法源配置缺少字段：{', '.join(missing)}")
        for field in ("base_effective_from", "text_as_of"):
            _require_iso_date(value[field], field)
        enacted_on = value.get("enacted_on")
        if enacted_on is not None:
            _require_iso_date(enacted_on, "enacted_on")
        return cls(
            law_id=str(value["law_id"]),
            title=str(value["title"]),
            short_title=(
                None
                if value.get("short_title") is None
                else str(value["short_title"])
            ),
            enacting_body=(
                None
                if value.get("enacting_body") is None
                else str(value["enacting_body"])
            ),
            source_file=str(value["source_file"]),
            enacted_on=None if enacted_on is None else str(enacted_on),
            base_effective_from=str(value["base_effective_from"]),
            text_as_of=str(value["text_as_of"]),
            current_version_label=(
                None
                if value.get("current_version_label") is None
                else str(value["current_version_label"])
            ),
            enabled=bool(value.get("enabled", True)),
        )


def _require_iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LegalAIError(f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise LegalAIError(f"{field} 不是合法的 ISO 日期：{value}") from exc
    return value


def load_legal_sources(
    config_path: Path,
) -> tuple[LegalSourceConfig, ...]:
    config_path = Path(config_path)
    if not config_path.exists():
        raise LegalAIError(f"法源配置不存在：{config_path}")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LegalAIError(f"法源配置不是合法 JSON：{exc}") from exc
    if not isinstance(payload, list):
        raise LegalAIError("法源配置必须是数组")
    entries = tuple(LegalSourceConfig.from_dict(item) for item in payload)
    seen: set[str] = set()
    for entry in entries:
        if entry.law_id in seen:
            raise LegalAIError(f"law_id 重复：{entry.law_id}")
        seen.add(entry.law_id)
    return entries


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------


def connect_database(
    database_path: Path, *, create: bool, read_only: bool = False
) -> sqlite3.Connection:
    """打开法源数据库。

    - ``create=True`` 是「**不存在才建**」，不是「每次重建」：重复摄取必须幂等。
    - ``read_only=True`` 以 ``mode=ro`` + ``PRAGMA query_only`` 打开，供查询与
      审计使用（与 ``evidence.py`` / ``verify.py`` 的既有约定一致）。只读连接
      既不会在目录里写 journal，也让只读 agent 在物理上无法改动法源库。
    - 数据库已存在但 schema 版本不兼容时显式报错，绝不静默读取旧库。
    """

    database_path = Path(database_path)
    if create and read_only:
        raise LegalAIError("create 与 read_only 不能同时使用")
    exists = database_path.exists()
    if not exists and (read_only or not create):
        raise LegalAIError(
            f"法源数据库不存在：{database_path}；请先运行 legal-source ingest"
        )

    resolved = database_path.resolve()
    if read_only:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro", uri=True
        )
    else:
        connection = sqlite3.connect(str(resolved))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if read_only:
        connection.execute("PRAGMA query_only = ON")
    try:
        if exists:
            _assert_compatible(connection, database_path)
        if not read_only:
            if not exists:
                connection.executescript(SCHEMA_SQL)
            _write_metadata(connection)
            connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def _write_metadata(connection: sqlite3.Connection) -> None:
    for key, value in (
        ("index_version", LEGAL_SOURCE_INDEX_VERSION),
        ("schema_version", str(LEGAL_SOURCE_SCHEMA_VERSION)),
    ):
        connection.execute(
            "INSERT OR IGNORE INTO legal_source_metadata(key, value) "
            "VALUES (?, ?)",
            (key, value),
        )


def _assert_compatible(
    connection: sqlite3.Connection, database_path: Path
) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    actual = int(row[0]) if row is not None else -1
    if actual != LEGAL_SOURCE_SCHEMA_VERSION:
        raise LegalAIError(
            f"法源数据库 schema 版本不兼容：文件为 {actual}，"
            f"本程序需要 {LEGAL_SOURCE_SCHEMA_VERSION}（{database_path}）。"
            "不静默读取旧库，请显式重建。"
        )


# --------------------------------------------------------------------------
# 摄取（append-only）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    law_id: str
    source_file: str
    source_sha256: str
    articles_total: int
    articles_inserted: int
    articles_unchanged: int
    articles_superseded: int
    version_events: int
    cross_refs: int
    database_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "law_id": self.law_id,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "articles_total": self.articles_total,
            "articles_inserted": self.articles_inserted,
            "articles_unchanged": self.articles_unchanged,
            "articles_superseded": self.articles_superseded,
            "version_events": self.version_events,
            "cross_refs": self.cross_refs,
            "database_path": self.database_path,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_source_path(config: LegalSourceConfig, project_root: Path) -> Path:
    return (Path(project_root) / config.source_file).resolve()


def ingest_legal_source(
    *,
    database_path: Path,
    config: LegalSourceConfig,
    project_root: Path = REPO_ROOT,
) -> IngestResult:
    """把一个法源文件的当前合并文本摄进 append-only 版本库。

    重复摄取同一份文件是幂等的：条文的 ``text_sha256`` 相同则不产生新版本。
    条文内容发生变化时，**关闭旧版本并追加新版本**，绝不覆盖正文。
    """

    source_path = resolve_source_path(config, project_root)
    if not source_path.exists():
        raise LegalAIError(f"法源源文件不存在：{source_path}")
    text = source_path.read_text(encoding="utf-8")
    source_sha256 = _sha256_file(source_path)

    articles = parse_articles(text)
    if not articles:
        raise LegalAIError(f"未能从 {config.source_file} 解析出任何条文")
    events = parse_version_events(text)

    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database_path, create=True)
    try:
        with connection:
            _upsert_legal_source(connection, config)
            inserted = 0
            unchanged = 0
            superseded = 0
            cross_ref_count = 0
            for article in articles:
                outcome, refs = _append_article_version(
                    connection, config, article
                )
                cross_ref_count += refs
                if outcome == "inserted":
                    inserted += 1
                elif outcome == "unchanged":
                    unchanged += 1
                else:
                    superseded += 1
            _record_version_events(connection, config, events)
            ingestion_id = _ingestion_id(config.law_id, source_sha256)
            connection.execute(
                """
                INSERT OR IGNORE INTO legal_source_ingestions(
                    ingestion_id, law_id, source_file, source_sha256,
                    text_as_of, current_version_label, ingested_at,
                    lexicon_version, article_count, event_count,
                    cross_ref_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingestion_id,
                    config.law_id,
                    config.source_file,
                    source_sha256,
                    config.text_as_of,
                    config.current_version_label,
                    _now_iso(),
                    LEXICON_VERSION,
                    len(articles),
                    len(events),
                    cross_ref_count,
                ),
            )
    finally:
        connection.close()

    return IngestResult(
        law_id=config.law_id,
        source_file=config.source_file,
        source_sha256=source_sha256,
        articles_total=len(articles),
        articles_inserted=inserted,
        articles_unchanged=unchanged,
        articles_superseded=superseded,
        version_events=len(events),
        cross_refs=cross_ref_count,
        database_path=str(database_path),
    )


def _ingestion_id(law_id: str, source_sha256: str) -> str:
    return hashlib.sha256(
        f"{law_id}:{source_sha256}".encode("utf-8")
    ).hexdigest()[:24]


def _upsert_legal_source(
    connection: sqlite3.Connection, config: LegalSourceConfig
) -> None:
    """登记法源身份。已存在时校验身份字段一致，不一致即报错，不静默改写。"""

    row = connection.execute(
        "SELECT * FROM legal_sources WHERE law_id = ?", (config.law_id,)
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO legal_sources(
                law_id, title, short_title, enacting_body,
                enacted_on, base_effective_from
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                config.law_id,
                config.title,
                config.short_title,
                config.enacting_body,
                config.enacted_on,
                config.base_effective_from,
            ),
        )
        return
    conflicts = [
        field
        for field in ("title", "enacted_on", "base_effective_from")
        if row[field] != getattr(config, field)
    ]
    if conflicts:
        raise LegalAIError(
            f"{config.law_id}：法源身份字段不允许修改（"
            + "、".join(conflicts)
            + "）。如需更正，请显式迁移法源数据库，不要静默改写。"
        )


def _append_article_version(
    connection: sqlite3.Connection,
    config: LegalSourceConfig,
    article: ParsedArticle,
) -> tuple[str, int]:
    existing = connection.execute(
        """
        SELECT version_id, text_sha256, effective_from
        FROM legal_article_versions
        WHERE law_id = ? AND article_no = ? AND effective_to IS NULL
        """,
        (config.law_id, article.article_no),
    ).fetchone()

    if existing is not None and existing["text_sha256"] == article.text_sha256:
        # 幂等：同一份文本重复摄取不产生新版本，也不重复写交叉引用
        return "unchanged", 0

    effective_from = config.base_effective_from
    version_number = 1
    if existing is not None:
        # append-only：唯一的 UPDATE —— 关闭旧版本的生效区间
        if config.text_as_of <= existing["effective_from"]:
            raise LegalAIError(
                f"{config.law_id} 第 {article.article_no} 条内容发生变化，"
                f"但 text_as_of={config.text_as_of} 不晚于旧版本生效日 "
                f"{existing['effective_from']}；请先核实并更新 "
                "config/legal_sources.json 中的 text_as_of，再重新摄取。"
            )
        connection.execute(
            """
            UPDATE legal_article_versions
            SET effective_to = ?, status = 'superseded'
            WHERE version_id = ?
            """,
            (config.text_as_of, existing["version_id"]),
        )
        effective_from = config.text_as_of
        version_number = _next_version_number(
            connection, config.law_id, article.article_no
        )
        outcome = "superseded"
    else:
        outcome = "inserted"

    version_id = (
        f"{config.law_id}:{article.article_no}:v{version_number}"
    )
    connection.execute(
        """
        INSERT INTO legal_article_versions(
            version_id, law_id, article_no, article_no_kind,
            article_no_sort, supplement_index, text, chapter_path_json,
            effective_from, effective_to, text_as_of, amendment,
            char_offset_start, char_offset_end, text_sha256, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, 'current')
        """,
        (
            version_id,
            config.law_id,
            article.article_no,
            article.article_no_kind,
            article.article_no_sort,
            article.supplement_index,
            article.text,
            json.dumps(list(article.chapter_path), ensure_ascii=False),
            effective_from,
            config.text_as_of,
            article.char_offset_start,
            article.char_offset_end,
            article.text_sha256,
        ),
    )
    ref_count = _insert_cross_refs(connection, version_id, article)
    return outcome, ref_count


def _next_version_number(
    connection: sqlite3.Connection, law_id: str, article_no: str
) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) FROM legal_article_versions
        WHERE law_id = ? AND article_no = ?
        """,
        (law_id, article_no),
    ).fetchone()
    return int(row[0]) + 1


def _insert_cross_refs(
    connection: sqlite3.Connection,
    version_id: str,
    article: ParsedArticle,
) -> int:
    refs = extract_cross_refs(article.text)
    inserted = 0
    for ref in refs:
        ref_id = hashlib.sha256(
            f"{version_id}:{ref.char_offset}:{ref.ref_text}".encode("utf-8")
        ).hexdigest()[:24]
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO legal_cross_refs(
                ref_id, from_version_id, to_law_id, to_article_no,
                char_offset, ref_text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ref_id,
                version_id,
                version_id.split(":", 1)[0],
                ref.to_article_no,
                ref.char_offset,
                ref.ref_text,
            ),
        )
        inserted += cursor.rowcount if cursor.rowcount > 0 else 0
    return inserted


def _record_version_events(
    connection: sqlite3.Connection,
    config: LegalSourceConfig,
    events: Iterable[ParsedVersionEvent],
) -> None:
    for event in events:
        event_id = hashlib.sha256(
            f"{config.law_id}:{event.label}:{event.adopted_on}".encode("utf-8")
        ).hexdigest()[:24]
        connection.execute(
            """
            INSERT OR IGNORE INTO legal_version_events(
                event_id, law_id, label, adopted_on, body, note
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                config.law_id,
                event.label,
                event.adopted_on,
                event.body,
                event.note,
            ),
        )


# --------------------------------------------------------------------------
# 查询（时间旅行）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArticleView:
    version_id: str
    law_id: str
    law_title: str
    article_no: str
    text: str
    chapter_path: tuple[str, ...]
    effective_from: str
    effective_to: str | None
    text_as_of: str
    status: str
    text_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "law_id": self.law_id,
            "law_title": self.law_title,
            "article_no": self.article_no,
            "text": self.text,
            "chapter_path": list(self.chapter_path),
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "text_as_of": self.text_as_of,
            "status": self.status,
            "text_sha256": self.text_sha256,
        }


def query_article(
    *,
    database_path: Path,
    law_id: str,
    article_no: str,
    as_of: str | None = None,
) -> ArticleView | None:
    """按 ``as_of`` 时点取条文版本。这就是「时间旅行查询」。"""

    if as_of is None:
        as_of = date.today().isoformat()
    else:
        _require_iso_date(as_of, "as_of")
    article_no = article_no.strip()
    if not article_no:
        raise LegalAIError("条号不能为空")

    connection = connect_database(Path(database_path), create=False, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT v.*, s.title AS law_title
            FROM legal_article_versions v
            JOIN legal_sources s ON s.law_id = v.law_id
            WHERE v.law_id = ? AND v.article_no = ?
              AND v.effective_from <= ?
              AND (v.effective_to IS NULL OR v.effective_to > ?)
            ORDER BY v.effective_from DESC
            LIMIT 1
            """,
            (law_id, article_no, as_of, as_of),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return ArticleView(
        version_id=row["version_id"],
        law_id=row["law_id"],
        law_title=row["law_title"],
        article_no=row["article_no"],
        text=row["text"],
        chapter_path=tuple(json.loads(row["chapter_path_json"])),
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        text_as_of=row["text_as_of"],
        status=row["status"],
        text_sha256=row["text_sha256"],
    )


def cross_refs_for(
    *, database_path: Path, law_id: str, article_no: str
) -> dict[str, list[dict[str, Any]]]:
    connection = connect_database(Path(database_path), create=False, read_only=True)
    try:
        outgoing = connection.execute(
            """
            SELECT r.to_article_no, r.ref_text, r.char_offset
            FROM legal_cross_refs r
            JOIN legal_article_versions v ON v.version_id = r.from_version_id
            WHERE v.law_id = ? AND v.article_no = ? AND v.effective_to IS NULL
            ORDER BY r.char_offset
            """,
            (law_id, article_no),
        ).fetchall()
        incoming = connection.execute(
            """
            SELECT v.article_no AS from_article_no, r.ref_text
            FROM legal_cross_refs r
            JOIN legal_article_versions v ON v.version_id = r.from_version_id
            WHERE r.to_law_id = ? AND r.to_article_no = ?
              AND v.effective_to IS NULL
            ORDER BY v.article_no_sort, v.article_no
            """,
            (law_id, article_no),
        ).fetchall()
    finally:
        connection.close()
    return {
        "outgoing": [dict(row) for row in outgoing],
        "incoming": [dict(row) for row in incoming],
    }


def render_article_text(
    view: ArticleView, refs: Mapping[str, list[dict[str, Any]]] | None = None
) -> str:
    lines: list[str] = []
    lines.append(f"《{view.law_title}》第 {view.article_no} 条")
    lines.append(f"version_id : {view.version_id}")
    effective = view.effective_from
    if view.effective_to is None:
        effective += " 起（现行）"
    else:
        effective += f" 至 {view.effective_to}"
    lines.append(f"生效区间   : {effective}")
    lines.append(f"文本时点   : {view.text_as_of}｜状态：{view.status}")
    if view.chapter_path:
        lines.append("章节       : " + " > ".join(view.chapter_path))
    lines.append(f"text_sha256: {view.text_sha256}")
    lines.append("")
    lines.append(view.text)
    if refs is not None:
        outgoing = refs.get("outgoing", [])
        incoming = refs.get("incoming", [])
        lines.append("")
        lines.append(
            f"引出引用（{len(outgoing)}）："
            + (
                "、".join(f"第 {item['to_article_no']} 条" for item in outgoing)
                or "无"
            )
        )
        lines.append(
            f"被引用（{len(incoming)}）："
            + (
                "、".join(
                    f"第 {item['from_article_no']} 条" for item in incoming
                )
                or "无"
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


def build_legal_source_audit(database_path: Path) -> dict[str, Any]:
    database_path = Path(database_path)
    connection = connect_database(database_path, create=False)
    try:
        laws: list[dict[str, Any]] = []
        for law_row in connection.execute(
            "SELECT * FROM legal_sources ORDER BY law_id"
        ).fetchall():
            law_id = law_row["law_id"]
            current = connection.execute(
                """
                SELECT article_no, article_no_kind, article_no_sort,
                       supplement_index, text_sha256
                FROM legal_article_versions
                WHERE law_id = ? AND effective_to IS NULL
                """,
                (law_id,),
            ).fetchall()
            bases = sorted(
                row["article_no_sort"]
                for row in current
                if row["article_no_kind"] == "base"
            )
            missing: list[int] = []
            if bases:
                present = set(bases)
                missing = [
                    number
                    for number in range(1, max(bases) + 1)
                    if number not in present
                ]
            duplicates = sorted(
                {
                    row["article_no"]
                    for row in current
                    if sum(
                        1
                        for other in current
                        if other["article_no"] == row["article_no"]
                    )
                    > 1
                }
            )
            cross_refs = connection.execute(
                """
                SELECT COUNT(*) FROM legal_cross_refs r
                JOIN legal_article_versions v ON v.version_id = r.from_version_id
                WHERE v.law_id = ? AND v.effective_to IS NULL
                """,
                (law_id,),
            ).fetchone()[0]
            dangling = connection.execute(
                """
                SELECT COUNT(*) FROM legal_cross_refs r
                JOIN legal_article_versions v ON v.version_id = r.from_version_id
                WHERE v.law_id = ? AND v.effective_to IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM legal_article_versions t
                      WHERE t.law_id = r.to_law_id
                        AND t.article_no = r.to_article_no
                        AND t.effective_to IS NULL
                  )
                """,
                (law_id,),
            ).fetchone()[0]
            top_targets = connection.execute(
                """
                SELECT r.to_article_no, COUNT(*) AS hits
                FROM legal_cross_refs r
                JOIN legal_article_versions v ON v.version_id = r.from_version_id
                WHERE v.law_id = ? AND v.effective_to IS NULL
                GROUP BY r.to_article_no
                ORDER BY hits DESC, CAST(r.to_article_no AS INTEGER)
                LIMIT 5
                """,
                (law_id,),
            ).fetchall()
            superseded = connection.execute(
                """
                SELECT COUNT(*) FROM legal_article_versions
                WHERE law_id = ? AND effective_to IS NOT NULL
                """,
                (law_id,),
            ).fetchone()[0]
            ingestion = connection.execute(
                """
                SELECT * FROM legal_source_ingestions
                WHERE law_id = ?
                ORDER BY ingested_at DESC LIMIT 1
                """,
                (law_id,),
            ).fetchone()
            events = connection.execute(
                """
                SELECT COUNT(*) FROM legal_version_events WHERE law_id = ?
                """,
                (law_id,),
            ).fetchone()[0]

            laws.append(
                {
                    "law_id": law_id,
                    "title": law_row["title"],
                    "short_title": law_row["short_title"],
                    "base_effective_from": law_row["base_effective_from"],
                    "current_articles": len(current),
                    "base_articles": len(bases),
                    "supplemental_articles": len(current) - len(bases),
                    "max_article_no": max(bases) if bases else 0,
                    "missing_article_numbers": missing,
                    "duplicate_article_numbers": duplicates,
                    "superseded_versions": superseded,
                    "cross_refs": int(cross_refs),
                    "dangling_cross_refs": int(dangling),
                    "top_referenced_articles": [
                        {
                            "article_no": row["to_article_no"],
                            "hits": row["hits"],
                        }
                        for row in top_targets
                    ],
                    "version_events": int(events),
                    "source_file": (
                        ingestion["source_file"] if ingestion else None
                    ),
                    "source_sha256": (
                        ingestion["source_sha256"] if ingestion else None
                    ),
                    "text_as_of": (
                        ingestion["text_as_of"] if ingestion else None
                    ),
                    "current_version_label": (
                        ingestion["current_version_label"]
                        if ingestion
                        else None
                    ),
                }
            )
        counts = {
            "laws": len(laws),
            "article_versions": connection.execute(
                "SELECT COUNT(*) FROM legal_article_versions"
            ).fetchone()[0],
            "current_articles": connection.execute(
                "SELECT COUNT(*) FROM legal_article_versions "
                "WHERE effective_to IS NULL"
            ).fetchone()[0],
            "cross_refs": connection.execute(
                "SELECT COUNT(*) FROM legal_cross_refs"
            ).fetchone()[0],
            "version_events": connection.execute(
                "SELECT COUNT(*) FROM legal_version_events"
            ).fetchone()[0],
            "ingestions": connection.execute(
                "SELECT COUNT(*) FROM legal_source_ingestions"
            ).fetchone()[0],
        }
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
    finally:
        connection.close()

    return {
        "schema_version": LEGAL_SOURCE_SCHEMA_VERSION,
        "index_version": LEGAL_SOURCE_INDEX_VERSION,
        "lexicon_version": LEXICON_VERSION,
        "generated_at": _now_iso(),
        "database": str(database_path),
        "counts": counts,
        "user_version": int(user_version),
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "laws": laws,
        "caveats": [
            "源文件为合并文本，仅含现行有效版本；逐条修正历史未获取。"
            "v1 版本的 effective_from 记为法律整体生效日，不是该条最近一次修正的生效日。",
            "text_as_of 与 base_effective_from 由 config/legal_sources.json "
            "人工声明，属于待核验事实。",
            "行内交叉引用一律按同法引用（本法第X条）处理，不解析跨法引用。",
        ],
    }


def render_legal_source_audit_markdown(audit: Mapping[str, Any]) -> str:
    counts = audit["counts"]
    lines: list[str] = []
    lines.append("# 法源层审计")
    lines.append("")
    lines.append(f"> 生成：{audit['generated_at']}")
    lines.append(
        f"> 数据库：`{audit['database']}`"
        f"｜schema {audit['schema_version']}"
        f"｜{audit['index_version']}"
    )
    lines.append(
        f"> 总数：{counts['laws']} 部法源、{counts['current_articles']} 条现行、"
        f"{counts['cross_refs']} 条交叉引用、{counts['version_events']} 条沿革事件"
    )
    lines.append("")
    lines.append(
        f"- `integrity_check` = {audit['integrity_check']}"
        f"｜外键违规 {audit['foreign_key_violations']}"
        f"｜`user_version` = {audit['user_version']}"
    )
    lines.append("")
    for law in audit["laws"]:
        lines.append(f"## {law['title']}（`{law['law_id']}`）")
        lines.append("")
        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 现行条文 | {law['current_articles']} |")
        lines.append(
            f"| 其中「之一」型 | {law['supplemental_articles']} |"
        )
        lines.append(f"| 最大条号 | {law['max_article_no']} |")
        lines.append(f"| 已废弃版本 | {law['superseded_versions']} |")
        lines.append(f"| 交叉引用 | {law['cross_refs']} |")
        lines.append(f"| 悬空引用 | {law['dangling_cross_refs']} |")
        lines.append(f"| 沿革事件 | {law['version_events']} |")
        lines.append(f"| 整体生效日 | {law['base_effective_from']} |")
        lines.append(f"| 文本时点 | {law['text_as_of']} |")
        lines.append(f"| 源文件 | `{law['source_file']}` |")
        lines.append(f"| 源 SHA-256 | `{law['source_sha256']}` |")
        lines.append("")
        if law["missing_article_numbers"]:
            missing = "、".join(
                str(number) for number in law["missing_article_numbers"]
            )
            lines.append(f"**缺失条号**：{missing}")
            lines.append("")
        else:
            lines.append("**缺失条号**：无（1 至最大条号连续）")
            lines.append("")
        if law["duplicate_article_numbers"]:
            lines.append(
                "**重复条号**："
                + "、".join(law["duplicate_article_numbers"])
            )
            lines.append("")
        if law["top_referenced_articles"]:
            lines.append("被引用最多的条文：")
            lines.append("")
            for item in law["top_referenced_articles"]:
                lines.append(
                    f"- 第 {item['article_no']} 条（{item['hits']} 次）"
                )
            lines.append("")
    lines.append("## 已知限制")
    lines.append("")
    for caveat in audit["caveats"]:
        lines.append(f"- {caveat}")
    lines.append("")
    return "\n".join(lines)


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def write_legal_source_audit(
    audit: Mapping[str, Any], json_path: Path, markdown_path: Path
) -> None:
    _atomic_write_text(
        Path(json_path),
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write_text(
        Path(markdown_path), render_legal_source_audit_markdown(audit)
    )

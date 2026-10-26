from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "legal_ai" / "config" / "books.json"
DEFAULT_PAGE_MAPS_PATH = (
    REPO_ROOT / "legal_ai" / "config" / "page_maps.json"
)

TEXT_LAYER_POLICIES = frozenset({"embedded_only", "auto", "ignore"})
OCR_POLICIES = frozenset({"off", "low_quality_pages", "all_pages"})
TEXT_SOURCES = frozenset({"embedded_text", "ocr", "hybrid", "empty"})
TEXT_VERIFICATION_STATUSES = frozenset(
    {"embedded_text", "ocr_unverified", "ocr_verified"}
)


class LegalAIError(Exception):
    """A user-facing, expected project error."""


@dataclass(frozen=True)
class BookConfig:
    book_id: str
    title: str
    subtitle: str | None
    authors: tuple[str, ...]
    translators: tuple[str, ...]
    edition: str | None
    volume: str | None
    language: str
    jurisdictions: tuple[str, ...]
    subjects: tuple[str, ...]
    source_type: str
    authority_tier: str
    file_path: str
    expected_pdf_pages: int
    text_layer_policy: str
    ocr_policy: str
    ocr_profile: str | None
    # Kept only to read the original P0-P6 books.json without changing its
    # historical meaning.  New v2 entries should use the three policy fields.
    extract_mode: str | None
    enabled: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BookConfig":
        required = {
            "book_id",
            "title",
            "authors",
            "translators",
            "language",
            "jurisdictions",
            "subjects",
            "source_type",
            "authority_tier",
            "file_path",
            "expected_pdf_pages",
            "enabled",
        }
        missing = sorted(required - value.keys())
        if missing:
            raise LegalAIError(
                f"书目配置缺少字段：{', '.join(missing)}"
            )
        if not isinstance(value["expected_pdf_pages"], int) or value[
            "expected_pdf_pages"
        ] <= 0:
            raise LegalAIError("expected_pdf_pages 必须是正整数")
        for field in ("authors", "translators", "jurisdictions", "subjects"):
            if not isinstance(value[field], list):
                raise LegalAIError(f"{field} 必须是数组")
        legacy_extract_mode = value.get("extract_mode")
        has_v2_policy = any(
            field in value
            for field in ("text_layer_policy", "ocr_policy", "ocr_profile")
        )
        if has_v2_policy:
            missing_policy = [
                field
                for field in ("text_layer_policy", "ocr_policy", "ocr_profile")
                if field not in value
            ]
            if missing_policy:
                raise LegalAIError(
                    "v2 书目配置必须同时声明 text_layer_policy、"
                    "ocr_policy 和 ocr_profile；缺少："
                    + ", ".join(missing_policy)
                )
            text_layer_policy = str(value["text_layer_policy"])
            ocr_policy = str(value["ocr_policy"])
            ocr_profile_value = value["ocr_profile"]
            ocr_profile = (
                None if ocr_profile_value is None else str(ocr_profile_value)
            )
            if legacy_extract_mode is not None and legacy_extract_mode != "embedded_text":
                raise LegalAIError(
                    f"{value['book_id']}：不支持的历史 extract_mode="
                    f"{legacy_extract_mode}"
                )
            if legacy_extract_mode == "embedded_text" and (
                text_layer_policy != "embedded_only" or ocr_policy != "off"
            ):
                raise LegalAIError(
                    f"{value['book_id']}：历史 extract_mode=embedded_text 与 "
                    "v2 OCR 策略冲突；请移除历史字段或保持 embedded_only/off"
                )
        else:
            if legacy_extract_mode != "embedded_text":
                raise LegalAIError(
                    f"{value['book_id']}：需要 v2 策略字段；历史兼容仅支持 "
                    "extract_mode=embedded_text"
                )
            text_layer_policy = "embedded_only"
            ocr_policy = "off"
            ocr_profile = None

        if text_layer_policy not in TEXT_LAYER_POLICIES:
            raise LegalAIError(
                "text_layer_policy 必须是 "
                + "/".join(sorted(TEXT_LAYER_POLICIES))
            )
        if ocr_policy not in OCR_POLICIES:
            raise LegalAIError(
                "ocr_policy 必须是 " + "/".join(sorted(OCR_POLICIES))
            )
        if ocr_policy == "off" and ocr_profile is not None:
            raise LegalAIError("ocr_policy=off 时 ocr_profile 必须为 null")
        if ocr_policy != "off" and not ocr_profile:
            raise LegalAIError("启用 OCR 时必须声明非空 ocr_profile")
        if text_layer_policy == "ignore" and ocr_policy == "off":
            raise LegalAIError("text_layer_policy=ignore 不能与 ocr_policy=off 并用")
        return cls(
            book_id=str(value["book_id"]),
            title=str(value["title"]),
            subtitle=value.get("subtitle"),
            authors=tuple(str(item) for item in value["authors"]),
            translators=tuple(str(item) for item in value["translators"]),
            edition=value.get("edition"),
            volume=value.get("volume"),
            language=str(value["language"]),
            jurisdictions=tuple(str(item) for item in value["jurisdictions"]),
            subjects=tuple(str(item) for item in value["subjects"]),
            source_type=str(value["source_type"]),
            authority_tier=str(value["authority_tier"]),
            file_path=str(value["file_path"]),
            expected_pdf_pages=value["expected_pdf_pages"],
            text_layer_policy=text_layer_policy,
            ocr_policy=ocr_policy,
            ocr_profile=ocr_profile,
            extract_mode=(
                str(legacy_extract_mode)
                if legacy_extract_mode is not None
                else None
            ),
            enabled=bool(value["enabled"]),
        )


@dataclass(frozen=True)
class InventoryRecord:
    book: BookConfig
    absolute_path: Path
    sha256: str
    actual_pdf_pages: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book.book_id,
            "title": self.book.title,
            "file_path": self.book.file_path,
            "expected_pdf_pages": self.book.expected_pdf_pages,
            "actual_pdf_pages": self.actual_pdf_pages,
            "sha256": self.sha256,
            "jurisdictions": list(self.book.jurisdictions),
            "subjects": list(self.book.subjects),
            "source_type": self.book.source_type,
            "authority_tier": self.book.authority_tier,
            "extract_mode": self.book.extract_mode,
            "text_layer_policy": self.book.text_layer_policy,
            "ocr_policy": self.book.ocr_policy,
            "ocr_profile": self.book.ocr_profile,
        }

from __future__ import annotations

import hashlib
from dataclasses import dataclass


SEGMENTATION_VERSION = "seg-v1"

TERMINAL_PUNCTUATION = {"。", "！", "？", "；"}
COLON_BOUNDARY_PREFIXES = set("第0123456789一二三四五六七八九十甲乙丙丁戊己庚辛壬癸（(")
OPEN_TO_CLOSE = {
    "(": ")",
    "（": "）",
    "[": "]",
    "【": "】",
    "《": "》",
    "〈": "〉",
    "「": "」",
    "『": "』",
    "“": "”",
    "‘": "’",
}
CLOSERS = set(OPEN_TO_CLOSE.values())
TRAILING_MARKS = CLOSERS | {"”", "’", "」", "』", "》", "〉"}
FOOTNOTE_MARKS = set("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")
QUOTE_CLOSERS = {"”", "’", "」", "』"}


@dataclass(frozen=True)
class SentenceSpan:
    text: str
    start_offset: int
    end_offset: int


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_sentences(normalized_text: str) -> list[SentenceSpan]:
    spans: list[SentenceSpan] = []
    stack: list[str] = []
    start = 0
    pending_boundary = False
    index = 0

    while index < len(normalized_text):
        character = normalized_text[index]

        if _is_blank_line_at(normalized_text, index) and not stack:
            _append_trimmed(spans, normalized_text, start, index)
            index = _consume_newlines(normalized_text, index)
            start = index
            pending_boundary = False
            continue

        if character in OPEN_TO_CLOSE:
            stack.append(OPEN_TO_CLOSE[character])
        elif stack and character == stack[-1]:
            stack.pop()
        elif character in CLOSERS and stack:
            stack.pop()

        if character in TERMINAL_PUNCTUATION or _is_colon_boundary(
            normalized_text, index
        ):
            pending_boundary = not stack or stack[-1] in QUOTE_CLOSERS

        if pending_boundary and not stack:
            boundary = _consume_trailing_marks(normalized_text, index + 1)
            _append_trimmed(spans, normalized_text, start, boundary)
            index = boundary
            start = index
            pending_boundary = False
            continue

        index += 1

    _append_trimmed(spans, normalized_text, start, len(normalized_text))
    return spans


def build_sentence_records(
    *,
    book_id: str,
    page_id: str,
    pdf_page: int,
    normalized_text: str,
    raw_text: str,
    raw_slice,
    chapter_path: list[str],
    normalization_version: str,
) -> list[dict]:
    records = []
    for sequence, span in enumerate(split_sentences(normalized_text), start=1):
        sentence_id = f"{page_id}:s{sequence:03d}"
        records.append(
            {
                "sentence_id": sentence_id,
                "page_id": page_id,
                "book_id": book_id,
                "pdf_page": pdf_page,
                "seq": sequence,
                "raw_text": raw_slice(
                    raw_text, span.start_offset, span.end_offset
                ),
                "normalized_text": span.text,
                "start_offset": span.start_offset,
                "end_offset": span.end_offset,
                "text_sha256": text_sha256(span.text),
                "chapter_path": chapter_path,
                "normalization_version": normalization_version,
                "segmentation_version": SEGMENTATION_VERSION,
            }
        )
    return records


def build_chunk_records(
    *,
    book_id: str,
    page_id: str,
    pdf_page: int,
    sentences: list[dict],
    chapter_path: list[str],
    min_chars: int = 300,
    max_chars: int = 900,
) -> list[dict]:
    chunks: list[dict] = []
    current: list[dict] = []

    def current_length(items: list[dict]) -> int:
        return len("".join(item["normalized_text"] for item in items))

    def flush() -> None:
        if not current:
            return
        sequence = len(chunks) + 1
        text = "\n".join(item["normalized_text"] for item in current)
        chunks.append(
            {
                "chunk_id": f"{page_id}:c{sequence:03d}",
                "page_id": page_id,
                "book_id": book_id,
                "pdf_page": pdf_page,
                "seq": sequence,
                "first_sentence_id": current[0]["sentence_id"],
                "last_sentence_id": current[-1]["sentence_id"],
                "sentence_ids": [
                    item["sentence_id"] for item in current
                ],
                "text": text,
                "char_count": len(text),
                "chapter_path": chapter_path,
                "segmentation_version": SEGMENTATION_VERSION,
            }
        )
        current.clear()

    for sentence in sentences:
        sentence_length = len(sentence["normalized_text"])
        if (
            current
            and current_length(current) >= min_chars
            and current_length(current) + sentence_length > max_chars
        ):
            flush()
        current.append(sentence)
        if sentence_length >= max_chars:
            flush()
    flush()
    return chunks


def _append_trimmed(
    spans: list[SentenceSpan], text: str, start: int, end: int
) -> None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        spans.append(SentenceSpan(text[start:end], start, end))


def _consume_trailing_marks(text: str, index: int) -> int:
    while index < len(text):
        character = text[index]
        if character in TRAILING_MARKS or character in FOOTNOTE_MARKS:
            index += 1
            continue
        if character == "[":
            closing = text.find("]", index + 1, index + 8)
            if closing != -1 and text[index + 1 : closing].isdigit():
                index = closing + 1
                continue
        break
    return index


def _is_blank_line_at(text: str, index: int) -> bool:
    if text[index] != "\n":
        return False
    return index + 1 < len(text) and text[index + 1] == "\n"


def _consume_newlines(text: str, index: int) -> int:
    while index < len(text) and text[index] == "\n":
        index += 1
    return index


def _is_colon_boundary(text: str, index: int) -> bool:
    if text[index] != "：":
        return False
    next_index = index + 1
    if next_index >= len(text):
        return True
    next_character = text[next_index]
    return next_character.isspace() or next_character in COLON_BOUNDARY_PREFIXES

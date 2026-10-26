from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


NORMALIZATION_VERSION = "norm-v1"
NORMALIZATION_RULES = (
    "crlf_to_lf",
    "nbsp_and_ideographic_space_to_ascii_space",
    "collapse_horizontal_whitespace",
    "strip_space_around_newline",
    "collapse_three_or_more_newlines_to_two",
    "strip_outer_whitespace",
)


@dataclass(frozen=True)
class NormalizedPage:
    text: str
    raw_offsets: tuple[int, ...]
    version: str = NORMALIZATION_VERSION
    rules: tuple[str, ...] = NORMALIZATION_RULES

    def raw_slice_for_normalized_span(
        self, raw_text: str, start_offset: int, end_offset: int
    ) -> str:
        if start_offset < 0 or end_offset < start_offset:
            raise ValueError("invalid normalized offsets")
        if start_offset == end_offset:
            return ""
        if end_offset > len(self.raw_offsets):
            raise ValueError("normalized offsets outside page text")
        raw_start = self.raw_offsets[start_offset]
        raw_end = self.raw_offsets[end_offset - 1] + 1
        return raw_text[raw_start:raw_end]


def normalize_page_text(raw_text: str | None) -> NormalizedPage:
    if raw_text is None:
        return NormalizedPage("", ())

    chars: list[str] = []
    offsets: list[int] = []
    index = 0
    previous_space = False
    previous_newline = False
    newline_run = 0
    pending_space_offset: int | None = None

    def append(character: str, raw_offset: int) -> None:
        nonlocal previous_space, previous_newline, newline_run
        chars.append(character)
        offsets.append(raw_offset)
        previous_space = character == " "
        previous_newline = character == "\n"
        newline_run = newline_run + 1 if character == "\n" else 0

    while index < len(raw_text):
        character = raw_text[index]
        raw_offset = index

        if character == "\r":
            if index + 1 < len(raw_text) and raw_text[index + 1] == "\n":
                index += 1
            character = "\n"
        elif character in {"\u00a0", "\u3000"}:
            character = " "
        elif character in {"\t", "\f", "\v"}:
            character = " "

        if character == " ":
            if previous_space or previous_newline:
                index += 1
                continue
            pending_space_offset = raw_offset
            index += 1
            continue

        if character == "\n":
            if previous_space and chars:
                chars.pop()
                offsets.pop()
            if newline_run >= 2:
                index += 1
                pending_space_offset = None
                continue
            append("\n", raw_offset)
            pending_space_offset = None
            index += 1
            continue

        if pending_space_offset is not None and not previous_newline:
            append(" ", pending_space_offset)
        pending_space_offset = None
        append(character, raw_offset)
        index += 1

    if pending_space_offset is not None and not previous_newline:
        append(" ", pending_space_offset)

    text, mapped_offsets = _strip_outer_whitespace(chars, offsets)
    return NormalizedPage("".join(text), tuple(mapped_offsets))


def _strip_outer_whitespace(
    chars: Iterable[str], offsets: Iterable[int]
) -> tuple[list[str], list[int]]:
    paired = list(zip(chars, offsets))
    start = 0
    end = len(paired)
    while start < end and paired[start][0].isspace():
        start += 1
    while end > start and paired[end - 1][0].isspace():
        end -= 1
    stripped = paired[start:end]
    return [item[0] for item in stripped], [item[1] for item in stripped]

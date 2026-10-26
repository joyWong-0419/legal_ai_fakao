"""把一个问题切成检索词。

分层策略（宁可多召回，也不要静默零命中）：

1. **显式 terms**：调用方给了就不加工，便于审计"是判断错还是代码错"；
2. **词典种子**：命中的法律术语（沿用 `subject_terms.json` 的 160 个词）；
3. **二元滑窗兜底**：剔除含功能字的组合，最多补 4 个；
4. **整句兜底**：仍为空时保留整句（维持旧行为，但状态会标为 no_hit 而不是 not_covered）。

为什么不做完整中文分词：引入 jieba 会带来一个不可解释的分词器，
而出错时我们无法区分"分词错"还是"检索错"。词典 + 二元窗足够把
"整句当一个词"的零命中问题解决掉。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

DEFAULT_TERMS_PATH = (
    Path(__file__).resolve().parent / "config" / "subject_terms.json"
)

MAX_TERMS = 8
MAX_DICTIONARY_SEEDS = 6
MAX_BIGRAMS = 4

# 含这些字的二元组基本是"为什么 / 可以 / 是不是"之类的功能词，切出来只会稀释打分
FUNCTION_CHARS = set(
    "怎 么 为 什 可 以 吗 呢 吧 的 了 是 有 没 会 能 要 就 都 也 很 而 且 但 或 与 及 在 我 你 他 她 它 们 这 那 哪 何 如 若 则 被 把 从 到 对 于 之 其 所 者"
    .split()
)

_SPLIT = re.compile(r"[\s,，。；;、！？!?：:（）()\[\]【】「」《》]+")


def load_dictionary_terms(path: Path | str = DEFAULT_TERMS_PATH) -> tuple[str, ...]:
    """读取词典里的全部法律术语，按长度降序（长词更具体，先匹配）。"""
    import json

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    terms: set[str] = set()
    for config in (payload.get("domains") or {}).values():
        for term in config.get("terms") or ():
            text = str(term).strip()
            if len(text) >= 2:
                terms.add(text)
    return tuple(sorted(terms, key=lambda item: (-len(item), item)))


def split_terms(
    query: str,
    *,
    dictionary: Iterable[str] | None = None,
    max_terms: int = MAX_TERMS,
    include_bigrams: bool = True,
) -> list[str]:
    """返回去重后的检索词列表（保持出现顺序）。

    ``include_bigrams=False`` 时只返回词典命中的法律术语，
    用于「先精确检索，零命中再放宽」的两段式策略。
    """
    text = (query or "").strip()
    if not text:
        return []

    seeds_source = (
        tuple(dictionary) if dictionary is not None else load_dictionary_terms()
    )
    found: list[str] = []
    seen: set[str] = set()

    # 1. 词典种子：按长词优先，避免「和解」抢在「刑事和解」前面
    for term in seeds_source:
        if len(found) >= MAX_DICTIONARY_SEEDS:
            break
        if term in seen:
            continue
        if term in text:
            found.append(term)
            seen.add(term)

    # 2. 二元滑窗兜底
    if include_bigrams and len(found) < 3:
        added = 0
        compact = "".join(_SPLIT.split(text))
        for index in range(len(compact) - 1):
            if added >= MAX_BIGRAMS or len(found) >= max_terms:
                break
            gram = compact[index : index + 2]
            if gram in seen:
                continue
            if any(char in FUNCTION_CHARS for char in gram):
                continue
            found.append(gram)
            seen.add(gram)
            added += 1

    # 3. 整句兜底
    if not found:
        found = [text]

    return found[:max_terms]

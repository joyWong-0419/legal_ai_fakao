"""路由决策与状态判定（纯函数，可单测）。

分工原则：
- **模型**决定查哪些书、哪一层、切哪些词；
- **机器**只负责把选择翻译成过滤条件，并把结果如实分类。

这里没有任何启发式判断——`dictionary` 只出现在 `scope="auto"` 这一条显式路径上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

LAYER_MAP_PATH = Path(__file__).resolve().parent / "config" / "layer_map.json"


@dataclass(frozen=True)
class Routing:
    subjects: tuple[str, ...]
    book_ids: tuple[str, ...]
    source: str  # model | dictionary | none
    layer: str | None = None
    requested: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "layer": self.layer,
            "subjects": list(self.subjects),
            "book_ids": list(self.book_ids),
        }


def load_layer_map(path: Path | str = LAYER_MAP_PATH) -> dict[str, tuple[str, ...]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        raise ValueError("layer_map.json 缺少 layers")
    return {
        str(name): tuple(str(item) for item in config.get("subjects") or ())
        for name, config in layers.items()
    }


def subjects_for_layer(
    layer: str, mapping: Mapping[str, Sequence[str]] | None = None
) -> tuple[str, ...]:
    table = mapping if mapping is not None else load_layer_map()
    key = (layer or "").strip()
    if key not in table:
        raise ValueError(f"未知的层：{layer}；可选：{'、'.join(table)}")
    return tuple(table[key])


def resolve_routing(
    *,
    book_ids: Iterable[str] | None = None,
    subjects: Iterable[str] | None = None,
    layer: str | None = None,
    auto_subjects: Iterable[str] | None = None,
    layer_map: Mapping[str, Sequence[str]] | None = None,
) -> Routing:
    """按优先级决定过滤条件。

    优先级：book_ids > subjects > layer > auto（词典）> 不过滤。
    """
    chosen_books = tuple(
        dict.fromkeys(str(item).strip() for item in (book_ids or ()) if str(item).strip())
    )
    chosen_subjects = tuple(
        dict.fromkeys(str(item).strip() for item in (subjects or ()) if str(item).strip())
    )
    if chosen_books:
        return Routing(
            subjects=chosen_subjects,
            book_ids=chosen_books,
            source="model",
            layer=layer,
        )
    if chosen_subjects:
        return Routing(subjects=chosen_subjects, book_ids=(), source="model", layer=layer)
    if layer:
        return Routing(
            subjects=subjects_for_layer(layer, layer_map),
            book_ids=(),
            source="model",
            layer=layer,
        )
    auto = tuple(str(item).strip() for item in (auto_subjects or ()) if str(item).strip())
    if auto:
        return Routing(subjects=auto, book_ids=(), source="dictionary", layer=None)
    return Routing(subjects=(), book_ids=(), source="none", layer=None)


def classify_status(item_count: int, corpus_scope: Sequence[str]) -> str:
    """把检索结果分成三态。

    - ``ok``：有证据；
    - ``not_covered``：过滤后**一本书都没有**，这是语料缺口；
    - ``no_hit``：书在库里，但本次检索没命中——这是检索问题，不是语料问题。
    """
    if item_count > 0:
        return "ok"
    if not corpus_scope:
        return "not_covered"
    return "no_hit"


__all__ = [
    "Routing",
    "classify_status",
    "load_layer_map",
    "resolve_routing",
    "subjects_for_layer",
]

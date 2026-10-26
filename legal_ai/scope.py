"""检索作用域：把问题收敛到一个部门法。

这不是 agent，是**参数**：部门法路由是确定性映射，用一张词典加几条规则
就能完成，而且可以单测。

规则：
1. 词典命中数最多的部门法胜出；
2. 平局（例如「合同诈骗」同时命中民法与刑法）→ 不限定，交给全库检索；
3. 一个都没命中 → 不限定；
4. 调用方可用 ``override`` 强制指定，指定了不存在的部门法就报错。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

DEFAULT_TERMS_PATH = (
    Path(__file__).resolve().parent / "config" / "subject_terms.json"
)


@dataclass(frozen=True)
class Scope:
    domains: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    laws: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()
    expanded: bool = False

    @property
    def filtered(self) -> bool:
        return bool(self.subjects)

    def expand(self) -> "Scope":
        """放开学科过滤，但保留部门法标记（用于回显）。"""
        return replace(self, subjects=(), expanded=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domains": list(self.domains),
            "subjects": list(self.subjects),
            "laws": list(self.laws),
            "matched_terms": list(self.matched_terms),
            "expanded": self.expanded,
        }


def load_terms(path: Path | str = DEFAULT_TERMS_PATH) -> dict[str, dict[str, tuple[str, ...]]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    domains = payload.get("domains")
    if not isinstance(domains, dict) or not domains:
        raise ValueError("subject_terms.json 缺少 domains")
    parsed: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, config in domains.items():
        parsed[str(name)] = {
            "subjects": tuple(str(x) for x in config.get("subjects") or ()),
            "laws": tuple(str(x) for x in config.get("laws") or ()),
            "terms": tuple(str(x) for x in config.get("terms") or ()),
        }
    return parsed


def resolve_scope(
    query: str,
    *,
    terms: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
    override: str | None = None,
) -> Scope:
    """把一个查询解析成检索作用域。"""
    table = terms if terms is not None else load_terms()

    if override:
        name = override.strip()
        if name not in table:
            raise ValueError(f"未知的部门法：{name}；可选：{'、'.join(table)}")
        config = table[name]
        return Scope(
            domains=(name,),
            subjects=tuple(config["subjects"]),
            laws=tuple(config["laws"]),
        )

    text = (query or "").strip()
    if not text:
        return Scope()

    matched: dict[str, list[str]] = {}
    for name, config in table.items():
        hits = [term for term in config["terms"] if term and term in text]
        if hits:
            matched[name] = hits

    if not matched:
        return Scope()

    best = max(len(hits) for hits in matched.values())
    winners = [name for name, hits in matched.items() if len(hits) == best]
    if len(winners) > 1:
        # 平局：可能是跨部门法问题，不限定范围
        return Scope()

    name = winners[0]
    config = table[name]
    return Scope(
        domains=(name,),
        subjects=tuple(config["subjects"]),
        laws=tuple(config["laws"]),
        matched_terms=tuple(matched[name]),
    )

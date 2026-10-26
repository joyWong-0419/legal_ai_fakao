"""把法条源文件规范化为「行首第X条」格式。

用途：部分法源来自北大法宝导出，条文写作 ``- **第一条**　内容``，
      而 ``legal_source.parse_articles`` 按行首锚定 ``^第X条``，
      因此需要生成一份只改行首格式、不改正文文字的派生副本。

原则：
- 只处理行首的 ``- **第X条**``，正文一字不改；
- 输出到 ``law/_derived/<name>.md``，原始文件保持不动；
- 幂等：重复运行结果一致。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "law" / "_derived"

# 例：- **第一条**　　中华人民共和国民事诉讼法以宪法为根据……
_ARTICLE_LINE = re.compile(
    r"^- \*\*(第[一二三四五六七八九十百千零〇]+条)\*\*[\u3000\s]*(.*)$"
)


def normalize(text: str) -> tuple[str, int]:
    """返回（规范化文本, 命中条数）。"""
    out: list[str] = []
    hits = 0
    for line in text.split("\n"):
        match = _ARTICLE_LINE.match(line)
        if match:
            out.append(f"{match.group(1)} {match.group(2)}".rstrip())
            hits += 1
        else:
            out.append(line)
    return "\n".join(out), hits


def main(argv: list[str]) -> int:
    targets = argv[1:] or ["民诉法"]
    DERIVED.mkdir(parents=True, exist_ok=True)
    status = 0
    for name in targets:
        source = ROOT / "law" / f"{name}.md"
        if not source.exists():
            print(f"缺少源文件：{source}")
            status = 1
            continue
        normalized, hits = normalize(source.read_text(encoding="utf-8"))
        destination = DERIVED / f"{name}.md"
        destination.write_text(normalized, encoding="utf-8", newline="")
        article_lines = sum(
            1 for line in normalized.split("\n") if line.startswith("第")
        )
        print(f"{name}: 命中 {hits} 条 → {destination.relative_to(ROOT)}（行首条文 {article_lines}）")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

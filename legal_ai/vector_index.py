"""chunk 级向量索引：构建、加载、语义召回。

设计取舍：
- 不引入向量数据库。7,700 条 × 512 维 float32 约 16 MB，
  暴力余弦在 CPU 上是毫秒级，ANN 索引的开销反而更大。
- 向量按 chunk 建（检索单位），引用仍回到句子（由证据层负责）。
- 构建产物放 ``data/index/``，与 FTS 索引并列；重建不影响 FTS。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .embed import Embedder

VECTOR_SCHEMA = "chunk-vectors-v1"
MODEL_NAME = "BAAI/bge-small-zh-v1.5"

_EMBEDDER: Embedder | None = None


def get_embedder() -> Embedder:
    """进程内复用同一个 ONNX 会话（加载约 0.2 秒）。"""
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = Embedder()
    return _EMBEDDER


@dataclass(frozen=True)
class ChunkVectors:
    ids: tuple[str, ...]
    books: tuple[str, ...]
    matrix: np.ndarray
    metadata: dict

    def __len__(self) -> int:
        return len(self.ids)


def vector_paths(index_dir: Path) -> tuple[Path, Path]:
    return (
        index_dir / "chunk_vectors.npy",
        index_dir / "chunk_vectors.json",
    )


def load_chunks(project_root: Path) -> list[dict]:
    directory = project_root / "data" / "processed" / "chunks"
    rows: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    if not rows:
        raise FileNotFoundError(f"没有可用的 chunk：{directory}")
    return rows


def build(
    project_root: Path,
    index_dir: Path,
    *,
    batch_size: int = 64,
) -> dict:
    """从 processed/chunks 重建向量索引，返回统计信息。"""
    rows = load_chunks(project_root)
    texts = [str(row["text"]) for row in rows]
    ids = [str(row["chunk_id"]) for row in rows]
    books = [chunk_id.split(":", 1)[0] for chunk_id in ids]

    started = time.time()
    matrix = get_embedder().encode(texts, batch_size=batch_size)
    elapsed = time.time() - started

    index_dir.mkdir(parents=True, exist_ok=True)
    vectors_path, metadata_path = vector_paths(index_dir)
    np.save(vectors_path, matrix.astype(np.float32))
    metadata = {
        "schema_version": VECTOR_SCHEMA,
        "model": MODEL_NAME,
        "dimension": int(matrix.shape[1]),
        "chunk_count": len(ids),
        "books": sorted(set(books)),
        "ids": ids,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "encode_seconds": round(elapsed, 1),
        "note": "向量只用于召回；引用仍由句子记录生成。",
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return metadata


def load(index_dir: Path) -> ChunkVectors:
    vectors_path, metadata_path = vector_paths(index_dir)
    if not vectors_path.exists() or not metadata_path.exists():
        raise FileNotFoundError(
            f"向量索引不存在：{index_dir}；请先运行 build-vectors"
        )
    matrix = np.load(vectors_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    # chunk 顺序在构建时固定为「book_id:page:chunk」字典序，
    # 这里从 metadata 之外的旁路恢复：ids 不落盘时用 metadata 无法还原，
    # 因此 ids 单独存进 json，避免依赖构建顺序。
    ids = tuple(metadata.get("ids") or ())
    books = tuple(chunk_id.split(":", 1)[0] for chunk_id in ids)
    if not ids:
        raise ValueError("向量索引缺少 ids，请重建")
    return ChunkVectors(ids=ids, books=books, matrix=matrix, metadata=metadata)


def semantic_scores(
    vectors: ChunkVectors,
    query: str,
    *,
    allowed_books: Iterable[str] | None = None,
) -> np.ndarray:
    """返回全量余弦分数（不是 top-k），便于融合时复用。"""
    query_vector = get_embedder().encode([query])[0]
    scores = vectors.matrix @ query_vector
    allowed = set(allowed_books or ())
    if allowed:
        mask = np.array([book in allowed for book in vectors.books])
        scores = np.where(mask, scores, -np.inf)
    return scores


def top_k(
    vectors: ChunkVectors,
    scores: np.ndarray,
    k: int,
) -> list[tuple[str, float]]:
    if k <= 0:
        return []
    finite = np.isfinite(scores)
    if not finite.any():
        return []
    k = min(k, int(finite.sum()))
    order = np.argpartition(-scores, k - 1)[:k]
    order = order[np.argsort(-scores[order])]
    return [(vectors.ids[int(index)], float(scores[int(index)])) for index in order]


def rrf_fuse(
    rankings: Sequence[Sequence[str]], *, k: int = 60
) -> list[str]:
    """Reciprocal Rank Fusion：score = Σ 1/(k + rank)。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))


__all__ = [
    "ChunkVectors",
    "build",
    "get_embedder",
    "load",
    "load_chunks",
    "rrf_fuse",
    "semantic_scores",
    "top_k",
    "vector_paths",
]

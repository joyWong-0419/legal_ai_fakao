"""本地 ONNX 句向量（bge-small-zh-v1.5）。

为什么不引向量数据库：
- 语料只有约 7,700 个 chunk，512 维 float32 也就 16 MB；
- 暴力余弦 7,700 × 512 ≈ 400 万次乘加，CPU 上毫秒级；
- 引 ANN 索引省下的时间比索引自身开销还小。

这个模块只做「把文本变成向量」，不含检索与排序。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parents[1] / "vendor" / "models" / "bge-small-zh-v1.5"
)
DEFAULT_DIMENSION = 512
MAX_LENGTH = 512
BATCH_SIZE = 32


class Embedder:
    """bge 系列的 ONNX 推理：CLS 池化 + L2 归一化。"""

    def __init__(
        self,
        model_dir: Path | str = DEFAULT_MODEL_DIR,
        *,
        max_length: int = MAX_LENGTH,
        threads: int = 0,
    ) -> None:
        self.model_dir = Path(model_dir)
        onnx_path = self.model_dir / "model.onnx"
        tokenizer_path = self.model_dir / "tokenizer.json"
        if not onnx_path.exists():
            raise FileNotFoundError(f"缺少 ONNX 权重：{onnx_path}")
        if not tokenizer_path.exists():
            raise FileNotFoundError(f"缺少分词器：{tokenizer_path}")

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(
            str(onnx_path), options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self.dimension = DEFAULT_DIMENSION

    def encode(
        self, texts: list[str], *, batch_size: int = BATCH_SIZE
    ) -> np.ndarray:
        """返回 (n, dim) 的 L2 归一化向量；空输入返回 (0, dim)。"""
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)

        chunks: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encodings = self.tokenizer.encode_batch(batch)
            input_ids = np.array([item.ids for item in encodings], dtype=np.int64)
            attention_mask = np.array(
                [item.attention_mask for item in encodings], dtype=np.int64
            )
            feed = {"input_ids": input_ids, "attention_mask": attention_mask}
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)
            hidden = self.session.run(None, feed)[0]
            pooled = hidden[:, 0, :]  # CLS
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            chunks.append((pooled / norms).astype(np.float32))
        return np.vstack(chunks)


def cosine_scores(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """矩阵与单条查询的余弦相似度（两边都已归一化）。"""
    return matrix @ query.astype(np.float32)

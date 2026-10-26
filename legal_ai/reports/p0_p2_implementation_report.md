# P0–P2 实施报告

生成日期：2026-07-09

## 实施范围

已完成规划中的：

- P0：项目基线与六本书书目登记。
- P1：逐页原始文本抽取、PDF SHA-256 存证、书签提取和抽取质量报告。
- P2：规范化、切句、章节路径映射和不跨 PDF 页的 chunk 生成。

当前实现仍然不调用模型、不进行 OCR、不接入云服务、向量数据库或 Web UI，
也不修改 `book/` 下的原始 PDF。

## P2 处理结果

| book_id | processed 页 | sentences | chunks | 有章节路径页 |
|---|---:|---:|---:|---:|
| `dworkin-laws-empire-zh` | 365 | 5755 | 631 | 363 |
| `hart-concept-of-law-3e-zh` | 231 | 7527 | 529 | 229 |
| `klunzinger-bgb-general-zh` | 285 | 3451 | 305 | 285 |
| `rousseau-social-contract-zh` | 148 | 3562 | 239 | 147 |
| `zhang-criminal-law-6e-v1` | 869 | 18645 | 1632 | 869 |
| `zhang-criminal-law-6e-v2` | 859 | 17492 | 1604 | 859 |

合计：2757 页，56432 条句子，4940 个页内 chunk。

## 新增产物

- `legal_ai/normalize.py`：版本化最小规范化规则，当前版本 `norm-v1`。
- `legal_ai/segment.py`：切句与 chunk 生成，当前版本 `seg-v1`。
- `legal_ai/process.py`：从 raw pages 生成 processed pages、sentences 与 chunks。
- `legal_ai/data/processed/pages/<book_id>.jsonl`：页级规范化文本和章节路径。
- `legal_ai/data/processed/sentences/<book_id>.jsonl`：句子、offset、文本哈希和章节路径。
- `legal_ai/data/processed/chunks/<book_id>.jsonl`：不跨 PDF 页的检索块。
- `legal_ai/reports/processing_quality.json`：P2 聚合质量统计。

`legal_ai/data/` 已被 `.gitignore` 排除，processed 层包含原文衍生数据，只保留
在本地个人环境。

## 验收情况

已执行：

```powershell
python -m legal_ai --help
python -m legal_ai inventory
python -m legal_ai process --all
python -m unittest discover -s legal_ai\tests -v
```

验收结果：

- CLI 已包含 `inventory`、`ingest`、`process`。
- 6 本书 processed 页数合计 2757，与 P1 raw 页数一致。
- 56432 条句子的 `start_offset/end_offset` 全量回指页级 `normalized_text` 成功。
- 4940 个 chunk 的首句和尾句 ID 全量存在。
- chunk 内所有 sentence ID 均属于同一 `page_id`，未发现跨 PDF 页 chunk。
- 重复运行 `process --all` 后 18 个 processed JSONL 文件 SHA-256 不变。
- 17 个 `unittest` 用例通过。

## 已知边界

- P2 只做最小规范化，不删除 raw text，也不把规范化文本覆盖回 P1 原始存证。
- 当前章节路径来自 PDF 书签；书签缺失或异常的页面保留空章节路径。
- P2 仍不推测印刷页码；印刷页码校准属于 P3。
- 当前 chunk 是页内句子聚合块；引用层仍应回到句子 ID，而不是直接引用整块。

## 下一阶段入口：P3

P3 应新增页码校准工具与审计报告：

- 生成每本书待校准页码报告。
- 支持分段 `page_maps.json`。
- 对 `verified` 映射段记录起点、中点、终点核验样本。
- 引用输出不得把 `detected` 或 `unknown` 当作已验证印刷页。

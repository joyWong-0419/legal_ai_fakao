# 全库检索恢复验收

验收日期：2026-08-14

## 范围

新增 OCR 工作仍只覆盖合同法与民法总论；原有六本文字层书籍恢复参与检索。因此当前 SQLite/FTS 索引包含 8 本书：

- `contract-law-8e`
- `yang-civil-general-2022`
- `hart-concept-of-law-3e-zh`
- `zhang-criminal-law-6e-v1`
- `zhang-criminal-law-6e-v2`
- `klunzinger-bgb-general-zh`
- `dworkin-laws-empire-zh`
- `rousseau-social-contract-zh`

未对后六本书执行新的 OCR；它们使用既有文字层处理数据。

## 索引与核验

| 指标 | 结果 |
| --- | ---: |
| 启用书目 / 已检查 PDF | 8 / 8 |
| PDF 页 | 3,836 |
| 句子 | 75,243 |
| 检索片段 / FTS 行 | 6,664 / 6,664 |
| SQLite 完整性 / 外键冲突 | `ok` / 0 |
| 全库核验错误 | 0 |

旧书回归查询也已通过：`承认规则` 限定《法律的概念》返回 3 条结果，`正当防卫` 限定原刑法两卷范围返回 3 条结果。

机器可读结果见 [verification.json](verification.json) 与 [index_quality.json](index_quality.json)。

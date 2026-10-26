# v2 基线审计与数据契约实施包

> 审计日期：2026-08-11  
> 依据：[[../../法学攻克系统/AI法律学习系统/00-全项目技术方案与北极星目标-实施Agent版|v2.0 总方案]]  
> 本轮边界：基线审计、数据契约变更清单、合成 OCR fixture、合同法试点计划。  
> 未修改：`book/` 下任何 PDF、现有 6 本书的原始/处理数据、SQLite、已保存证据包。

## 1. 基线结论

P0–P6 的文字型资料管线保持可复现，不应重写。2026-08-11 的只读复核结果：

| 项目 | 结果 |
|---|---:|
| 单元测试 | 64 / 64 通过 |
| `verify` | 0 错误；6 本书、2,757 页、56,432 句、4,940 chunks |
| SQLite | `integrity_check=ok`；外键违规 0 |
| 已保存证据包 | 11 个包、23 条证据均可核验 |
| 黄金评测 | 36 题完成且通过 |
| Macro Recall@10 | 0.970370 |
| Macro Precision@5 | 0.894444 |
| 现有管线/索引 | `p1-v1` / `fts5-trigram-v1` |

六本当前书目均为 `extract_mode=embedded_text`。`verify` 已重新校验这六个
源 PDF 的哈希和页数；本轮没有重跑或覆盖它们的抽取文件。

项目根目录目前不是 Git 工作树，因此无法用提交哈希冻结代码版本；本报告以
上述版本号、报告统计和测试结果作为可复核基线。`.gitignore` 已保护
`legal_ai/data/`、SQLite、私有审计及证据笔记，但尚未覆盖 v2.0 规定的
项目本地运行时/缓存目录。

## 2. 扫描资料审计与试点对象

试点对象为 `book/合同法（第八版）【数字版】 (崔建远) (z-library.sk, 1lib.sk, z-lib.sk).pdf`：

| 项目 | 结果 |
|---|---|
| PDF 页数 | 518 |
| SHA-256 | `891ff90ccc2c2baed8d17d37fb491ef927c7e48cd0ed2386aaa1a65e18d547da` |
| 文件大小 | 52,496,971 bytes |
| 旧抽样文字层 | 8 页中 0 页具有可用正文；大多数页为空 |
| 初步处理结论 | 需要 OCR；不得录入现有 6 本文字型索引 |

该结论来自已有 `book_analysis_result.json` 的抽样和本轮只读文件哈希复核，
不是对整本书正文的重新抽取或 OCR。真实 OCR 只能在后续阶段 C、完成环境与
适配器验收后开始。

## 3. 已识别的 v2 契约缺口

| 当前实现 | v2 必需变化 | 受影响位置 | 迁移纪律 |
|---|---|---|---|
| `BookConfig.extract_mode` 仅允许 `embedded_text` | 新增 `text_layer_policy`、`ocr_policy`、`ocr_profile`；保留旧字段兼容映射 | `domain.py`、`inventory.py`、`config/books.json` | `extract_mode=embedded_text` 映射为 `embedded_only/off`；禁止静默改变旧书行为 |
| `raw/pages/*.jsonl` 仅存 pypdf 文本 | 新建 `raw/ocr/pages/<book_id>.jsonl`，逐页保存 OCR、块、坐标、置信度、源哈希和错误 | 新增 `ocr.py`/`ocr_engines.py` | OCR 永不覆盖 `raw_text`；真实 OCR 全文只存本地数据目录 |
| `process.py` 总是由 `raw_text` 规范化 | 引入 canonical page 选择，并保留 `text_source`、核验状态和 OCR 元数据 | `process.py`、`normalize.py` | `embedded_text` 书籍的规范化文本与句子 ID 必须保持稳定 |
| SQLite `pages` 无文字来源/OCR 字段 | 新索引 schema 加来源、核验、OCR 状态、引擎、平均置信度、OCR 文本哈希 | `index.py`、`verify.py` | 使用新版本数据库；旧数据库必须显式报“不兼容”，不能混读 |
| 证据包只有页码/句子来源 | 证据加入 `text_source`、`text_verification_status`、`ocr_avg_confidence`、OCR 警告 | `evidence.py`、`obsidian_export.py` | 引文仍只能从 `sentences.normalized_text` 生成 |
| 检索未暴露来源风险 | 命中与上下文返回来源和低质量/未核验缺口 | `search.py`、`evidence.py` | 默认仍只返回短引文，不返回整页全文 |
| 无 OCR/MCP 运行边界 | 后续增加项目内环境脚本、OCR 适配器和只读 MCP | `scripts/`、`pyproject.toml`、`mcp_server.py` | MCP 不能 OCR、重建索引、写笔记或执行 shell |

## 4. 拟冻结的 v2 数据契约

### 4.1 书目策略

每本书使用三个独立字段，取代语义过宽的 `extract_mode`：

| 字段 | 建议枚举 | 含义 |
|---|---|---|
| `text_layer_policy` | `embedded_only` / `auto` / `ignore` | 是否以及怎样使用 PDF 原生文字层 |
| `ocr_policy` | `off` / `low_quality_pages` / `all_pages` | 哪些页面可以进入 OCR 候选集 |
| `ocr_profile` | 命名 profile 或 `null` | 引擎、模型、DPI、预处理和版式策略的固定名称 |

`contract-law-8e` 在真正实施阶段应以 `auto`、`low_quality_pages`、
`zh_book_cpu_v1` 录入；在 OCR 试点通过前，`enabled` 应保持 `false`，避免
被 `ingest --all` 或 `build-index --rebuild` 意外纳入。

### 4.2 OCR 原始页

新文件为 `legal_ai/data/raw/ocr/pages/<book_id>.jsonl`。本轮已用
`tests/fixtures/ocr/contract-law-8e-pilot.jsonl` 固化以下最小字段：

```text
schema_version, page_id, book_id, pdf_page, source_pdf_sha256,
ocr_status, ocr_engine, ocr_engine_version, ocr_model_id,
render_dpi, preprocess_profile, ocr_text, ocr_text_sha256,
ocr_avg_confidence, blocks[], error, skip_reason
```

`blocks[]` 的每项必须有稳定 `block_id`、`text`、`bbox`、`confidence`、
`reading_order` 和 `role`。`ocr_text` 的顺序必须由 `reading_order` 决定，
`ocr_text_sha256` 必须对 UTF-8 文本计算。

状态语义固定如下：

- `generated`：有非空文本、文字块、置信度和哈希；并不等于人工核验。
- `failed`：必须带 `error.code`；文本、哈希、置信度和块均为空，不得写作成功空页。
- `skipped`：必须带 `skip_reason`，仅用于已知封面、版权页或人工明确排除页。
- `pending`：已入候选清单但尚未处理；正式数据中不能与 `generated` 混淆。
- `not_required`：文字层经策略判定可用或 OCR 已关闭；不创建 OCR 结果也必须在统一页面层可见。

### 4.3 统一页面与证据状态

`processed/pages` 及 SQLite `pages` 必须增加：

```text
text_source: embedded_text | ocr | hybrid | empty
text_verification_status: embedded_text | ocr_unverified | ocr_verified
ocr_status, ocr_engine, ocr_avg_confidence, ocr_text_sha256
```

`hybrid` 只能在逐页、可审计地拼接经明示策略选择的字段时使用；它不是“用 OCR
偷偷修正原文”。`ocr_verified` 只能由单独的人工核验记录产生，OCR 置信度不能
改变该状态。

证据包 schema 升级后，每条证据须追加上述来源和核验字段。若来源为
`ocr_unverified`，Markdown 与 JSON 都必须包含“引用前请核对原页”的结构化
警告。学理来源与现行法未核验提示继续保留。

### 4.4 版本与迁移

建议实现时一次性升级为明确的新版本（名称可在代码评审中微调）：

```text
PIPELINE_VERSION = p2-ocr-v1
PROCESSING_SCHEMA_VERSION = 2
INDEX_SCHEMA_VERSION = 2
INDEX_VERSION = fts5-trigram-v2
EVIDENCE_SCHEMA_VERSION = evidence-v2
```

不原地改写当前 `legal_corpus.sqlite` 或既有证据包。先构建独立 v2 数据库、
跑 6 本文字型回归与 OCR fixture 集成测试，确认无回归后才切换默认路径；旧
版本只能被显式读取或给出版本不兼容错误。

## 5. 本轮已加入的可执行保护

新增的 `test_ocr_fixture.py` 检查合成 OCR fixture 的：

- page/book ID 与源哈希格式；
- 成功页的块顺序、几何坐标、置信度和文本 SHA-256；
- `failed` 与 `skipped` 页不会伪装为“空文本但成功”；
- 所有示例文字均为自编短句，不含原书正文。

这项测试在生产 OCR 代码尚未接入前先锁定输入/输出边界。后续 B/C 阶段的真实
OCR 适配器、处理器和索引迁移必须复用并扩展该夹具，而不是将夹具文本写进
产品逻辑。

## 6. 合同法 OCR 试点计划（后续阶段 C）

1. 先实现并测试项目内 `bootstrap.ps1`、`run.ps1`、`env.ps1`，确认依赖、模型
   和缓存都位于项目根目录；不使用全局 pip 或用户目录模型缓存。
2. 实现 `OCREngine` 接口、页面渲染、断点缓存键和 `ocr --book-id --pages`；先只
   允许 `contract-law-8e` 的显式调用。
3. 从正文、疑似双栏/脚注、低质量和空白/封面场景各选取页面，首次总量限定为
   10–30 页。页面清单在执行前写入私有审计报告，不从模型回答推断。
4. 对每页保存源 PDF SHA-256、引擎/模型版本、DPI、预处理配置、文字块、坐标、
   置信度、错误与缓存键；一页失败不终止其余页面。
5. 人工检查阅读顺序、标题/正文、脚注、空白页和低置信度页，生成 OCR 质量
   报告；所有引文初始仍是 `ocr_unverified`。
6. 仅当试点通过 fixture、处理、索引、证据警告和人工抽样门禁后，才扩大到整本；
   没有评测结果前不引入向量检索，也不启用 MCP 写入能力。

## 7. 阶段 B 实施结果（2026-08-11）

阶段 B 已完成，且没有下载模型、调用 OCR、处理合同法真实页面或启用 MCP。

| 验收项 | 结果 |
|---|---|
| 书目兼容 | 6 本旧书的 `extract_mode=embedded_text` 自动映射为 `embedded_only/off`；合同法以 v2 策略登记但 `enabled=false` |
| OCR 契约 | 新增 `legal_ai/ocr.py`；合成 fixture 的成功/失败/跳过页均受哈希、块顺序、坐标和状态校验 |
| 统一页面 | `process` 输出 `text_source`、`text_verification_status` 及 OCR 元数据；合成低质量页可选择 `ocr_unverified` 文本 |
| SQLite | `INDEX_SCHEMA_VERSION=2`、`fts5-trigram-v2`、`p2-ocr-v1`；原有 6 本书重建成功 |
| 旧资料回归 | 2,757 页、56,432 句、4,940 chunks 均保持不变；36 题评测仍为 Recall@10 `0.970370`、Precision@5 `0.894444` |
| 证据兼容 | 历史 evidence-v1 包保持只读可验；新 evidence-v2 包带文字来源和 OCR 核验字段 |
| 最终核验 | 0 错误；SQLite user version 2；12 个包、24 条证据可复核 |

自动化测试已由基线的 67 项扩充至 73 项，新增 OCR JSONL 读写、旧配置映射、
OCR canonical page 和未核验 OCR 引文警告的覆盖。

## 8. 阶段 C 三页试点结果（2026-08-11）

用户将原计划的 10–30 页首轮试点明确缩小为“随机 3 页”。实现采用项目根目录
`.venv`、`.cache` 和 `scripts/env.ps1`，未修改系统 PATH 或使用全局 pip；锁定
清单为 `legal_ai/requirements-ocr.lock`。OCR 栈为 PyMuPDF 1.28.2、RapidOCR
3.9.2 与 ONNX Runtime 1.28.0，轻量模型随项目 `.venv` 内的 RapidOCR wheel 提供。

固定种子 `20260811` 从合同法 518 页中不放回均匀抽取 PDF 第 `37, 115, 337`
页。每页以 300 DPI 渲染、90 秒超时、最多两次重试执行；三页均一次成功。最终
JSONL 契约、渲染图哈希、源 PDF 前后 SHA-256 与关键标题抽查均通过，且未发现
Unicode 替换字符。细节见 `reports/ocr_pilot_contract-law-8e.md` 与同名 JSON。

首次运行暴露了 Windows 子进程输出编码问题；已改为 worker/parent 双侧 UTF-8，
并在文本含 Unicode 替换字符时强制写为 `failed`。修复后用相同随机页覆盖重跑，
避免保留乱码成功记录。测试已扩充至 78 项且全部通过。

本试点不构成逐字人工核验：合同法仍为 `enabled=false`，三页均为
`ocr_unverified`，不进入 `process`、SQLite、检索或证据层；未执行全量 OCR、
向量检索或 MCP。

## 9. 下一次实施入口

下一轮仍只应继续阶段 C 的质量门禁：补充双栏、页眉页脚、脚注、低质量、空白/
版权页的样本和人工对照记录，并针对合同法建立独立黄金题与 OCR 检索评测。仅在
这些门禁通过后，才可评估有限范围的统一处理和索引；仍不得直接全量 OCR、加入
向量检索或启用 MCP。

# 本地法律学习检索系统

当前实现范围是规划中的 P0–P6 加 v2 阶段 B：六本书的书目审计、SHA-256、逐页原始
文本抽取、PDF 书签清单、抽取质量报告、规范化文本、句子记录和页内检索
块、PDF 页到印刷页的分段映射、随机抽查和人工确认，以及本地
SQLite/FTS5 trigram 检索、句子级证据包、全链路只读校验、黄金问题评测
和 Obsidian/Claudian 证据笔记导出。v2 已加入 OCR 原始页 JSONL 契约、
统一页面来源字段和 SQLite v2；尚未下载 OCR 模型、执行真实 OCR 或调用
模型、云服务及向量数据库。

## 运行环境

- Python 3.10 或更高版本
- `pypdf>=6,<7`
- 测试使用 Python 标准库 `unittest`，不强制依赖 pytest

若当前 Python 未安装依赖，且确实需要联网安装，可临时使用清华大学 TUNA
的 PyPI 镜像：

```powershell
python -m pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple "pypdf>=6,<7"
```

Codex 桌面工作区也可直接使用其自带、已包含 pypdf 的 Python。推荐在仓库
根目录直接运行 `python -m legal_ai ...`，这样默认配置会稳定指向本地
`books/` 与 `legal_ai/config/`。

本机普通 `python` 尚未安装 pypdf 时，可直接使用 Codex 自带解释器：

```powershell
$py = 'C:\Users\ok\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m legal_ai --help
```

## 中国网络环境

P0–P6 的全部命令均在本机运行，不访问网络。优先使用
已包含 pypdf 的 Codex 自带 Python；只有新环境缺少依赖时才需要国内镜像。

## 从零运行 P0–P6

在仓库根目录 `E:\about\note\fakao_note` 执行：

```powershell
python -m legal_ai --help
python -m legal_ai inventory
python -m legal_ai ingest --all
python -m legal_ai process --all
python -m legal_ai page-map audit --per-book 2
python -m legal_ai build-index --rebuild
python -m legal_ai search "正当防卫" --limit 10 --format markdown
python -m legal_ai context "正当防卫 必要限度" --limit 8 --format json
python -m legal_ai verify
python -m legal_ai citation-audit
python -m legal_ai evaluate
python -m legal_ai obsidian-export "正当防卫 必要限度" --limit 6
python -m unittest discover -s legal_ai\tests -v
```

单本重建：

```powershell
python -m legal_ai ingest --book-id hart-concept-of-law-3e-zh
python -m legal_ai process --book-id hart-concept-of-law-3e-zh
```

## 输出

- `legal_ai/reports/inventory.json`：六本书页数、SHA-256 与书目元数据。
- `legal_ai/data/raw/pages/<book_id>.jsonl`：逐页原始文本存证。
- `legal_ai/data/raw/outlines/<book_id>.json`：PDF 书签及合法性状态。
- `legal_ai/data/raw/ocr/pages/<book_id>.jsonl`：后续 OCR 原始页契约位置；
  仅允许带源 PDF 哈希、块坐标、置信度和失败状态的本地产物。
- `legal_ai/reports/extraction_quality.json`：不含长引文的质量统计。
- `legal_ai/data/processed/pages/<book_id>.jsonl`：规范化后的页级文本。
- `legal_ai/data/processed/sentences/<book_id>.jsonl`：句子、页内 offset 和文本哈希。
- `legal_ai/data/processed/chunks/<book_id>.jsonl`：不跨 PDF 页的检索块。
- `legal_ai/reports/processing_quality.json`：P2 处理数量和章节映射统计。
- `legal_ai/config/page_maps.json`：分段页码映射及状态。
- `legal_ai/reports/page_map_audit.json`：P3 机器可读审计结果。
- `legal_ai/reports/page_map_audit.md`：可直接在 Obsidian 打开的抽查清单。
- `legal_ai/reports/private/page_map_acceptance/`：本地抽查页面快照，不提交。
- `legal_ai/data/index/legal_corpus.sqlite`：本地 SQLite/FTS5 全文索引，不提交。
- `legal_ai/reports/index_quality.json`：P4 表数量、SQLite 版本与完整性统计。
- `legal_ai/data/evidence/<run_id>.json`：句子级直接引文证据包，不提交。
- `legal_ai/reports/verification.json`：不含正文的 P5 全链路校验报告。
- `legal_ai/reports/private/citation_acceptance.md`：每书 1–2 页人工核验清单，不提交。
- `legal_ai/config/golden_questions.json`：36 个不含原文的冻结黄金问题及页级 qrel。
- `legal_ai/reports/retrieval_evaluation.json`：P6 指标、来源分布和逐题结果。
- `法学攻克系统/AI法律学习系统/证据笔记/`：本地 Obsidian 证据笔记，不提交。

`legal_ai/data/` 被 `.gitignore` 排除，其中包含受版权保护的原文衍生数据，
仅用于本地个人学习。报告不会把学理来源描述为已经核验的现行法。

## 数据纪律

- `raw_text` 保留 pypdf 的逐页原始输出，不做清洗或改写。
- `processed/pages` 的 `text_source` 明确区分 `embedded_text`、`ocr`、
  `hybrid` 和 `empty`；OCR 文本初始只能是 `ocr_unverified`，置信度不等于
  人工核验。
- 抽取异常使用 `extraction_error` 明示，`raw_text` 记为 `null`，不以空
  字符串掩盖错误。
- PDF 页从 1 开始；P1 不推测印刷页码。
- 每页有稳定 `page_id` 与原始文本 SHA-256。
- 抽取前后再次计算源 PDF SHA-256；发生变化时命令失败。
- `normalized_text` 是检索层文本，不覆盖 `raw_text`。
- 句子 `start_offset/end_offset` 均指向页级 `normalized_text`。
- chunk 只在同一 PDF 页内聚合句子，引用时仍回到句子 ID。
- `detected` 只表示候选映射；引用层不得将其显示成已确认印刷页。
- `verified` 段必须已有起点、段内和终点 3 个实施检查页，并再通过一页
  用户随机抽查；无法可靠校准的页面始终保持 `unknown`。
- SQLite v2（`p2-ocr-v1` / `fts5-trigram-v2`）不会静默读取旧索引；旧
  P0–P6 证据包保持只读可核验，新建证据包会携带文字来源与 OCR 核验字段。

## P3 人工抽查

在 Obsidian 中打开 `legal_ai/reports/page_map_audit.md`，每本书查看 1–2 个
“渲染快照”或点击“打开原书”。只核对页面上是否存在候选印刷页码：

1. 候选值一致时，复制报告底部对应的 `page-map confirm` 命令执行；当前
   审计选择每书两页，所以同一可映射书籍的两页都匹配后才会升级。
2. 报告写“无可见页码”且页面确实没有页码时，执行对应的
   `--no-visible-page` 命令。
3. 看到的值与候选不一致时不要执行清单中的匹配命令，把书名、PDF 页和
   实际值发给实施 Agent，由 Agent 重新分段并生成清单。
4. 完成确认后依次运行 `python -m legal_ai process --all` 和
   `python -m legal_ai page-map audit --per-book 2`，将状态写回页级数据并
   查看最新汇总。

清单内全部用户抽查都匹配时，具备起点、段内、终点 3 个实施检查样本的
对应 `detected` 段才会升级为 `verified`。任何后续反例都会被记录并立即
撤销该段的 `verified`。确认记录保存在 `reports/private/`，不会进入公开
仓库。预期结果是刑法上、刑法下、德国民法总则和法律帝国的正文段升级；
《法律的概念》和《社会契约论》继续保持 `unknown`，这不是验收失败。

2026-07-11 的 12 页用户抽查已经全部通过。当前共 `verified` 2,278 页、
`unknown` 479 页，`detected/rejected` 均为 0；上述步骤保留用于以后出现
反例或重新生成审计计划时复核。

## P4 本地检索

三字以上中文术语由 FTS5 trigram 召回；两字和一字查询使用参数化
`instr` 子串回退。多词以空格或常见标点分开召回后合并。所有过滤值均为
SQL 绑定参数。P4 尚不做中文自然问句分词；长问题请先写成以空格分隔的
关键词，例如 `正当防卫 必要限度`。

```powershell
# Markdown，适合在 Obsidian/Claudian 中阅读
python -m legal_ai search "正当防卫" --limit 10 --format markdown

# JSON，适合让 Claudian 或其他本地 Agent 继续处理
python -m legal_ai search "合同" --limit 8 --format json `
  --jurisdiction "德国" --subject "民法"

# 过滤到单本书；过滤参数均可重复
python -m legal_ai search "法律解释" --book-id hart-concept-of-law-3e-zh
```

搜索结果包含可解释的 FTS、精确命中、章节、元数据和最终分数；默认同一
PDF 页最多返回两个 chunk。输出中的文本明确标记为“检索片段”，不是 P5
意义上的精确引文或证据包。`build-index --rebuild` 使用同目录临时文件原子
替换，保留既有 `retrieval_runs`，且拒绝写入索引目录之外的路径。P3 页码
确认后，应重新执行：

```powershell
python -m legal_ai process --all
python -m legal_ai build-index --rebuild
```

## P5 证据包与校验

`context` 先检索 chunk，再只凭 `chunk_id` 回查数据库中的书籍、页面和句子；
调用方不能传入引文、PDF 页或句子 ID。每条引文由 1–2 个完整连续句子组成，
不超过 400 字。元数据命中、质量标记页或无法完整输出的超长句不会冒充
直接证据。证据包固定声明当前六本书都是学理来源，不能单独证明现行法。

```powershell
# JSON：适合交给 Claudian 或其他本地 Agent
python -m legal_ai context "正当防卫 必要限度" --limit 8 --format json

# Markdown：可直接复制或嵌入 Obsidian 笔记
python -m legal_ai context "意思表示" --limit 8 --format markdown `
  --jurisdiction "德国"

# 全量只读核验 PDF、页码、句子、chunk、FTS、检索历史和证据包
python -m legal_ai verify

# 复用 P3 固定随机页，生成每书 2 页、共 12 页的人工清单
python -m legal_ai citation-audit
```

证据落盘前会再次反查句子、定位、引文哈希和对应检索运行；任一不一致就
拒绝写入。`verified` 才显示“已核验印刷页”，`detected` 明示为候选且
未核验，`unknown/rejected` 只使用 PDF 页。若当前 `page_maps.json` 已降级
但 SQLite 尚未重建，`context` 会拒绝生成证据，避免旧状态造成伪核验。

人工核验时在 Obsidian 打开
`legal_ai/reports/private/citation_acceptance.md`，每条点击“打开原书”或
“打开渲染快照”，检查短引文、PDF 页和印刷页状态。当前清单按每书 2 页
生成，因此 12 页都应检查；若要改成每书 1 页，先重新执行：

```powershell
python -m legal_ai page-map audit --per-book 1 --seed 20260710
python -m legal_ai citation-audit
```

## P6 黄金评测

`evaluate` 固定使用 36 个真实学习问题：每本书 5 个单书问题，共 30 个；
另有 6 个跨书比较问题。每个问题登记人工候选范围，并把相关页绑定到真实
句子 ID 和 SHA-256。当前前五页全部经过 0/1/2 人工判断；另外每题至少有
一条来自目录/术语扫描候选范围、且不在当前前五页内的独立正例，避免用
被测排序器的结果反向“自证” Recall。

```powershell
python -m legal_ai evaluate
python -m legal_ai evaluate --format json
```

评测固定请求用户实际看到的 top 10 chunk，再按 PDF 页去重；不会扩大候选
池来凑满 10 个页面。`Precision@5` 的分母始终为 5。只要前五存在未判断页，
或缺少独立候选池正例，状态就是 `incomplete`，不会输出正式 macro 指标。
当前真实结果为 Macro `Recall@10 = 0.970`、`Precision@5 = 0.894`，超过
规划门槛 0.85/0.70；6 个跨书题均在前十结果中命中至少两本相关书。

## P6 Obsidian / Claudian 接入

当前 vault 已安装 Claudian。系统不需要额外插件 API：`obsidian-export`
直接在 vault 内新建 Markdown，Claudian 即可读取和继续整理。

```powershell
python -m legal_ai obsidian-export "正当防卫 必要限度" --limit 6

# 可指定目录和文件名；同名时创建 -2、-3，绝不覆盖原笔记
python -m legal_ai obsidian-export "承认规则 法效力" `
  --output-dir "法学攻克系统/AI法律学习系统/证据笔记" `
  --filename "承认规则证据笔记.md"
```

笔记包含问题、定义 TODO、证据、引用索引、覆盖缺口、待核验现行法和个人
归纳区；frontmatter 固定 `current_law_verified: false`。证据与引用区保留
run ID、证据包、页面/句子 ID、引文 SHA-256 和可点击 PDF 页链接。由
`legal-concept-forge` 约束定义/归纳结构，但导出器不会凭空生成定义。

Claudian 适合作为后处理协作者：可以填写“定义”和“我的归纳与比较”，但
不应手改证据与定位。需要新证据时重新运行 `obsidian-export`。

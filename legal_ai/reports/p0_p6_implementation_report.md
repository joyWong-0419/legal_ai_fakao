# P0–P6 第一阶段实施报告

## 结论

第一阶段 P0–P6 已完成。用户于 2026-07-11 确认六本书、每书 2 页、共
12 页的人工抽查全部通过；页码映射、证据包、检索评测和 Obsidian 最小
接入均形成可复现命令与报告。

全程未改变模型选择，未修改 `book/` 下的 PDF，未启动 OCR、云服务、
向量数据库或 Web UI。除核对国内镜像和 Obsidian 官方链接语法外，项目
运行本身全部离线。

## 语料与索引状态

| 项目 | 结果 |
|---|---:|
| 书籍 | 6 |
| PDF / 索引页 | 2,757 |
| 句子 | 56,432 |
| chunk | 4,940 |
| FTS 行 | 4,940 |
| 页码 `verified` | 2,278 |
| 页码 `unknown` | 479 |
| 页码 `detected/rejected` | 0 / 0 |
| SQLite `integrity_check` | `ok` |
| 外键违规 | 0 |

刑法上、刑法下、德国民法总则和《法律帝国》的正文映射已经用户抽样确认；
《法律的概念》和《社会契约论》没有可靠可见印刷页，因而保持 `unknown`。
这表示系统诚实地只使用 PDF 页，不是验收失败。

## P5 最终闭环

- 12/12 个人目视样本通过，引文、PDF 定位和印刷页状态一致。
- 确认记录保存在
  `legal_ai/reports/private/citation_acceptance_confirmation.json`。
- 用户确认后已重新运行 `process --all` 和 `build-index --rebuild`。
- 历史证据包允许保守的 `detected → verified` 后续升级；反向降级仍会使
  旧 `verified` 证据失败，防止伪核验。
- 最新 `verify` 检查 6 个 PDF、2,757 页、56,432 句、4,940 chunk/FTS、
  32 次检索运行、11 个证据包和 23 条证据，错误为 0。

## P6 黄金问题集

黄金集位于 `legal_ai/config/golden_questions.json`：

- 36 个真实学习问题；
- 每本书 5 个单书问题，共 30 个；
- 6 个跨书比较问题；
- 覆盖中国刑法总论、刑法分论、德国民法总则、哈特规则理论、德沃金
  解释理论、卢梭主权/法律观和跨书比较；
- 216 条页级人工判断，其中每题当前前五页均有 0/1/2 判断；
- 每题另有至少 1 条不在当前前五页、来自目录或术语扫描候选范围的独立
  正例，并绑定真实句子 ID 和 SHA-256。

最后一项是重要的抗自证门禁。早期版本只判断当前前五页，导致 Recall@10
机械地等于 1.0；该结果未被接受。补充独立 qrel 并冻结后，正式 Recall
下降到 0.970，反映了 6 个真实未召回页，而不是用排序器结果反向构造真值。

### 指标口径

- 检索固定调用用户实际看到的 `limit=10` chunk 结果；
- 相同 PDF 页只保留首次出现的结果；
- 不扩大候选池来凑满 10 个唯一页；
- `Precision@5` 分母固定为 5；
- 前五有未判断页、预期书籍无正例，或缺少候选范围外的独立池正例时，
  状态为 `incomplete`，不生成正式 macro/micro 指标；
- qrel 的句子文本 SHA、数据库 SHA 和黄金集 SHA 必须三向一致。

### 真实评测结果

| 指标 | 结果 | 门槛 |
|---|---:|---:|
| Macro Recall@10 | 0.970370 | ≥ 0.85 |
| Macro Precision@5 | 0.894444 | ≥ 0.70 |
| Micro Recall@10 | 0.969543 | 报告项 |
| Micro Precision@5 | 0.894444 | 报告项 |
| 跨书题至少命中 2 本相关书 | 6/6 | 6/6 |
| 前五未判断页 | 0 | 0 |
| 评测状态 | complete / passed | passed |

逐书 top-10 页来源计数为：德沃金 67、哈特 59、德国民法 50、卢梭 58、
刑法上 48、刑法下 43。完整报告位于
`legal_ai/reports/retrieval_evaluation.json`。

## P6 Obsidian / Claudian 最小接入

新增命令：

```powershell
python -m legal_ai obsidian-export "正当防卫 必要限度" --limit 6
```

默认在 `法学攻克系统/AI法律学习系统/证据笔记/` 新建 Markdown。当前 vault
已安装 Claudian 2.0.25，它能直接读取 vault 内文件，因此不需要开发额外
插件或联网接口。

笔记包含：

- 问题；
- 定义 TODO；
- E001… 直接证据；
- 引用索引；
- 覆盖缺口；
- `current_law_verified: false` 的待核验现行法清单；
- “我的归纳与比较”工作区；
- run ID、证据包路径、book/page/chunk/sentence ID、引文 SHA 和 PDF 页链接。

`legal-concept-forge` 只用于约束定义、边界和比较法笔记结构，不会替代原文
证据，也不会凭空生成定义。Claudian 可以继续填写定义和归纳区，但不应
修改证据/引用区；需要新证据时重新导出。

### 写入安全

- 输出必须位于当前 vault；
- 拒绝 `.obsidian/`、`book/`、`legal_ai/data/` 和路径逃逸；
- 文件使用独占创建，同名时生成 `-2/-3`，永不覆盖已有笔记；
- 公开 writer 也会重新调用 SQLite 证据校验，调用方即使同时伪造引文和
  自报哈希也不能写成“直接证据”；
- PDF 链接采用 Obsidian 原生
  `[[books/⟨书名⟩.pdf#page=N|打开 PDF 第 N 页]]` 形式。

示范笔记：
`法学攻克系统/AI法律学习系统/证据笔记/P6-正当防卫证据笔记.md`。

## 测试与复现

自动化测试：64/64 通过。新增覆盖包括：

- qrel SHA 篡改、空候选池、重复问题/查询、排序版本池来源；
- 没有独立候选池正例时评测保持 `incomplete`；
- page 去重和固定 P@5 分母；
- Obsidian 路径逃逸、受保护目录、空证据、同名不覆盖；
- 自算哈希的伪造证据包仍被 writer 拒绝；
- Windows 文件名和 JSON CLI 输出。

复现命令：

```powershell
$py = 'C:\Users\ok\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m legal_ai verify
& $py -m legal_ai evaluate
& $py -m legal_ai obsidian-export '正当防卫 必要限度' --limit 6
& $py -m unittest discover -s legal_ai\tests -v
```

## 已知边界与下一阶段

- 当前检索是可解释的词法检索，长自然问句应提供空格分隔关键词；
- 30 个单书问题使用书籍过滤，6 个跨书问题限定比较书目；当前指标不代表
  任意无过滤问题的全库质量；
- top-10 的原始单位是 chunk，再按页折叠，因此部分问题少于 10 个唯一页；
- 当前六本都是学理来源，不能单独证明现行法；
- 扫描书 OCR、正式法源库、语义向量和复杂 Obsidian 自动化仍属于后续阶段。

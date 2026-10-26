# P0-P4 实施报告

## 结论

P4 SQLite 与全文检索已经完成。系统当前可从六本本地、可抽取文字的 PDF
重建私有 SQLite 数据库，以 FTS5 trigram 检索三字以上中文术语，并以安全
的参数化子串查询召回两字或一字术语。未调用任何模型，也未改变模型选择。

按用户指示，项目在 P4 验收完成后中止，不进入 P5。

## 实现内容

- 新增 `index.py`：原子重建 SQLite、五张核心表、法域/学科辅助表、FTS5
  trigram、外键和一致性门禁；重建目标被强制限制在
  `legal_ai/data/index/` 的 `.sqlite/.sqlite3` 文件，不能覆盖 PDF 或笔记。
- 新增 `search.py`：多词分别召回、短词回退、书籍/法域/学科过滤、可解释
  排序、同页去重、跨书适度多样化、检索运行留痕。
- 新增 CLI：
  - `python -m legal_ai build-index --rebuild`
  - `python -m legal_ai search "<query>" --limit 10 --format markdown|json`
- Windows 标准输出统一为 UTF-8，中文、德文混合 JSON 可直接解析。
- Markdown 结果生成 Obsidian PDF 页链接，并把 chunk 明示为“检索片段”。

## 真实数据库验收

| 项目 | 数量/结果 |
|---|---:|
| 书籍 | 6 |
| PDF 页 | 2,757 |
| 句子 | 56,432 |
| chunk | 4,940 |
| FTS 行 | 4,940 |
| 已保留检索运行 | 5 |
| SQLite `integrity_check` | ok |
| 外键违规 | 0 |
| 页码状态 | detected 2,278；unknown 479 |

数据库位置为 `legal_ai/data/index/legal_corpus.sqlite`，位于 Git 忽略的本地
私有数据目录。

## 检索验收

- “正当防卫”由 trigram 在真实库召回 128 个候选 FTS 行。
- 两字“合同”通过参数化子串回退召回，并可同时按德国法域、民法学科过滤。
- 重建前后“正当防卫”前 10 个 chunk ID 及顺序一致。
- 结果页去重实测每页最多 2 个 chunk。
- 单引号、双引号、百分号、下划线、星号、括号、`OR`、连字符和类似 SQL
  注入文本均不报错、不改变 SQL 结构。
- JSON CLI 输出经解析器直接读取；德文字符不会再触发 Windows GBK 错误。
- 自动化测试当前 38 项全部通过。

## 当前边界

- P4 是词法检索，不处理近义改写或语义向量召回。
- P4 不对无空格中文自然问句自动分词；多概念查询应以空格或标点分隔。
- 当前搜索输出是候选检索块，不是精确引文；句子级证据包属于尚未实施的
  P5。
- `detected` 印刷页只显示为未核验候选，不会冒充 `verified`。
- 《法律的概念》和《社会契约论》电子版无可靠可见印刷页码，继续保持
  `unknown`。
- P3 用户抽查尚待完成；完成后需重新运行 `process --all` 和
  `build-index --rebuild`，数据库才会收到新的页码状态。

## 可复现命令

```powershell
$py = 'C:\Users\ok\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m legal_ai process --all
& $py -m legal_ai page-map audit --per-book 2 --seed 20260710
& $py -m legal_ai build-index --rebuild
& $py -m legal_ai search '正当防卫' --limit 10 --format json
& $py -m legal_ai search '合同' --limit 10 --format markdown --jurisdiction '德国' --subject '民法'
& $py -m unittest discover -s legal_ai\tests -v
```

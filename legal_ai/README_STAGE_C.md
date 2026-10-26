# 阶段 C：OCR 与全库检索使用说明

本文件补充 `README.md` 的 P0–P6 历史说明。新增 OCR 工作仅限定为 `contract-law-8e`（合同法）和 `yang-civil-general-2022`（民法总论）；既有六本文字层书籍保持启用并继续参与全库检索。未对其余书籍启动新的 OCR 工作。

环境和 OCR 命令均应从项目根目录运行，使用项目私有 `.venv`、`.cache` 和 `vendor/`；不得使用全局 pip、`--user` 或修改系统 PATH。

```powershell
$py = 'C:\Users\ok\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap_ocr.ps1 -Python $py
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_legal_ai.ps1 `
  ocr-pilot --book-id contract-law-8e --random-count 3 --seed 20260811
```

- 锁定包版本：`requirements-ocr.lock`。
- 初始化脚本将 Python 包安装到 `.venv`，pip 下载缓存写入 `.cache\pip`。
- RapidOCR 3.9.2 的轻量 CPU 模型随 wheel 位于项目 `.venv`；试点不下载用户目录模型。
- 输出 OCR 正文、渲染图与缓存键仅在被 Git 忽略的 `data/raw/ocr/`；公开报告不保存整页正文。
- `ocr-pilot` 仅处理显式页或带固定种子的随机小样本。它不会开启书目、调用 `process`、重建索引或产生证据包。

本次合同法试点的固定随机页为 37、115、337，均成功但始终是 `ocr_unverified`。详情见 [OCR 试点审计](reports/ocr_pilot_contract-law-8e.md)。

## 已核验样本后的全书原始 OCR

在样本通过人工核验后，可执行仅写入原始层的全书任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_legal_ai.ps1 `
  ocr --book-id contract-law-8e --all --checkpoint-every 10
```

- `ocr` 要求 `--all`，并读取 `config/ocr_page_exclusions.json` 中与源 PDF SHA-256 绑定的封面、出版信息和空白页排除表。
- 常驻 RapidOCR worker 会复用模型；每页仍有独立超时。每 10 个更新页以原子方式写入 JSONL，可在中断后直接重复同一命令续跑。
- 排除页保留为 `skipped`（已有人工审计的试点原始记录可以保留），不会进入未来的正文语料。
- 全书命令仍不会开启书目、调用 `process` 或重建索引；所有 `generated` 文本一律为 `ocr_unverified`。

合同法第八版的原始 OCR 覆盖结果为：518 页记录完整，514 页 `generated`、4 页 `skipped`、0 页失败。处理阶段还会依据与 PDF SHA-256 绑定的排除表，将第 1、2、3 页作为前置页、4、6、518 页作为空白页；即使原始层保留历史试点记录，也不会作为正文进入检索。详见 [全书 OCR 覆盖报告](reports/ocr_full_contract-law-8e.md) 与机器可读 [审计 JSON](reports/ocr_full_contract-law-8e.json)。

## 民法总论：导入既有 PP-OCRv6 JSON

民法总论已经提供了页数组式 PP-OCRv6 JSON。因此必须使用导入命令，不重新运行本地 OCR，也不从相邻 Markdown 文件取正文：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_legal_ai.ps1 `
  ocr-import --book-id yang-civil-general-2022 `
  --input '.\book\民法总论 (杨代雄) (z-library.sk, 1lib.sk, z-lib.sk).pdf_by_PP-OCRv6.json'
```

- 导入器逐项按数组下标对应一基 PDF 页码；JSON 项数必须与 PDF 页数完全相等。
- 原始 JSON 只读；导入记录保留其 SHA-256、`paddleocr` / `external-pp-ocrv6` / `PP-OCRv6` 溯源信息，`render_dpi` 保持 `null`，不伪造本地渲染参数。
- 空结果页和已登记空白页进入 `skipped`；无效置信度块会被丢弃并计入审计，但不改写原始 JSON。所有可检索 OCR 文本均为 `ocr_unverified`，引用前仍须对照 PDF 原页。

本次导入：561 页中 550 页 `generated`、11 页 `skipped`、0 页失败；共丢弃 21 个无效置信度块。机器可读审计见 [民法总论 OCR 导入报告](reports/ocr_import_yang-civil-general-2022.json)。

## 全库检索复跑与验收

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_legal_ai.ps1 ingest --all
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_legal_ai.ps1 process --all
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_legal_ai.ps1 `
  build-index --rebuild --discard-retrieval-history
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_legal_ai.ps1 `
  verify --current-scope
```

`--current-scope` 核验当前全部启用书目，不将历史证据包作为当前范围的失败项。民法总论接入时的双书快照见 [双书 OCR 接入验收报告](reports/dual_book_retrieval_acceptance.md)；当前全库状态见 [全库检索验收报告](reports/full_corpus_retrieval_acceptance.md)。

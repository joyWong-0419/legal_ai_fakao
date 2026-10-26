# P3 印刷页码映射审计

本报告只把 `verified` 当作已确认印刷页；`detected` 是候选映射，
`unknown` 表示当前版本没有可可靠显示的印刷页码。

人工验收抽样：每本书 2 页；固定随机种子 `20260710`，重复运行得到相同页面。

## 汇总

| 书名 | PDF 页 | verified | detected | unknown | rejected |
|---|---:|---:|---:|---:|---:|
| 法律的概念 第三版 | 231 | 0 | 0 | 231 | 0 |
| 刑法学 第六版（上） | 869 | 838 | 0 | 31 | 0 |
| 刑法学 第六版（下） | 859 | 837 | 0 | 22 | 0 |
| 德国民法导论：总则 | 285 | 249 | 0 | 36 | 0 |
| 法律帝国 | 365 | 354 | 0 | 11 | 0 |
| 社会契约论 | 148 | 0 | 0 | 148 | 0 |

## 分段映射

### 法律的概念 第三版 (`hart-concept-of-law-3e-zh`)

| PDF 范围 | 印刷页候选 | 状态 | 实施检查页 | 说明 |
|---|---|---|---|---|
| 1-231 | - | unknown | 11, 120, 230 | 抽样页面未显示可可靠核验的印刷页码，保持 unknown。 |

### 刑法学 第六版（上） (`zhang-criminal-law-6e-v1`)

| PDF 范围 | 印刷页候选 | 状态 | 实施检查页 | 说明 |
|---|---|---|---|---|
| 1-30 | - | unknown | - | 封面、历版前言、目录及无可见页码的正文篇章页。 |
| 31-868 | 2-839 | verified | 31, 449, 868 | 页眉页码稳定满足 printed_page = pdf_page - 29；用户已于 2026-07-11 完成随机抽查，映射已核验。 |
| 869-869 | - | unknown | - | 末页无正文页码。 |

### 刑法学 第六版（下） (`zhang-criminal-law-6e-v2`)

| PDF 范围 | 印刷页候选 | 状态 | 实施检查页 | 说明 |
|---|---|---|---|---|
| 1-22 | - | unknown | - | 封面、目录和无可见页码的篇章页。 |
| 23-859 | 844-1680 | verified | 23, 441, 859 | 页眉页码稳定满足 printed_page = pdf_page + 821；用户已于 2026-07-11 完成随机抽查，映射已核验。 |

### 德国民法导论：总则 (`klunzinger-bgb-general-zh`)

| PDF 范围 | 印刷页候选 | 状态 | 实施检查页 | 说明 |
|---|---|---|---|---|
| 1-35 | - | unknown | - | 前言、目录、篇章页及无可见页码页面。 |
| 36-284 | 4-252 | verified | 36, 159, 284 | 页眉页码稳定满足 printed_page = pdf_page - 32；用户已于 2026-07-11 完成随机抽查，映射已核验。 |
| 285-285 | - | unknown | - | 封底页。 |

### 法律帝国 (`dworkin-laws-empire-zh`)

| PDF 范围 | 印刷页候选 | 状态 | 实施检查页 | 说明 |
|---|---|---|---|---|
| 1-10 | - | unknown | - | 封面、序言和目录存在重置页码，当前不合并推断。 |
| 11-364 | 1-354 | verified | 11, 188, 364 | 正文页脚稳定满足 printed_page = pdf_page - 10；用户已于 2026-07-11 完成随机抽查，映射已核验。 |
| 365-365 | - | unknown | - | 出版信息页。 |

### 社会契约论 (`rousseau-social-contract-zh`)

| PDF 范围 | 印刷页候选 | 状态 | 实施检查页 | 说明 |
|---|---|---|---|---|
| 1-148 | - | unknown | 3, 75, 148 | 电子版抽样页面未显示可可靠核验的印刷页码，保持 unknown。 |

## 你的随机抽查页

打开 PDF 指定页，只核对页面上可见的印刷页码。候选值一致时执行
对应命令；当前无候选且页面确实无可见页码时，才执行
`--no-visible-page` 命令。若不一致，不要执行命令；把书名、PDF 页
和实际看到的页码交给实施 Agent 重新分段。

| 书名 | PDF 页 | 候选印刷页 | 当前状态 | 打开原书 | 渲染快照 |
|---|---:|---:|---|---|---|
| 法律的概念 第三版 | 54 | 待观察（当前无候选） | unknown | [[book/天下·法律的概念(第三版) (哈特) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=54\|打开 PDF 第 54 页]] | [查看](private/page_map_acceptance/hart-concept-of-law-3e-zh/pdf-0054.png) |
| 法律的概念 第三版 | 141 | 待观察（当前无候选） | unknown | [[book/天下·法律的概念(第三版) (哈特) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=141\|打开 PDF 第 141 页]] | [查看](private/page_map_acceptance/hart-concept-of-law-3e-zh/pdf-0141.png) |
| 刑法学 第六版（上） | 327 | 298 | verified | [[books/张明楷刑法6上.pdf#page=327\|打开 PDF 第 327 页]] | [查看](private/page_map_acceptance/zhang-criminal-law-6e-v1/pdf-0327.png) |
| 刑法学 第六版（上） | 444 | 415 | verified | [[books/张明楷刑法6上.pdf#page=444\|打开 PDF 第 444 页]] | [查看](private/page_map_acceptance/zhang-criminal-law-6e-v1/pdf-0444.png) |
| 刑法学 第六版（下） | 576 | 1397 | verified | [[books/张明楷刑法6下.pdf#page=576\|打开 PDF 第 576 页]] | [查看](private/page_map_acceptance/zhang-criminal-law-6e-v2/pdf-0576.png) |
| 刑法学 第六版（下） | 748 | 1569 | verified | [[books/张明楷刑法6下.pdf#page=748\|打开 PDF 第 748 页]] | [查看](private/page_map_acceptance/zhang-criminal-law-6e-v2/pdf-0748.png) |
| 德国民法导论：总则 | 36 | 4 | verified | [[book/德国民法导论：总则【数字版】 ([德] 欧根·克伦钦格) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=36\|打开 PDF 第 36 页]] | [查看](private/page_map_acceptance/klunzinger-bgb-general-zh/pdf-0036.png) |
| 德国民法导论：总则 | 273 | 241 | verified | [[book/德国民法导论：总则【数字版】 ([德] 欧根·克伦钦格) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=273\|打开 PDF 第 273 页]] | [查看](private/page_map_acceptance/klunzinger-bgb-general-zh/pdf-0273.png) |
| 法律帝国 | 66 | 56 | verified | [[book/法律帝国 = Law’s Empire (罗纳德·德沃金, Ronald Dworkin, 许杨勇 (译)) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=66\|打开 PDF 第 66 页]] | [查看](private/page_map_acceptance/dworkin-laws-empire-zh/pdf-0066.png) |
| 法律帝国 | 244 | 234 | verified | [[book/法律帝国 = Law’s Empire (罗纳德·德沃金, Ronald Dworkin, 许杨勇 (译)) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=244\|打开 PDF 第 244 页]] | [查看](private/page_map_acceptance/dworkin-laws-empire-zh/pdf-0244.png) |
| 社会契约论 | 81 | 待观察（当前无候选） | unknown | [[book/社会契约论 (卢梭) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=81\|打开 PDF 第 81 页]] | [查看](private/page_map_acceptance/rousseau-social-contract-zh/pdf-0081.png) |
| 社会契约论 | 133 | 待观察（当前无候选） | unknown | [[book/社会契约论 (卢梭) (z-library.sk, 1lib.sk, z-lib.sk).pdf#page=133\|打开 PDF 第 133 页]] | [查看](private/page_map_acceptance/rousseau-social-contract-zh/pdf-0133.png) |

### 确认命令

```powershell
python -m legal_ai page-map confirm --book-id hart-concept-of-law-3e-zh --pdf-page 54 --no-visible-page
python -m legal_ai page-map confirm --book-id hart-concept-of-law-3e-zh --pdf-page 141 --no-visible-page
python -m legal_ai page-map confirm --book-id zhang-criminal-law-6e-v1 --pdf-page 327 --printed-page 298
python -m legal_ai page-map confirm --book-id zhang-criminal-law-6e-v1 --pdf-page 444 --printed-page 415
python -m legal_ai page-map confirm --book-id zhang-criminal-law-6e-v2 --pdf-page 576 --printed-page 1397
python -m legal_ai page-map confirm --book-id zhang-criminal-law-6e-v2 --pdf-page 748 --printed-page 1569
python -m legal_ai page-map confirm --book-id klunzinger-bgb-general-zh --pdf-page 36 --printed-page 4
python -m legal_ai page-map confirm --book-id klunzinger-bgb-general-zh --pdf-page 273 --printed-page 241
python -m legal_ai page-map confirm --book-id dworkin-laws-empire-zh --pdf-page 66 --printed-page 56
python -m legal_ai page-map confirm --book-id dworkin-laws-empire-zh --pdf-page 244 --printed-page 234
python -m legal_ai page-map confirm --book-id rousseau-social-contract-zh --pdf-page 81 --no-visible-page
python -m legal_ai page-map confirm --book-id rousseau-social-contract-zh --pdf-page 133 --no-visible-page
```

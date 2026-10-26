# 合同法第八版 OCR 边界页面审计

> 执行日期：2026-08-11  
> 机器可读报告：[ocr_boundary_contract-law-8e.json](ocr_boundary_contract-law-8e.json)  
> 前置抽样人工确认：[private/ocr_manual_review_contract-law-8e.json](private/ocr_manual_review_contract-law-8e.json)

## 目的与输入

本次只补充随机正文试点未覆盖的版式边界，不扩大到全书 OCR。选择 PDF 第 1、2、518 页，分别覆盖封面、CIP/出版信息和空白末页。所有页面使用 PyMuPDF 1.28.2、300 DPI、RapidOCR 3.9.2 与 ONNX Runtime 1.28.0；每页上限 90 秒、最多两次尝试。

## 结果

| PDF 页 | 页面性质 | OCR 状态 | 块数 | 平均置信度 | 处理结论 |
|---:|---|---|---:|---:|---|
| 1 | 封面、分散标题/署名版式 | `generated` | 23 | 0.983541 | 未来应作为 `frontmatter`，不生成检索证据 |
| 2 | CIP 与出版信息，多块密排 | `generated` | 35 | 0.983812 | 未来应作为 `frontmatter`，不生成检索证据 |
| 518 | 空白末页 | `failed`：`ocr_empty_result` | 0 | — | 未来应作为 `blank`，错误可见且不伪装成成功空页 |

源 PDF SHA-256 在执行前后保持一致；三页 JSONL 契约均通过，成功页不含 Unicode 替换字符。

## 版式判断

- 第 1 页的封面大标题被拆成多个 OCR 块，符合稀疏、装帧式版面的预期；这不影响正文 OCR 质量评估，但也说明封面不得进入正文检索。
- 第 2 页包含出版元数据、横线分区和多块对齐信息；OCR 生成可审计文字块，但其用途仅限书目核对，不应作为法学论证证据。
- 第 518 页几乎全白，仅有扫描边缘痕迹。`ocr_empty_result` 是正确的失败可见行为；不应重试、更不应填充虚假文本。

## 结论与门禁

随机正文样本与本次边界样本共同表明：单栏正文可用、前置材料可被识别并隔离、空白页失败可见。所有 OCR 成功页仍为 `ocr_unverified`；本轮没有写入 `process`、SQLite、FTS、证据包或 Obsidian 笔记。

进入统一处理前，仍需实现并审计明确的 `frontmatter/blank` 页面处置规则，确保第 1、2、518 页不会被转换成可检索正文。不能因本报告通过而直接全量 OCR 或开启 `contract-law-8e`。

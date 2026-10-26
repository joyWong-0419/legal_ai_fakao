# P0–P1 实施报告

生成日期：2026-07-09

## 实施范围

已完成规划中的 P0 项目基线与 P1 逐页抽取存证。当前实现不调用模型、不使用
OCR、不接入云服务、向量数据库或 Web UI，也不修改 `book/` 下的原始 PDF。

## 已处理书籍

| book_id | PDF 页数 | SHA-256 |
|---|---:|---|
| `hart-concept-of-law-3e-zh` | 231 | `bd511d08b614387e140d36c00db1bfec80ffd65091c4871c3a57a9d04df73846` |
| `zhang-criminal-law-6e-v1` | 869 | `bf38ca60fe5077da8b71364f168b64d50b99a7849e83ac9bfca79a58b3157478` |
| `zhang-criminal-law-6e-v2` | 859 | `4236a5d04618ae4be30c2d24367e09a37a10d05f780bb5a7272ba4244dbcbef4` |
| `klunzinger-bgb-general-zh` | 285 | `cc06ec49de0e1af7c431c89f70e30a814464ded17bf9d20cef5f600daeb45a72` |
| `dworkin-laws-empire-zh` | 365 | `8909bd54dadf49f7bb2b2cc801a87ab9ca8dac905bd67d9840ee2e8d98c6a930` |
| `rousseau-social-contract-zh` | 148 | `62d8fb24065b253dee278281674395c00df832b129d9890ca0f69a8e608fc0ef` |

合计：6 本书，2757 个 PDF 页面，2757 条逐页记录，抽取错误 0 条。

## 产物

- `legal_ai/config/books.json`：六本书的人工书目配置。
- `legal_ai/reports/inventory.json`：路径、页数、SHA-256、法域、学科和来源类型审计。
- `legal_ai/data/raw/pages/<book_id>.jsonl`：逐页原始抽取文本、页级哈希和错误字段。
- `legal_ai/data/raw/outlines/<book_id>.json`：PDF 书签结构和页码合法性。
- `legal_ai/reports/extraction_quality.json`：空页、近空页、乱码、重复页和书签异常统计。

`legal_ai/data/` 已被 `.gitignore` 排除，全文衍生数据只保留在本地个人环境。

## 质量结果

| book_id | 空页 | 近空页 | 抽取错误 | 书签异常 |
|---|---:|---:|---:|---:|
| `dworkin-laws-empire-zh` | 3 | 0 | 0 | 0 |
| `hart-concept-of-law-3e-zh` | 1 | 0 | 0 | 0 |
| `klunzinger-bgb-general-zh` | 9 | 5 | 0 | 1 |
| `rousseau-social-contract-zh` | 1 | 0 | 0 | 0 |
| `zhang-criminal-law-6e-v1` | 8 | 4 | 0 | 0 |
| `zhang-criminal-law-6e-v2` | 5 | 1 | 0 | 0 |

已知异常：

- Hart 书签解析存在 22 条 pypdf 警告，已捕获进质量报告，不影响逐页文本记录。
- Klunzinger 有 1 条书签无法映射到合法 PDF 页，已标记为无效书签。
- 当前阶段不推测印刷页码；印刷页校准留到 P3。

## 验收命令

```powershell
python -m legal_ai --help
python -m legal_ai inventory
python -m legal_ai ingest --all
python -m unittest discover -s legal_ai\tests -v
```

最近一次完整测试：11 个 `unittest` 用例通过。

## 下一阶段入口

进入 P2 时应新增：

- 版本化规范化规则。
- 句子切分与 offset 回指。
- 不跨 PDF 页的 chunk 生成。
- 章节路径到页面的映射。
- 句子、chunk ID 稳定性测试。

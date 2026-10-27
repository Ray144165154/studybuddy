# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

## [0.3.0] - 2026-10-27

这一版来自一次真实测试：用一本 422 页的扫描版《概率论基础》。

### 新增

- **`Library.add_document()`**：导入任何已抽取好的文本（OCR 结果、EPUB、课堂笔记），
  不再只认 PDF。
- **`studybuddy add-text pages.jsonl -t 书名`**：把 OCR 结果直接灌进书库。
- **`scripts/ocr_macos.swift`**：macOS Vision OCR 工具，中文识别质量好、无需额外安装；
  422 页约 2.5 分钟（150 DPI）。
- **导航页识别**：目录页与索引页会被标记并从检索中排除（`include_nav=True` 可查看），
  否则它们包含全书标题，几乎会霸占所有查询。

### 修复

- 目录页的每一行都匹配"章节标题"模式，导致整页内容被当作标题吞掉、且把后续页面的
  小节状态污染成目录条目。
- OCR 结果绕过文本清洗，产生只含页码（`•388•`）的空片段，并残留页眉装饰字符。
- 页码清洗支持 `•30・`、`·30·`、`- 30 -` 等扫描件常见形式。
- 小节标题末尾的 `・` 等装饰字符会被去掉。

### 变更

- 测试 206 → **228**（新增 `add_document`、`add-text`、导航页识别与过滤、OCR 页码清洗）。

## [0.2.0] - 2026-10-27

### 新增

- **混合检索（可选）**：接任意 OpenAI 兼容的 `/embeddings` 端点后，词法 BM25 与向量
  余弦相似度用 Reciprocal Rank Fusion 融合。解决「问法和书上写法不一样就搜不到」的问题。
- `Embedder` 客户端：批处理、重试（尊重 `Retry-After`）、`dimensions` 截断、Ollama 兼容。
- `EmbeddingIndex`：base64 float32 持久化到 `.studybuddy/embeddings.jsonl`，模型变更自动重建。
- `studybuddy index [--force]` 建立向量索引；`studybuddy ask --no-semantic` 关闭混合检索。
- `studybuddy stats` 显示向量索引状态。
- 语义检索失败时自动退回词法检索并在 `Answer.warnings` 中说明，不会让提问失败。

### 修复

- 按教材过滤时，语义召回一侧错误地传入了「书 ID」而非「chunk ID」，导致带 `--book`
  的混合检索丢失全部向量结果。

### 变更

- 测试 156 → 206，新增 31 个 embedding 单元测试与 16 个混合检索/降级集成测试。

### 修复

- **中文 PDF 字符合成**：部分 PDF 生成器把汉字映射到 Kangxi 部首区
  （`⾃` U+2F83 而非 `自` U+81EA）或 CJK 兼容区，导致中文检索失效。
  现在在抽取阶段做针对性归一化，不影响全角标点与排版。

### 新增

- **中英文双语验证**：新增真实中文 PDF 夹具
  （`tests/fixtures/zh_sample.pdf`）与 12 个双语测试，覆盖抽取、章节识别、
  中文检索、部首写法的查询、以及中英文教材混排。

## [0.1.0] - 2026-10-27

首个公开版本。

### 新增

- **PDF 教材导入**：按页解析、清洗页眉页脚、自动识别中文章节标题
  （`第X章`、`3.1`、`Chapter N`），分块时保留书名/页码/小节。
- **跨教材检索**：中文用字符二元组 + BM25，无需 jieba 等分词依赖；支持按教材过滤
  与单本上限，避免一本书霸占上下文。
- **带页码引用的回答**：每个片段带 `[n]` 编号，回答末尾列出来源，可直接翻书核对。
- **长期记忆**：学生画像、薄弱点/偏好事实、历史提问，全部落在
  `.studybuddy/memory.json`，可在后续会话中被召回并注入 prompt。
- **间隔重复**：每个知识点有掌握度与下次复习日期（SM-2 简化版），
  `studybuddy review` 直接给出今天该复习什么。
- **学习功能**：答疑、复习提纲、自测出题、易混概念对比。
- **多模型**：任何 OpenAI 兼容端点（DeepSeek / OpenAI / OpenRouter / Ollama / vLLM），
  带重试、退避、超时与流式。
- **CLI**：`add / books / remove / ask / quiz / summary / compare / review / grade /
  memory / profile / stats`。
- **141 个离线测试**、离线端到端 demo（不需要 API key）、CI（3.10–3.13：ruff +
  mypy --strict + 测试 + 构建 wheel）。

### 设计说明

- 使用 pypdf 解析 PDF；其余全部为标准库。
- 教材解析失败（扫描版、加密）会明确报错并提示 OCR，而不是静默返回空库。
- 记忆抽取失败绝不影响回答：抽取是一次额外调用，异常被吞掉并降级。

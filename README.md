# studybuddy 📚

**上传你的教材 PDF，跨多本书答疑，每个答案都能翻到具体第几页。它还会记住你哪里薄弱，并按遗忘曲线提醒你复习。**

[![CI](https://github.com/Ray144165154/studybuddy/actions/workflows/ci.yml/badge.svg)](https://github.com/Ray144165154/studybuddy/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-1%20(pypdf)-brightgreen)](pyproject.toml)

---

## 为什么做这个

通用聊天机器人不知道你的教材讲到哪里，也不会告诉你答案在第 143 页第 3 段。
而教材问答工具往往只做「检索 + 回答」，答完就忘——下次你还得重新解释自己哪里不会。

`studybuddy` 想把三件事一次做对：

1. **答案必须有出处** —— 每个结论后面跟 `[1]`，末尾列出《书名》第几页第几节，你能直接翻书核对。
2. **要能跨教材** —— 「拉格朗日中值定理和柯西中值定理的关系」可能同时需要高数书和数分书。
3. **要记得住你** —— 你薄弱的知识点、偏好的讲解方式、上次问过什么，跨会话保留，并按遗忘曲线安排复习。

**中英文教材都支持**：中文按字符二元组索引（不需要分词器），英文按词索引（目录页与索引页会被自动识别并排除，否则它们会霸占几乎所有查询）；复合问句会按主题拆分，并额外按**小节标题**召回章节首页，避免"定义在章首、习题被搜到"的常见错位；
章节标题同时识别 `第三章`、`3.1`、`Chapter 3`、`Section 2.1` 四种写法。
中文 PDF 常见的字符合成问题（Kangxi 部首 `⾃` 代替 `自`）会被自动归一化，
否则「自变量」永远搜不到「⾃变量」。

## 效果

```text
$ studybuddy ask "导数的定义是什么？拉格朗日和中值定理什么关系？"

导数的定义是函数增量与自变量增量之比的极限 [2]，它刻画的是瞬时变化率。

拉格朗日中值定理是柯西中值定理的特例 [1]，所以考试里遇到“存在 ξ 使 f'(ξ)=…”
时，先判断能不能直接用拉格朗日；如果两个函数同时出现，才考虑柯西。

来源:
  [1] 《高等数学》 第40页 · 4.2 中值定理
  [2] 《高等数学》 第12页 · 3.1 导数的概念
```

```text
$ studybuddy memory

画像: 专业: 计算机科学与技术；年级: 大二；在修课程: 高等数学、线性代数

记住了 2 条:
  f0001 薄弱点: 中值定理部分比较薄弱，需要先复习罗尔定理
  f0002 偏好: 偏好先看证明再看例题

在跟进的 2 个话题:
  导数的定义: 见 1 次, 对 0 / 错 0, 掌握度 0%, 下次 2026-10-28
  中值定理:     见 1 次, 对 0 / 错 0, 掌握度 0%, 下次 2026-10-28
```

## 5 分钟上手

```bash
git clone https://github.com/Ray144165154/studybuddy.git
cd studybuddy
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

export STUDYBUDDY_API_KEY=sk-...                       # 或 DeepSeek / Ollama
export STUDYBUDDY_BASE_URL=https://api.deepseek.com/v1
export STUDYBUDDY_MODEL=deepseek-chat

# 1. 导入教材，可以一次传多本
studybuddy add 高等数学.pdf --course 高等数学
studybuddy add 线性代数.pdf --course 线性代数

# 2. 告诉它你是谁（可选，但个性化效果明显）
studybuddy profile --major 计算机科学与技术 --course 高等数学 --pref 先看推导

# 3. 提问
studybuddy ask "矩阵乘法为什么不满足交换律？"
```

不想配 API key？先跑离线演示，它会启动一个假的模型服务，把完整流程走一遍：

```bash
make demo
```

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `studybuddy add <pdf...> --course 高等数学` | 导入一本或多本 PDF，自动解析分页 |
| `studybuddy books` | 列出已导入教材 |
| `studybuddy ask "问题"` | 跨所有教材检索并回答，带页码引用 |
| `studybuddy ask "问题" --book 高等数学` | 限定在某本教材里问 |
| `studybuddy ask "问题" --rewrite` | 先让模型改写检索式（多一次小调用，难问题更准） |
| `studybuddy index` | 建立向量索引，启用混合检索（可选） |
| `studybuddy add-text pages.jsonl -t 书名` | 导入已抽取的文本（OCR 结果、笔记） |
| `studybuddy ui` | 打开本地网页界面（浏览器里提问、导入、看记忆） |
| `studybuddy quiz --book 高等数学 -n 5` | 生成 5 道理解题（`-a` 显示答案） |
| `studybuddy summary --book 高等数学 --chapter 3` | 第 3 章复习提纲 |
| `studybuddy compare "导数" "微分"` | 易混概念对比表 |
| `studybuddy review` | 按遗忘曲线给出今天该复习的题 |
| `studybuddy grade "中值定理" --correct` | 记录自测结果，更新掌握度与下次复习时间 |
| `studybuddy memory` | 查看记忆（`--forget f0001` / `--clear`） |
| `studybuddy profile --major ...` | 设置学生画像 |
| `studybuddy stats` | 书库与记忆概况 |

## 支持的模型

任何 OpenAI 兼容端点都可以，一个客户端搞定：

| 服务 | `STUDYBUDDY_BASE_URL` | `STUDYBUDDY_MODEL` |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` | 任意 |
| Ollama（本地） | `http://127.0.0.1:11434/v1` | `llama3.1` |
| vLLM / LM Studio | 你的地址 | 任意 |

API Key 解析顺序：`--api-key` → `STUDYBUDDY_API_KEY` → `OPENAI_API_KEY` →
`DEEPSEEK_API_KEY` → `OPENROUTER_API_KEY`。本地端点不校验鉴权时会自动用占位符。

## 跨平台

CI 在 **macOS、Windows、Linux** 三种系统上真跑测试（不只是 Linux）：

| 系统 | 状态 | 说明 |
| --- | --- | --- |
| macOS | ✅ CI | 附带 Vision OCR 脚本（`scripts/ocr_macos.swift`） |
| Windows | ✅ CI | 全流程可用；OCR 建议用 ocrmypdf 或 Mathpix |
| Linux | ✅ CI | 同上，OCR 用 ocrmypdf 最省事 |

代码里没有平台分支：路径全部用 `pathlib`，不调用外部命令。唯一需要留意的是终端编码——
输出被重定向时（管道、CI、写文件）会自动切到 UTF-8，避免 Windows 默认代码页把中文打断。

数据目录默认是**当前目录下的 `.studybuddy/`**，三个系统一致。可以改成绝对路径：

```powershell
# Windows
$env:STUDYBUDDY_ROOT = "$HOME\Documents\studybuddy-data\library"
```

```bash
# macOS / Linux
export STUDYBUDDY_ROOT="$HOME/Documents/studybuddy-data/library"
```

## 网页界面

```bash
studybuddy ui          # 打开 http://127.0.0.1:8765
```

一个单页界面，不依赖任何前端框架、也没有新的 Python 依赖（标准库 `http.server`）：

- **提问**：回答正文 + 可点击的 `[1]` 引用角标，来源列表带书名/页码/小节；公式 OCR 失真的
  片段会标「公式可能失真」
- **书库**：导入 PDF（拖拽或选择文件）、按书一键「出题」「提纲」
- **记忆**：画像、薄弱点、跟进话题、今天该复习什么，可一键「记住了/没记住」调整间隔重复
- **设置**：模型、接口地址、API Key。**Key 只存在进程内存里，不写磁盘**，重启即失效

默认只监听 `127.0.0.1`；界面是本地单用户工具，没有账号体系。

## 混合检索（可选）

默认是纯词法检索（BM25 + 中文二元组）：术语准确、完全离线，但**同义改写会漏召回**——
你问「为什么求导和微分是一回事」，某页只写「导数与微分的关系」，词法匹配就找不到。

补语义检索有两条路，都不影响不装的人：

### 本地模式（不需要 API key，推荐）

```bash
pip install "studybuddy[local-embed]"     # 额外装 onnxruntime + 模型运行时
export STUDYBUDDY_EMBED_PROVIDER=local
studybuddy index                          # 首次会下载 ~100MB 模型并缓存
```

用的是 `BAAI/bge-small-zh-v1.5`（中文小模型），CPU 上每页几十毫秒。
国内网络建议先设镜像：`export HF_ENDPOINT=https://hf-mirror.com`。

### 查询改写（可选，补齐"问法不同"）

```bash
studybuddy ask "为什么求导和微分是一回事" --rewrite
```

长问题或"关系/区别"类问题，会先花一次很小的调用把口语改写成教材式检索词
（"导数 微分 关系"），再检索。默认关闭，因为它会多一次 API 调用。

### API 模式（任意 OpenAI 兼容端点）

```bash
export STUDYBUDDY_EMBED_MODEL=text-embedding-3-small
export STUDYBUDDY_EMBED_BASE_URL=https://api.openai.com/v1   # 可以和对话模型不同厂商
studybuddy index
```

> 为什么本地模式不是默认依赖？因为 `fastembed` 会带进 onnxruntime（~50MB）、首次还要
> 下载模型，把 `pip install` 从几秒变成几分钟。**不装就自动退回词法检索**，一行代码都不用改。

之后每次提问会同时跑两路召回，再用 **RRF（Reciprocal Rank Fusion）** 融合：

```text
          ┌── BM25 词法排名 ──┐
问题 ─────┤                   ├── RRF 融合 ── 取前 k ── 带引用回答
          └── 向量语义排名 ───┘
```

为什么用 RRF 而不是加权求和：BM25 分数和余弦相似度量纲完全不同，加权系数几乎没法调；
RRF 只看名次（`1/(k+rank)` 累加），既不用调参，也不会因为某个分数尺度异常而崩掉。

设计上的三个保证：

- **没有 embedding 也能用**：不配置就完全走词法检索，行为和以前一模一样。
- **服务挂了也不崩**：查询向量化失败会自动退回词法检索，并在终端给出提示。
- **可随时关闭**：`studybuddy ask ... --no-semantic`。
- 向量存在 `.studybuddy/embeddings.jsonl`，base64 编码的 float32（每块约 6KB），
  换模型或 `--force` 会重建；删除某本书时自动清理对应向量。

Ollama 本地模型同样支持：

```bash
export STUDYBUDDY_EMBED_MODEL=nomic-embed-text
export STUDYBUDDY_EMBED_BASE_URL=http://127.0.0.1:11434/v1
```

## 扫描版 PDF

扫描件没有文字层，`studybuddy add` 会**明确拒绝并提示**（而不是导入一本空书）：

```
PDFError: no selectable text found in 概率论基础.pdf.
It looks like a scanned book -- run OCR (e.g. ocrmypdf) and try again.
```

OCR 之后用 `add-text` 导入即可。

**公式降噪**：数学扫描件的公式几乎必然被 OCR 认错（`σ`→`o`、`P(A|B)`→`P（AlB）`）。
每个片段会记录两个信号——公式行占比、以及"中文教材里不该出现的假名"密度（`ん`/`で`
通常是被认错的汉字或符号）。命中任一信号时，该片段送进 prompt 会附带一句提示：

> （提示：该片段公式较多，OCR 可能失真；引用公式、条件或数值前，请说明不确定并要求读者核对原书。）

这样模型会主动说"这里看不清、建议核对第 398 页"，而不是把乱码当成公式硬解释。
实测一本 422 页的扫描教材里 29% 的片段被标记。macOS 上仓库自带一个 Vision OCR 脚本（中文识别质量好，
不需要装 tesseract）：

```bash
# 编译 + 识别整本书（422 页约 2.5 分钟）
swiftc -O scripts/ocr_macos.swift -o /tmp/ocr_macos \
  -framework PDFKit -framework Vision -framework AppKit
/tmp/ocr_macos 概率论基础.pdf pages.jsonl 1 422 150

studybuddy add-text pages.jsonl -t "概率论基础" -c 概率论
```

**Windows / Linux** 用 [ocrmypdf](https://ocrmypdf.readthedocs.io/) 生成带文字层的 PDF，
再 `studybuddy add` 即可：

```powershell
winget install --id UB-Mannheim.TesseractOCR   # 或 choco install tesseract
pip install ocrmypdf
ocrmypdf -l chi_sim 概率论基础.pdf 概率论基础-ocr.pdf
studybuddy add 概率论基础-ocr.pdf -c 概率论
```

详见 [scripts/README.md](scripts/README.md)。

## 工作原理

```text
PDF ──► 按页抽文本 ──► 清洗/识别章节 ──► 分块（保留 书名+页码+小节）
                                              │
                                              ▼
问题 ──► 中文二元组 BM25 检索 ──► 片段 [1][2][3] ──┐
   ├──► 复合问句拆分（"A 与 B 的关系"）─────────────────┤
   └──► 小节标题匹配（把章节首页的"定义"拉进来）──────────┘
   │                                              ├──► prompt ──► 模型 ──► 带引用回答
   └──► 记忆召回（薄弱点/偏好/历史） ─────────────┘         │
                                                          ▼
                                        记忆抽取（知识点 + 事实）──► memory.json
```

代码结构（`src/studybuddy/`，按阅读顺序）：

| 文件 | 职责 |
| --- | --- |
| [`pdf.py`](src/studybuddy/pdf.py) | pypdf 抽取、页眉页脚清洗、扫描版报错提示 |
| [`chunk.py`](src/studybuddy/chunk.py) | 章节识别、分块、保留页码与小节 |
| [`index.py`](src/studybuddy/index.py) | 中文二元组分词 + BM25 |
| [`store.py`](src/studybuddy/store.py) | 书库持久化、跨教材检索、按书去重 |
| [`memory.py`](src/studybuddy/memory.py) | 长期记忆 + 间隔重复调度 |
| [`study.py`](src/studybuddy/study.py) | 编排：检索 → 带引用回答 → 记忆抽取 |
| [`cli.py`](src/studybuddy/cli.py) | 命令行 |

## 记忆是怎么设计的

记忆不是「把对话存进向量库」这种黑盒，而是四类可读、可删除的结构：

| 类型 | 内容 | 什么时候写 |
| --- | --- | --- |
| `profile` | 专业、年级、在修课程、偏好 | 你显式设置，或导入教材时记录课程 |
| `facts` | 「中值定理薄弱」「偏好先看证明」 | 每次回答后由模型抽取，去重后累加权重 |
| `episodes` | 你问过什么、当时用了哪些书 | 每次问答自动记录 |
| `topics` | 每个知识点的见面次数、对错、掌握度、下次复习日 | 问答命中话题、出题、自评结果 |

几个刻意的设计取舍：

- **写在本地 JSON**：`.studybuddy/memory.json`，能打开看、能删、能拷到另一台机器。
- **不阻塞回答**：记忆抽取是一次额外调用，失败了就跳过，绝不影响你拿到答案。
- **可完全关闭**：`studybuddy ask ... --no-memory` 既不读也不写。
- **遗忘曲线用简化 SM-2**：答对第一次 1 天、第二次 6 天、之后按难度系数递增；答错重置为 1 天并降低难度系数。

## 数据放在哪

```text
.studybuddy/
├── library.json   # 教材清单
├── chunks.jsonl   # 分块索引（一行一块，可 grep）
└── memory.json    # 画像、事实、历史、话题
```

默认在运行目录下，用 `--root` 或环境变量 `STUDYBUDDY_ROOT` 换位置：

```bash
export STUDYBUDDY_ROOT="$HOME/Documents/studybuddy-data/library"
studybuddy books
```

把书库放在 `~/Documents` 这类不会被清空的地方，比留在 `/tmp` 或项目目录里更稳妥。整个目录可以直接打包带走。`.gitignore`
已经把它排除，避免把教材和学习记录误提交。

## 开发

```bash
make install     # 建虚拟环境并安装 dev 依赖
make check       # ruff + mypy --strict + 299 个测试
make demo        # 离线端到端演示
```

测试全部离线：中英文各有一个真实 PDF 夹具（`tests/fixtures/zh_sample.pdf` 由真实
PDF 生成器产出，含 Kangxi 部首），模型用 `ScriptedLLM` 或本地假服务端，不花钱也不联网。

## 路线图

- [ ] 扫描版 PDF 的 OCR 集成与降级
- [ ] 向量检索（可选，接 embedding API），与 BM25 混合排序
- [ ] 网页界面（上传 + 对话 + 记忆面板）
- [ ] 记忆导入/导出，方便换设备
- [ ] 期末模式：按考试范围生成整本书的复习计划

## 参与贡献

欢迎 issue 和 PR，请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。一句话原则：
**答案必须可核对，记忆必须可关闭。**

## 已知限制

- 只支持**有文字层**的 PDF；扫描件会明确报错并提示先做 OCR（OCR 后可用 `add-text` 导入）。
- OCR 会认错数学符号（`σ`→`o`、`𝒜`→`9`），公式类检索效果明显弱于正文。
- 不做混合检索时，中文是纯词法匹配，同义改写（「求导」vs「微分」）可能漏召回；
  配置 embedding 后可以缓解，但 embedding 需要额外的 API（DeepSeek 目前不提供
  `/embeddings`，可指向 OpenAI 或本地 Ollama）。
- 向量索引是一次性成本，但每次提问会多一次 embedding 调用。
- 中文检索用字符二元组，对术语准确但语义泛化能力弱于向量检索。
- 一次问答会发两轮请求（回答 + 记忆抽取），token 成本约为普通问答的 1.5 倍。

## License

[MIT](LICENSE) © 2026 Ray144165154

---

## English TL;DR

`studybuddy` is a textbook-grounded study assistant for university students: upload
PDF textbooks, ask questions across all of them, and get answers with page-level
citations. It keeps a long-term memory of your weak topics and preferences, and
schedules reviews with a simplified SM-2 algorithm. Retrieval is BM25 over CJK
character bigrams (no tokenizer dependency), so it runs fully offline except for the
model calls. Chinese and English textbooks are both supported and tested.
299 offline tests, MIT licensed.

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)

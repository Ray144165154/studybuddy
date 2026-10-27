# 参与贡献

感谢你愿意帮忙。这个项目的范围很聚焦，所以最有用的贡献往往是小的：修一个 bug、
补一个边界情况、让错误信息更清楚、加一个示例。

## 基本规则

1. **答案必须可核对。** 任何会影响回答的功能改动，都要保证引用仍然指向真实页码，
   不允许为了"好看"而编造出处。
2. **记忆必须可关闭。** 任何新增的记忆写入，都要能被 `--no-memory` 或参数关掉。
3. **测试必须离线。** 测试集不联网、不需要 API key；用 `tests/support.py` 里的
   `ScriptedLLM` 和假服务端。
4. **改动行为就要加测试**，用户可见的改动还要更新 README 和 docstring。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

提交前请确保：

```bash
make check     # ruff + mypy --strict + 235 个测试
make demo      # 离线端到端演示还能跑
```

CI 会在 Python 3.10 ~ 3.13 上跑同样的命令，所以别用 3.10 之后才有的语法
（`match`、`TypeAlias`、`typing.override` 等）。

## 适合上手的 issue

- 支持扫描版 PDF 的 OCR 提示与降级路径
- 检索加上标题/小节的加权
- 为英文教材补一套评测集
- 记忆的导入/导出（换设备迁移）

## 代码风格

- `ruff` 负责 lint 与格式化，`mypy --strict` 负责类型，行宽 110。
- docstring：一行摘要 + 细节；参数用 Google 风格。
- 宁可显式，不要取巧：这个库是要被人读懂的。

## 提交信息

```
feat(retrieval): 中文改用字符二元组索引
fix(memory): 纠正间隔重复的第二次复习间隔
docs: 补充跨教材问答的示例
```

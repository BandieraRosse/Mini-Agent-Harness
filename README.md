# Mini Agent Harness

一个用于学习 Agent / Codex 核心的极简 Python 实现。只使用 Python 标准库，通过 DeepSeek 的 Chat Completions HTTP API 实现：

## 运行

需要 Python 3.10+。把 API Key 放入同目录的 `.deepseek_api_key`（该文件已被 `.gitignore` 忽略）：

```bash
cp .deepseek_api_key.example .deepseek_api_key
# 编辑 .deepseek_api_key，填入真实 key
python3 agent.py
```

示例输入：

```text
请读取 README.md 并告诉我第一行是什么
```

CLI 只显示交互摘要。固定上下文分别位于 `INSTRUCTIONS.md`、`AGENTS.md` 和 `tools.json`。每次用户任务会在 `log/` 下生成一个 JSON 日志文件。

一次模型请求的逻辑结构是：

```text
Instructions: INSTRUCTIONS.md
Input: AGENTS.md + conversation/tool history + current user prompt
Tools: tools.json
```



## 日志阅读器

查看一个日志的全部 round：

```bash
python3 log_reader.py log/20260821_212357_549681+0800.json
```

阅读器默认使用摘要模式，只显示上下文来源、字符长度、消息摘要和关键事件。使用 `--verbose` 展开指定日志中的完整上下文、history、tool schema、reasoning、tool call、observation、模型响应和 usage：

```bash
python3 log_reader.py log/20260821_212357_549681+0800.json --round 2 --verbose
```

只查看指定 round：

```bash
python3 log_reader.py log/20260821_212357_549681+0800.json --round 1
```

阅读器会用不同颜色区分 request、response、tool call、observation、reasoning 和 usage；设置 `NO_COLOR=1` 可以关闭颜色。

CLI 会把 usage 显示为易读格式：

```text
[Round 1] deepseek-v4-flash
tokens: 434 in (384 cached) | 404 out (45 reasoning)
```

摘要中的不同类型信息使用 ANSI 颜色区分；通过管道输出或设置 `NO_COLOR=1` 时自动关闭颜色。

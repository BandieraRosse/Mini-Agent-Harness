# Mini Agent Project Context

MiniAgent is a lightweight terminal coding tool focused on reliable execution, efficient tool calls, and convenient daily use. Codex is a reference for core capabilities and interaction quality.

这是仓库开发 Agent 的入口。项目以轻量、专业、可靠和高效为目标，逐步向 Codex 的核心工具能力靠拢。保持实现易维护，按实际需要引入抽象和依赖；教学演示不再是设计目标。模型接入与工具执行保持边界，便于后续扩展 OpenAI API。

## 开始工作

1. 阅读本文件；项目使用方式见 [README.md](README.md)，完整文档导航见 [docs/README.md](docs/README.md)。
2. 根据任务选择下面的专题，先读取相关实现与测试，再修改。无需一次读取全部文档。
3. 开发流程、验证方式和文档更新规则见 [开发指南](docs/development.md)。

## 按任务导航

| 任务 | 先读文档 | 主要代码 |
| --- | --- | --- |
| 理解 Agent 循环、上下文、会话和恢复 | [架构](docs/architecture.md) | `miniagent/context.py`、`core.py`、`messages.py`、`budget.py`、`api.py`、`sessions.py` |
| 配置模型、ChatGPT 登录、启动与排错 | [使用指南](docs/usage.md) | `miniagent/config.py`、`settings.py`、`cli.py`、`chatgpt_auth.py`、`responses.py` |
| 文件工具、补丁、命令和后台任务 | [工具参考](docs/tools.md) | `miniagent/tools.py`、`processes.py`、`security.py` |
| 终端显示、输入、快捷键和审批 | [终端交互](docs/terminal.md) | `miniagent/ui.py`、`input.py`、`presentation.py` |
| 构建、分发、安装和升级 | [分发与安装](docs/distribution.md) | `scripts/`、`pyproject.toml` |
| 了解已有验证及其局限 | [验收记录](docs/validation.md) | `tests/` |

## 开发约定

- Python 3.10+；核心优先使用标准库，终端直接依赖 `prompt-toolkit`。新增依赖或抽象需改善可靠性、效率或维护成本，保持安装和分发轻量。
- `miniagent/` 是实现，`agent.py` 是兼容入口，`tests/` 是自动验证，`docs/` 是专题说明。
- 保持工具调用与 `tool_call_id` 对应的结果完整；恢复会话不能重放历史命令。新增工具同步维护 schema、参数校验、分派和相关测试。
- 使用显式消息阶段区分中间输出与最终答复；不能以没有 tool call 判定结束。必需后台任务的终态必须先返回给模型。所有模型默认共用 256k token 估算窗口。
- 当前不实现交互式子进程 stdin/PTY 或安全沙箱；按轻量工具的核心执行能力推进。
- 文件修改和命令执行沿用现有审批边界；不要把 Shell 描述为安全沙箱。密钥不得写入日志、会话、文档或提交。
- 行为变化同步更新对应专题；导航变化同步更新本文件与文档索引。验收记录只写实际执行过的验证，并区分历史记录与当前结果。
- 根据改动运行相关测试；完整回归命令为 `python -m unittest discover -s tests -v`，提交前检查 `git diff --check`。

## Windows / PowerShell 文本编辑

- 本仓库主要在原生 Windows / PowerShell 下开发，以下约定供 Codex 等开发 Agent 遵循。源码、Markdown、JSON、TOML 等文本按 UTF-8 处理，不依赖 Windows PowerShell 5.1 的默认编码。
- 使用 `Get-Content`、`Set-Content`、`Out-File` 处理文本时显式指定 UTF-8；不要用默认编码的重定向写入仓库文件。PowerShell 5.1 的 `-Encoding UTF8` 会写入 BOM；写回时保留原文件 BOM 状态，必要时使用显式配置编码的 .NET 或 Python 文件 API。
- 不要将含中文或其他非 ASCII 字符的 PowerShell here-string 经管道传给 `python -` 等原生程序。管道标准输入与文件编码是不同边界，仅指定 Python 文件编码不能防止字符损坏；此类脚本优先用 `apply_patch` 创建 UTF-8 脚本文件后执行。
- 小范围编辑优先使用 `apply_patch`。脚本化修改优先使用结构化定位或短 ASCII 锚点，避免经 PowerShell 传递长中文段落做精确替换。Python 读写显式指定 `encoding="utf-8"`，已有 BOM 时使用 `utf-8-sig`；保留原有 CRLF/LF 和 BOM，注意文本 API 的默认换行转换，不做无关的全文件重写或编码规范化。
- 精确匹配或补丁失败后，先以 UTF-8 重新读取相关区域，核对磁盘上的内容后再修改，不猜测原文或反复重试旧文本。发现乱码或问号替换时，停止受影响的写入，先修正传输或解码方式。
- 修改后查看 `git diff` 并运行 `git diff --check`，确认中文可读、改动范围正确，没有意外 BOM、换行变化或无关重写；文档修改另检查相关链接与内容一致性。

## 指令与文档的加载边界

本文件约束本仓库的开发。`miniagent/instructions.md` 是程序内置的运行指令，修改它会影响 MiniAgent 的行为。

MiniAgent 从 `-C` 指定的目标目录加载根 `AGENTS.md`，不存在时回退到 `Agent.md`；不会自动展开 Markdown 链接或递归加载 `docs/`。本文件中的链接是按需阅读入口，具体机制见 [架构](docs/architecture.md)。

# Mini Agent Project Context

This is a deliberately small Agent Harness for learning the relationship between context, model requests, tool calls, observations, and the next model request.

这是仓库开发 Agent 的入口。保持核心循环小而可读，让上下文、模型请求、工具调用、观察结果和下一次请求之间的关系容易追踪。

## 开始工作

1. 阅读本文件；项目使用方式见 [README.md](README.md)，完整文档导航见 [docs/README.md](docs/README.md)。
2. 根据任务选择下面的专题，先读取相关实现与测试，再修改。无需一次读取全部文档。
3. 开发流程、验证方式和文档更新规则见 [开发指南](docs/development.md)。

## 按任务导航

| 任务 | 先读文档 | 主要代码 |
| --- | --- | --- |
| 理解 Agent 循环、上下文、会话和恢复 | [架构](docs/architecture.md) | `miniagent/context.py`、`core.py`、`api.py`、`sessions.py` |
| 配置模型、启动、交互命令与排错 | [使用指南](docs/usage.md) | `miniagent/config.py`、`cli.py` |
| 文件工具、补丁、命令和后台任务 | [工具参考](docs/tools.md) | `miniagent/tools.py`、`processes.py`、`security.py` |
| 终端显示、输入、快捷键和审批 | [终端交互](docs/terminal.md) | `miniagent/ui.py`、`input.py`、`presentation.py` |
| 构建、分发、安装和升级 | [分发与安装](docs/distribution.md) | `scripts/`、`pyproject.toml` |
| 了解已有验证及其局限 | [验收记录](docs/validation.md) | `tests/` |

## 开发约定

- Python 3.10+；核心优先使用标准库，终端直接依赖 `prompt-toolkit`。新增依赖或抽象应有明确用途，避免遮蔽学习所需的执行过程。
- `miniagent/` 是实现，`agent.py` 是兼容入口，`tests/` 是自动验证，`docs/` 是专题说明。
- 保持工具调用与 `tool_call_id` 对应的结果完整；恢复会话不能重放历史命令。新增工具同步维护 schema、参数校验、分派和相关测试。
- 文件修改和命令执行沿用现有审批边界；不要把 Shell 描述为安全沙箱。密钥不得写入日志、会话、文档或提交。
- 行为变化同步更新对应专题；导航变化同步更新本文件与文档索引。验收记录只写实际执行过的验证，并区分历史记录与当前结果。
- 根据改动运行相关测试；完整回归命令为 `python -m unittest discover -s tests -v`，提交前检查 `git diff --check`。

## 指令与文档的加载边界

本文件约束本仓库的开发。`miniagent/instructions.md` 是程序内置的运行指令，修改它会影响 MiniAgent 的行为。

MiniAgent 从 `-C` 指定的目标目录加载根 `AGENTS.md`，不存在时回退到 `Agent.md`；不会自动展开 Markdown 链接或递归加载 `docs/`。本文件中的链接是按需阅读入口，具体机制见 [架构](docs/architecture.md)。

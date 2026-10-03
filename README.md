# MiniAgent

面向本地和远程 SSH 的轻量终端编程 Agent。Python 3.10+，核心零第三方依赖；支持 DeepSeek、OpenAI 及兼容 Chat Completions 的服务。

## 快速开始

```bash
python -m miniagent
# 原入口仍然可用
python agent.py
```

启动时隐藏输入 API key；开发时也会自动读取项目或源码目录中的 `.deepseek_api_key`。切换 OpenAI 使用 `--provider openai`，对应 `.openai_api_key`。密钥不会写入会话或传入子进程环境。

```bash
# 推荐：安装增强终端输入和 miniagent 命令
python -m pip install '.[terminal]'
miniagent -C /path/to/project

# 一次任务，信任本次运行中的命令和文件修改
miniagent -C /path/to/project --trust -p "修复失败的测试并验证"

# 恢复当前项目的最近会话
miniagent --resume

# OpenAI / 内存会话
miniagent --provider openai --no-save
```

## 能做什么

- 流式回答，连续多轮工具调用，读取指令和按需获取项目内容。
- 文件列表、搜索、带行号分页读取；新建文件、精确替换、严格补丁，编辑前展示 diff 并校验文件散列。
- 执行测试/构建，后台任务与分页输出、超时和取消。
- 默认逐次确认，可切换信任；Ctrl+C 中断后继续输入。
- 新建、保存、恢复会话，保留工具调用关系；压缩较早上下文，恢复不重放历史命令。
- `/help`、`/clear`、`/resume`、`/exit` 等命令；可选增强多行输入，普通 SSH 终端也可运行。

默认确认每次修改/命令。`--trust` 只作用于当前运行；Shell 使用当前用户权限，本项目不提供安全沙箱。

## 文档与开发

- [使用指南](docs/usage.md)：安装、密钥、模型、输入、恢复和排错。
- [架构](docs/architecture.md)：上下文、Agent 循环、检查点与权限边界。
- [工具参考](docs/tools.md)：参数、分页、补丁及执行限制。
- [验收记录](docs/validation.md)：自动测试与真实 API 验证。
- [原始需求](plan.md)。

```bash
python -m unittest discover -s tests -v
```

源码集中于 `miniagent/`；`agent.py` 保留兼容入口。新会话在目标项目的 `.miniagent/sessions/`，`log_reader.py` 可以读取新会话及旧版 `log/` 日志。

# MiniAgent

面向本地和远程 SSH 的轻量终端编程 Agent。Python 3.10+，使用一个直接依赖 `prompt-toolkit` 提供终端交互；仅提供 DeepSeek API 和指定 URL + API key 的 GPT API 两种来源。

定位是方便日常使用的编程工具，以可靠执行、工具调用效率和清晰交互为重点，参考 Codex 逐步完善核心功能。两种来源统一使用 Chat Completions 协议，工具执行保持统一。

## 快速开始

Linux/macOS 下可直接运行 `make` 或 `make run`：首次启动自动创建 `.venv` 并安装增强终端依赖，随后运行当前源码。复用 `dist/wheels/` 的校验缓存；缓存缺失或损坏时从官方 PyPI 下载，不依赖 pip 配置的镜像，也不需要安装项目构建依赖。需要 Python 3.10+（含 `venv`、`pip`）和 Make，在交互终端中运行。

```bash
make run
# 可选：启动时逐次审批，或指定目标目录
make run ARGS="--ask -C /path/to/project"
```

也可手动安装并启动：

```bash
python -m pip install -e .
python -m miniagent
# 原入口仍然可用
python agent.py
```

交互启动会显示来源和模型选择（显式指定连接参数时跳过）；启动后输入 `/settings` 配置来源、URL、模型和密钥，或用 `/provider`、`/model` 快速切换。选择自动保存到用户目录，更新程序无需重新配置。API key 隐藏输入，可选择保存到用户目录或仅本次使用；密钥不会写入会话或传入子进程环境。具体路径与旧密钥文件迁移见 [使用指南](docs/usage.md#用户配置与密钥)。

```bash
# 在目标项目中运行
miniagent -C /path/to/project

# 一次任务，信任本次运行中的命令和文件修改
miniagent -C /path/to/project --trust -p "修复失败的测试并验证"

# 恢复当前项目的最近会话
miniagent --resume

# OpenAI / 内存会话
miniagent --provider openai --no-save
```

GPT 服务可在启动菜单输入地址，或使用命令：

```bash
miniagent --provider openai --base-url https://your-server:8444/ --model your-model
```

隐藏输入 API key 后可选择长期保存或仅本次使用。`/model` 查询模型列表并选择，`/model 模型名称` 直接切换。已移除 ChatGPT 登录、账号标签和 OAuth 依赖。

## 分发到 Linux 服务器

项目附带只依赖 Python 标准库的分发服务。先在本项目目录运行：

```bash
python3 scripts/distribute.py --build --bind 0.0.0.0 --port 8765
```

新服务器只需 Python 3.10+，无需 Git、pip 或虚拟环境。将 `SERVER` 替换为分发机器地址：

```bash
curl -fsSL http://SERVER:8765/install.sh | sh -s -- http://SERVER:8765 --add-to-path
export PATH="$HOME/.local/bin:$PATH"
miniagent -C /path/to/project
```

发布包约 630 KiB，附带纯 Python 终端依赖。重复安装命令即可升级；支持 wget、指定安装目录、离线构建和后台托管，见 [分发与安装](docs/distribution.md)。

## 能做什么

- 中间消息与最终答复分离；没有工具调用也可以继续执行，必需后台任务终态未返回时不能收尾。
- 文件列表、搜索、glob 过滤和分页读取，独立读取最多 4 路并行；新建文件、精确替换、唯一上下文补丁，审批后复查内容变化。
- 执行测试/构建，长命令自动返回任务 ID、等待式轮询、输出头尾保留、独立超时和取消。
- 工具失败返回明确错误码和恢复建议，区分审批拒绝、编辑冲突、命令失败及未知结果。
- 默认逐次确认，可切换只读或信任，也可记住本会话的完整命令；Ctrl+C 中断后继续输入。
- 统一 256k token 估算窗口，大工具结果按需分页；自动压缩较早上下文，保留完整档案。
- 会话增量保存与定期检查点，恢复保留工具调用关系，不重放历史命令。
- 工具默认显示带颜色的摘要；Ctrl+T 切换完整参数、输出和 diff，执行中也可切换。
- 输入 `/` 弹出中文提示与补全；`/clear`、`/resume`、`/status`、`/model`、`/permissions` 对齐常用 Codex 操作。
- 中英混合输入按字符边界移动和删除，支持多行及粘贴；`--plain` 保留普通终端模式。

默认确认每次修改/命令。`--trust` 只作用于当前运行；Shell 使用当前用户权限。本阶段不提供交互式子进程 stdin/PTY 或安全沙箱；文件复查也不构成并发事务。

## 文档与开发

- [Agent 开发入口](AGENTS.md)：项目约定、任务导航和按需阅读入口。
- [文档索引](docs/README.md)：文档分工与使用、维护路径。
- [开发指南](docs/development.md)：本地开发、验证和文档维护规则。
- [使用指南](docs/usage.md)：安装、密钥、模型、输入、恢复和排错。
- [终端交互](docs/terminal.md)：摘要/详情、快捷键、中文编辑及 Codex 源码参考。
- [分发与安装](docs/distribution.md)：标准库分发服务、Linux 一行安装、更新和离线使用。
- [架构](docs/architecture.md)：上下文、Agent 循环、检查点与权限边界。
- [工具参考](docs/tools.md)：参数、分页、补丁及执行限制。
- [验收记录](docs/validation.md)：自动测试与真实 API 验证。

```bash
python -m unittest discover -s tests -v
```

源码集中于 `miniagent/`；`agent.py` 保留兼容入口。会话保存在目标项目的 `.miniagent/sessions/`，由 JSON 检查点和 JSONL 增量组成。

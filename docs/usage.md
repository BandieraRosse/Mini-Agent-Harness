# 使用指南

## 启动与模型

```bash
# 源码运行，无第三方依赖
python -m miniagent -C /path/to/project

# 安装命令及增强输入（建议放在虚拟环境或用 pipx）
python -m pip install '.[terminal]'
miniagent -C /path/to/project

# 一次任务；默认逐次确认，自动化需明确授予信任
miniagent -C /path/to/project --trust -p "修复失败测试并验证"

# OpenAI Chat Completions
miniagent --provider openai --model gpt-6-astra

# 其他兼容服务
miniagent --provider custom --base-url https://example.com/v1 --model your-model

# 无保存、普通终端
miniagent --no-save --plain
```

`--base-url` 可填服务根路径或以 `/chat/completions` 结尾的完整接口。只允许 HTTPS；本机 loopback 服务可用 HTTP。自定义服务需使用 `--provider custom`，这样不会误取 DeepSeek/OpenAI 的开发密钥。

当前默认 DeepSeek 模型为 `deepseek-flash`，OpenAI 为 `gpt-6-astra`。模型名可通过 `--model` 覆盖，实际权限由 API 账号决定。程序使用 API key，不使用 ChatGPT/Codex 登录态。

## 密钥

启动会查找当前工作目录、再查找源码/安装目录下的 provider 对应文件：DeepSeek 为 `.deepseek_api_key`，OpenAI 为 `.openai_api_key`，custom 为 `.api_key`。找不到时用隐藏输入提示；不从环境变量读取 API key，不提供会进入命令历史的 `--api-key` 参数。

隐藏输入的密钥只在内存中，用于 Authorization 请求头。不把它写入配置、会话、日志或子进程环境。密钥文件仅为开发便利保留，已经列入 `.gitignore`。文件中仅写一行密钥，不加引号。无 TTY 时隐藏输入不可用，需预先准备对应文件。

## 交互命令

| 命令 | 行为 |
| --- | --- |
| `/help` | 帮助和快捷键 |
| `/new`、`/clear` | 保存旧会话并开始新会话，不改项目文件 |
| `/sessions` | 查看当前项目保存的会话 |
| `/resume` | 列表；`/resume ID` 或 `/resume latest` 恢复 |
| `/save` | 保存；`--no-save` 下提示当前不保存 |
| `/compact` | 立即压缩较早上下文 |
| `/approval ask` | 每次修改和命令运行前确认 |
| `/approval trust` | 信任当前项目的本次运行 |
| `/status` | 模型、目录、权限、消息数和状态 |
| `/paste` | 普通输入下进入多行模式，`/end` 提交 |
| `/exit` | 保存并退出 |

增强输入安装 `prompt-toolkit` 后自动启用：Enter 提交，Alt+Enter 或 Ctrl+J 换行，支持多行粘贴及内存历史。普通输入可用行末 `\` 续行，或 `/paste`。重定向输入不显示交互提示且默认拒绝需要审批的动作；一次任务自动运行使用 `--trust`。

Ctrl+C 中断当前模型生成或命令执行，回到可继续输入的状态；中断会终止本进程的后台任务。Ctrl+D 退出（Windows 普通控制台可用 Ctrl+Z 后 Enter）。新建/切换会话和退出也会清理后台任务。终端保持正常滚动记录，不启用全屏界面；`NO_COLOR=1` 关闭颜色，`--plain` 还关闭增强输入。

## 恢复、排错与日志

```bash
miniagent -C /path/to/project --list-sessions
miniagent -C /path/to/project --resume
miniagent -C /path/to/project --resume SESSION_ID --no-save
python log_reader.py /path/to/project/.miniagent/sessions/SESSION_ID.json
python log_reader.py /path/to/project/.miniagent/sessions/SESSION_ID.json --verbose
```

会话保存 assistant/tool 的完整对应关系。恢复后等待新指令；输入“检查当前状态后继续”即可。历史中的后台 job ID 不可在新进程使用，需要检查实际文件或重新运行必要的验证。强制关机后的未知结果不代表命令未执行。

遇到连接/空闲读取超时可提高 `--timeout`（默认 120 秒）；尚未收到生成内容时，部分网络错误与 429/5xx 最多重试两次。收到内容后不自动重试，不执行不完整工具调用。达到 40 轮限制后可继续输入，或启动时设置 `--max-rounds`。很长的上下文可调整 `--context-chars`，最少 16,000。

修改失败时，错误会显示并交回模型。散列冲突需要重读文件；重复匹配需要提供唯一上下文。命令输出只截取一页，模型可根据 `next_offset` 继续读取。工具细节见 [tools.md](tools.md)。

## 官方协议参考

- [DeepSeek 模型与别名](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [OpenAI 流式调用](https://developers.openai.com/api/docs/guides/streaming-responses)
- [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Codex CLI 交互参考](https://learn.chatgpt.com/docs/codex/cli)

这些链接用于核对协议和交互设计；MiniAgent 是独立实现，不依赖 Codex CLI。

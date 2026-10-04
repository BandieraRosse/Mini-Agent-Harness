# 使用指南

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

## 启动与模型

只有系统 Python 的远程服务器，可以从自建分发服务下载安装；无需 pip 或 Git，见 [分发与安装](distribution.md)。

```bash
# 安装后运行（建议使用虚拟环境或 pipx）
python -m pip install -e .
python -m miniagent -C /path/to/project

# 安装命令及增强输入（建议放在虚拟环境或用 pipx）
python -m pip install .
miniagent -C /path/to/project

# 一次任务；默认逐次确认，自动化需明确授予信任
miniagent -C /path/to/project --trust -p "修复失败测试并验证"

# OpenAI Chat Completions
miniagent --provider openai --model gpt-6-astra

# 指定 GPT 服务 URL
miniagent --provider openai --base-url https://example.com:8444/ --model your-model

# 无保存、普通终端
miniagent --no-save --plain
```

`--base-url` 可填服务根路径、API 前缀（如 `/v1`），或以 `/chat/completions` 结尾的完整接口。GPT 来源填写根路径时自动调用 `/v1/chat/completions`；有自定义路径时保留该前缀。模型选择使用本地列表，不请求网关的 `/models`。只允许 HTTPS；本机 loopback 服务可用 HTTP，证书校验保持开启。

仅提供 `deepseek` 和 `openai` 两种来源；后者表示指定 URL + API key 的 GPT 兼容服务，不需要 ChatGPT 登录。默认模型分别为 `deepseek-flash` 和 `gpt-6-astra`；请通过启动菜单、`--model` 或 `/model` 选择服务实际支持的模型。`login`、`logout`、`login-status` 和 `--account` 已移除。

GPT 本地候选为 `gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`，同时保留当前及本次运行手动选择的模型。候选不代表网关支持情况，可选择“输入其他模型名称…”或使用 `/model 名称`。GPT 的 `/model` 不需要连接或配置密钥即可选择；DeepSeek 仍从服务获取列表。

## 用户配置与密钥

交互启动依次选择来源、输入 GPT 地址、配置 API key 和选择模型。只有 DeepSeek API 与 GPT API（指定 URL + API key）两个选项；`--provider`、`--model` 或 `--base-url` 显式指定时跳过启动菜单。非交互输入不弹菜单。`/settings` 可以修改来源、URL、模型、API key 及查看存储位置。增强终端用方向键和 Enter，Esc 取消；普通交互终端输入编号，留空取消。模型列表失败时仍可手动输入模型名称。

用户目录为 Windows `%LOCALAPPDATA%\MiniAgent`，Linux/macOS `${XDG_CONFIG_HOME:-~/.config}/miniagent`。`config.json` 保存当前来源、各来源模型与地址；`keys/<来源>-<地址摘要>.key` 独立保存 API key。配置不跟随安装版本或项目目录，正常升级不会覆盖。项目指令 `AGENTS.md` 和 `.miniagent/sessions/` 会话仍属于项目。

优先级为本次命令行参数、已保存配置、内置默认值。命令行覆盖本身不写回；交互切换会保存。`--trust` 等权限仅本次运行有效。切换来源会保存旧会话、停止后台任务并开始新会话；同一来源切换模型保留上下文。

API key 隐藏输入后可选「保存到用户目录」或「仅本次运行使用」。保存文件为一行 UTF-8 明文，POSIX 新建目录 0700、文件 0600；Windows 使用用户目录继承的访问权限。文件工具禁止访问用户配置目录，Shell 仍使用当前用户权限。`--no-save` 仅关闭项目会话保存，不关闭配置和显式保存的 key。仅本次使用不会删除此前保存的 key；重启后临时 key 消失。

密钥按来源和接口地址隔离，更换地址不会复用旧 key。默认官方地址的旧 `.deepseek_api_key`、`.openai_api_key` 首次读取时复制到用户目录，不覆盖已有 key。旧 `custom` 配置迁移为 GPT 来源，仅导入接口完全匹配的旧 custom key；旧 ChatGPT 登录来源回退到 DeepSeek，旧 OAuth 文件不读取、不自动删除。无 TTY 时需事先准备对应的一行密钥文件；一次任务的隐藏输入不会自动保存。不读取环境变量，不提供 `--api-key` 参数。

## 交互命令

| 命令 | 行为 |
| --- | --- |
| `/help` | 帮助和快捷键 |
| `/clear` | 保存旧会话、清屏并开始新会话，不改项目文件 |
| `/new` | 保存旧会话并开始新会话，保留终端滚动记录 |
| `/sessions` | 查看当前项目保存的会话 |
| `/resume` | 打开会话选择；也支持 `/resume ID` 和 `/resume latest` |
| `/save` | 保存；`--no-save` 下提示当前不保存 |
| `/compact` | 立即压缩较早上下文 |
| `/model` | GPT 从本地候选选择，DeepSeek 从服务列表选择；`/model 名称` 直接切换，保留上下文 |
| `/settings` | API 来源、URL、模型、密钥保存和存储位置 |
| `/provider` | 选择来源；也支持 `/provider deepseek|openai`，保存选择并开始新会话 |
| `/permissions` | 选择 ask/trust/read-only；`rules` 查看本会话记住的命令，`reset` 清除 |
| `/status` | 模型、目录、权限、消息数和状态 |
| `/paste` | 普通输入下进入多行模式，`/end` 提交 |
| `/quit`、`/exit` | 保存并退出 |

安装包默认带上 `prompt-toolkit`：输入 `/` 显示中文命令菜单，方向键选择、Tab 补全、Enter 执行；Alt+Enter 或 Ctrl+J 换行。中文、英文及常见组合字符使用一致的光标与删除规则。原 `/approval` 作为 `/permissions` 的兼容别名保留。

工具默认显示动作摘要和少量输出；Ctrl+T 切换完整工具参数与结果。执行时可实时切换；输入时 Ctrl+T 打开当前会话的记录，Esc 返回，保留未提交草稿。详情视图可用 PgUp/PgDn 滚动，End 跟随最新输出。`--verbose` 初始启用详情，也适用于输出重定向。

普通输入可用行末 `\` 续行，或 `/paste`。重定向输入不显示交互提示且默认拒绝需要审批的动作；一次任务自动运行使用 `--trust`。

只读分析可以用 `--read-only` 启动，或输入 `/permissions read-only`；该模式不允许文件修改或 Shell 命令，与 `--trust` 互斥。命令审批时按 A 可记住当前完整命令及目录，避免相同命令反复确认；参数或目录变化仍需审批。规则不写入磁盘，新建/恢复会话时清除；`/permissions reset` 可随时清除。只读模式不会自动停止先前已经启动的进程。

模型可以在执行中输出没有工具调用的进度消息，之后继续工作。最终答复会等必需后台任务的终态返回；连续缺少消息阶段标记时会提示协议问题并停止，任务状态为 `stalled`，不会误标完成。阶段标记由程序指令管理，用户无需手动输入。只支持非交互式 Shell 子进程，当前不提供 stdin/PTY 或沙箱。

Ctrl+C 中断当前模型生成或命令执行，回到可继续输入的状态；中断会终止本进程的后台任务。已建立连接的模型流可主动关闭；DNS、建立连接或 TLS 握手阶段仍可能等待 `--timeout`。Ctrl+D 退出（Windows 普通控制台可用 Ctrl+Z 后 Enter）。新建/切换会话和退出也会清理后台任务。

执行和查看记录时使用可滚动的临时全屏视图，执行结束后把选定摘要/详情写回普通终端记录，再返回输入。审批始终显示完整命令或 diff。`NO_COLOR=1` 关闭颜色，`--plain` 关闭增强交互。完整按键说明见 [终端交互](terminal.md)。

## 恢复、排错与日志

模型列表和推理连接错误会区分 TLS 意外断开（SSL EOF）、证书验证、DNS、连接拒绝和超时，不输出底层异常中的代理密码或凭据。SSL EOF 表示 TLS 连接被对端或网络中间环节提前关闭，不能据此判断 key 失效或模型无权限；先检查当前代理/VPN 的运行状态和日志，再重试。自建服务证书必须受系统信任且匹配访问地址；证书失败时需修正服务证书或配置受信任 CA。程序使用 Python 标准库发现的环境/系统代理；更改代理配置后应退出并重新启动 MiniAgent，证书校验始终开启。

```bash
miniagent -C /path/to/project --list-sessions
miniagent -C /path/to/project --resume
miniagent -C /path/to/project --resume SESSION_ID --no-save
```

会话保存 assistant/tool 的完整对应关系。`.miniagent/sessions/SESSION_ID.json` 是完整检查点，旁边同名 `.jsonl` 是其后的增量；恢复时自动合并，显式保存或正常收尾时更新完整检查点。恢复后等待新指令；输入“检查当前状态后继续”即可。历史中的后台 job ID 不可在新进程使用，需要检查实际文件或重新运行必要的验证。强制关机后的未知结果不代表命令未执行。

遇到连接/空闲读取超时可提高 `--timeout`（默认 120 秒）；尚未收到生成内容时，部分网络错误与 429/5xx 最多重试两次。收到内容后不自动重试，不执行不完整工具调用。达到 40 轮限制后可继续输入，或启动时设置 `--max-rounds`。上下文窗口默认 256,000 token，可用 `--context-tokens` 设置，最少 16,384。旧 `--context-chars` 是显式字符预算覆盖，仅用于兼容。

每次模型请求前按统一窗口检查固定指令、活跃消息和工具定义，预留输出额度与 4,096 token 余量。采用估算值，并非具体模型的精确分词；`/model` 不会自动改变窗口。大工具结果优先转为可分页读取的预览，原始结果保留在会话中；仍超预算时生成较早记录的摘要，保留最近至少 6 条消息及完整工具调用组。`/compact` 可手动触发；最新输入或待摘要内容过大时会报错并保留原记录。详细机制见 [架构](architecture.md)。

修改失败时，错误会显示并交回模型。文本或补丁上下文不匹配时需要重读文件；重复匹配需要提供唯一上下文。命令输出只截取一页，模型可根据 `next_offset` 继续读取。工具细节见 [tools.md](tools.md)。

## 官方协议参考

- [DeepSeek 模型与别名](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [OpenAI 流式调用](https://developers.openai.com/api/docs/guides/streaming-responses)
- [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Codex CLI 交互参考](https://learn.chatgpt.com/docs/codex/cli)

这些链接用于核对协议和交互设计；MiniAgent 是独立实现，不依赖 Codex CLI。

# 架构与开发

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

MiniAgent 面向日常编程，优先保证执行可靠性、响应速度和维护成本。当前使用同步的模型/工具调度循环，命令由进程管理器独立运行并可自动让出控制。核心不依赖 SDK、数据库或服务进程。Python 3.10+；终端使用一个直接依赖 `prompt-toolkit`，普通输出仍可使用 `--plain`。

仓库根目录放项目说明、安装配置、`tests/` 和 `docs/`；`miniagent/` 是同一个项目的 Python 源码包，支持 `python -m miniagent` 以及安装后的 `miniagent` 命令。实际被操作的项目由 `-C` 指定，它的 `AGENTS.md` 和 `.miniagent/sessions/` 都属于该目标目录，与程序安装位置无关。

## 模块

| 文件 | 责任 |
| --- | --- |
| `miniagent/cli.py` | 参数、斜杠命令、生命周期与依赖组装 |
| `miniagent/ui.py` | 流式文字、工具状态、diff/命令审批、多行输入 |
| `miniagent/presentation.py` | 同一工具记录的摘要和详情渲染、状态与 diff 颜色 |
| `miniagent/input.py` | 命令注册表与补全、中英混合输入的字符边界 |
| `miniagent/config.py` | provider/model/endpoint 校验、用户配置和独立密钥文件 |
| `miniagent/settings.py` | 两种 API 来源选择、GPT URL 和密钥保存选择 |
| `miniagent/api.py` | 标准库 HTTP、SSE 组装、重试和协议完整性 |
| `miniagent/context.py`、`instructions.md` | 运行环境和项目指令 |
| `miniagent/core.py` | 多轮执行、完成门槛、只读并行调度、上下文压缩 |
| `miniagent/messages.py`、`budget.py` | 消息阶段与展示、256k 预算和结果分页 |
| `miniagent/sessions.py` | JSONL 增量日志、原子 JSON 检查点、会话列表与恢复 |
| `miniagent/tools.py` | 工具 schema、参数验证、搜索与可靠文件编辑 |
| `miniagent/tool_errors.py` | 工具失败的稳定错误码、重试语义与恢复建议 |
| `miniagent/processes.py` | Shell 子进程、后台任务、超时与取消 |
| `miniagent/security.py` | 已知密钥脱敏与流式分片处理 |

工具定义与参数校验放在同一个模块，避免旧版 `tools.json` 与实现失配。`agent.py` 保留启动兼容。旧版根目录 `api.py/context.py/tools.py/INSTRUCTIONS.md` 已迁入包；旧 `log/` 不迁移、不覆盖，当前保存 JSON 检查点与 JSONL 会话增量。

增强终端在主线程接收按键，在单个工作线程执行原来的 Agent 循环。界面从共享记录渲染，Ctrl+T 只改变显示方式；审批通过 Event 等待选择。Ctrl+C 设置取消事件，停止网络读取/命令并等待工作线程结束后再接收下一条任务，避免并行修改。未增加执行中追加任务、多 Agent 或插件等功能。设计来源与边界见 [终端交互](terminal.md)。

## 一轮任务

1. 从工作目录读取 `AGENTS.md`；不存在时读取 `Agent.md`。加入当前 OS、实际 Shell 和目录。不会启动扫描整个仓库。
2. 追加用户消息并保存检查点。
3. 发送系统指令、活跃历史与工具定义。SSE 可交错返回文字、推理字段和多个工具调用的参数分片。
4. 只有完整流结束并且返回正常结束原因后，才接收工具调用。流断开、长度限制和缺失调用 ID 都不执行部分工具。
5. **先持久化 assistant 的全部调用，再执行**。相邻的独立只读调用最多并行 4 个；其余调用按顺序执行。每个成功、拒绝或失败结果都追加为对应 `tool_call_id` 的 tool 消息，立即持久化。
6. 把结果交给模型继续。只有显式 `final_answer` 且必需后台任务的终态已经返回给模型，才结束本轮；没有 tool call 本身不是完成信号。默认每个用户任务最多 40 次模型请求，包括纯中间消息和被暂缓的最终答复。

并行白名单包括目录列表、文件发现、搜索、文件读取和归档结果读取。结果仍按模型给出的调用顺序写入会话；文件修改、审批、命令和轮询保持串行。取消时等待只读线程退出后才允许下一轮。参数错误作为观察返回，模型可修正参数继续工作。

消息分为 `commentary` 与 `final_answer`。前者可以只输出进度文字，随后由运行时提示模型继续；后者必须没有工具调用。当前 Chat Completions 适配采用开头的 `[commentary]` / `[final_answer]` 标记，解析后隐藏标记并保存 `phase`；重新发送历史时还原标记，不发送非标准 `phase` 字段。适配层也能读取可选原生 `phase` / `end_turn` 信号，信号冲突则拒绝响应。未标记的纯文本不会自动算作完成；连续 3 次缺少阶段则停为 `stalled`。明确的中间文字可以流式展示，最终候选先缓冲，过完成门槛才展示。


必需后台任务默认 `purpose=task`。进程还在运行，或已经结束但终态未由 `run_command` / `poll_command` / `cancel_command` 返回时，会暂缓最终答复并给模型 job ID，要求继续检查。被暂缓的答复也不会在恢复界面显示成完成。显式 `purpose=service` 用于需要持续运行的服务，不阻塞收尾；它仍受超时约束，且新建/切换会话、退出和中断会停止服务。任务结束允许如实报告验证失败，不代表运行时能判断答复中所有事实都正确。

这部分参考本地 Codex `6326163`：`codex-rs/protocol/src/models.rs` 的 `MessagePhase`、`core/src/session/turn.rs` 的 `end_turn=false` 后续轮次机制，以及 `core/src/stream_events_utils.rs` 对中间消息和最终消息的区分。采用的是显式阶段与继续执行的机制；必需进程完成门槛和 Chat Completions 前缀协议是 MiniAgent 的适配实现。

命令默认最多等待 10 秒即返回；未完成的进程继续运行，由 `poll_command` 等待新输出或完成，进程 `timeout` 独立计时。搜索/目录扫描检查与 UI、进程管理器共享的取消事件。工具失败通过统一 `error_code`、`retryable`、`next_action` 返回；当前没有失败承诺可以原样安全重试，恢复不重放原则保持不变。

## 会话与恢复

文件位于工作目录的 `.miniagent/sessions/`：`<ID>.json` 是完整检查点，`<ID>.jsonl` 是其后的增量消息及状态。首次保存建立检查点，后续每条消息追加并 flush/fsync；每 32 次追加或显式保存、结束、压缩时，以同目录临时文件、flush/fsync、`os.replace` 更新完整检查点。恢复时合并两者，不能只查看旧检查点来判断最新状态。检查点序号避免“快照已替换、旧日志未清理”造成重复；只忽略崩溃留下的末尾未完成行，完整损坏行或序号断裂报错。兼容旧 JSON 会话，按单写入者使用，不支持多进程同时写同一会话。

POSIX 下检查点和新建日志文件均仅当前用户可读写。密钥不作为配置字段存储，检查点和增量都在写入前脱敏。终端结果写入和日志写入仍在 Agent 主调度线程完成。

进程可能在命令已经产生副作用、但结果尚未落盘时退出。恢复时，缺少 tool 结果的调用会被补成“执行结果未知”，**绝不根据日志重放**。模型必须检查磁盘状态。后台进程不跨 MiniAgent 进程恢复；旧 job ID 只返回未知状态。

`--no-save` 不创建会话目录，不写会话或输入历史。终端增强输入的历史仅驻留内存。恢复已有会话时仍可用 `--no-save` 避免更新原会话。

## 压缩

所有模型默认按 **256,000 token** 窗口处理，由 `--context-tokens` 设置。请求预算计入固定指令、历史和工具 schema，扣除输出额度（默认 8,192）及 4,096 安全余量，默认输入预算为 243,712。标准库估算器按 ASCII 字符约 3 字符/token、其他 UTF-8 内容约 2 字节/token 计算；这不是具体模型的 tokenizer，也不保证服务端实际 token 数不越界。`/model` 不自动改变窗口。旧 `--context-chars` 仅保留为显式兼容覆盖。

每次请求先检查整批工具结果：超预算时改用保留状态、退出码、job ID、首尾摘要和 `result_ref` 的预览；模型可用 `read_tool_result` 分页读取原结果，不重放工具。若预览后仍超预算，较早消息通过无工具模型请求压缩为 Goal / Constraints / Completed work / Important findings / Next steps。保留最近消息，并把切分边界调整到完整 assistant/tool 组之前，避免半组调用。

完整消息仍保存在会话档案中，摘要和预览仅影响下次模型请求。摘要请求也检查预算；请求失败、待摘要记录过大或结果仍太长时保留原上下文并报告问题。非常大的单次用户输入仍需要缩短；当前尚未做跨多个摘要请求的分段压缩。

## 权限边界

默认逐次确认 Shell 命令和文件 diff。`--trust` 或 `/permissions trust` 信任当前运行。`--read-only` 或 `/permissions read-only` 仅暴露读取/轮询工具，并在运行时拒绝未暴露的修改或命令调用；已有后台进程不会因模式切换自动停止，可用 Ctrl+C 停止。审批时按 A 可记住完整命令与工作目录，参数或目录变化需重新批准；含脱敏或显示清理的命令不可记忆。`/permissions rules` 查看、`reset` 清除；规则仅驻留内存，新建/恢复会话时清除。

文件工具禁止越出工作目录、访问密钥及内部数据；Shell 以当前用户身份执行，**没有安全沙箱**。当前也不支持交互式子进程 stdin/PTY；这两项不在本阶段实现范围。

子进程环境过滤 API key 变量及含已知凭据的变量值，输出与会话脱敏。禁止常见的破坏性 Git 命令作为误操作防护，不能把字符串检查视为安全隔离。用户配置目录被文件工具保护；保存的 API key 仍是当前用户可读的文件，Shell 理论上可以读取。需要避免新增持久 key 时，可选择仅本次运行使用。

## 验证与扩展

```bash
python -m unittest discover -s tests -v
python -m compileall -q miniagent agent.py
```

测试使用临时项目和本地 HTTP 服务，覆盖 SSE 断流与多工具分片、重试、密钥泄漏、编辑冲突、补丁、后台进程、超时、中断、会话恢复及压缩，不依赖真实 API。CI 在 Ubuntu/Windows、Python 3.10/3.13 上运行。

新增工具时同步增加 schema、分派函数和错误语义，在有副作用的动作前调用审批。DeepSeek 与指定 URL 的 GPT 来源统一通过 Chat Completions 接入，复用工具执行、审批及进程管理。配置与密钥绑定具体接口；仅本次运行的密钥保存在内存，显式保存才写入用户目录。

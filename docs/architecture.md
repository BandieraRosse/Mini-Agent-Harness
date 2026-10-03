# 架构与开发

MiniAgent 的核心仍然是可读的小型同步循环：上下文 → 模型请求 → 工具调用 → 真实结果 → 下次请求。核心不依赖 SDK、数据库或服务进程。Python 3.10+；终端使用一个直接依赖 `prompt-toolkit`，普通输出仍可使用 `--plain`。

仓库根目录放项目说明、安装配置、`tests/` 和 `docs/`；`miniagent/` 是同一个项目的 Python 源码包，支持 `python -m miniagent` 以及安装后的 `miniagent` 命令。实际被操作的项目由 `-C` 指定，它的 `AGENTS.md` 和 `.miniagent/sessions/` 都属于该目标目录，与程序安装位置无关。

## 模块

| 文件 | 责任 |
| --- | --- |
| `miniagent/cli.py` | 参数、斜杠命令、生命周期与依赖组装 |
| `miniagent/ui.py` | 流式文字、工具状态、diff/命令审批、多行输入 |
| `miniagent/presentation.py` | 同一工具记录的摘要和详情渲染、状态与 diff 颜色 |
| `miniagent/input.py` | 命令注册表与补全、中英混合输入的字符边界 |
| `miniagent/config.py` | provider/model/endpoint 校验和隐藏密钥输入 |
| `miniagent/api.py` | 标准库 HTTP、SSE 组装、重试和协议完整性 |
| `miniagent/context.py`、`instructions.md` | 运行环境和项目指令 |
| `miniagent/core.py` | 多轮执行、每次操作前后保存、上下文压缩 |
| `miniagent/sessions.py` | 原子 JSON 检查点、会话列表与恢复 |
| `miniagent/tools.py` | 工具 schema、参数验证、搜索与可靠文件编辑 |
| `miniagent/processes.py` | Shell 子进程、后台任务、超时与取消 |
| `miniagent/security.py` | 已知密钥脱敏与流式分片处理 |

工具定义与参数校验放在同一个模块，避免旧版 `tools.json` 与实现失配。`agent.py` 保留启动兼容。旧版根目录 `api.py/context.py/tools.py/INSTRUCTIONS.md` 已迁入包；旧 `log/` 不迁移、不覆盖，`log_reader.py` 同时可读旧日志与新会话。

增强终端在主线程接收按键，在单个工作线程执行原来的 Agent 循环。界面从共享记录渲染，Ctrl+T 只改变显示方式；审批通过 Event 等待选择。Ctrl+C 设置取消事件，停止网络读取/命令并等待工作线程结束后再接收下一条任务，避免并行修改。未增加执行中追加任务、多 Agent 或插件等功能。设计来源与边界见 [终端交互](terminal.md)。

## 一轮任务

1. 从工作目录读取 `AGENTS.md`；不存在时读取 `Agent.md`。加入当前 OS、实际 Shell 和目录。不会启动扫描整个仓库。
2. 追加用户消息并保存检查点。
3. 发送系统指令、活跃历史与工具定义。SSE 可交错返回文字、推理字段和多个工具调用的参数分片。
4. 只有完整流结束并且返回正常结束原因后，才接收工具调用。流断开、长度限制和缺失调用 ID 都不执行部分工具。
5. **先保存 assistant 的全部调用，再依次执行**。每个成功、拒绝或失败结果都追加为对应 `tool_call_id` 的 tool 消息，立即保存。
6. 把结果交给模型继续；无工具调用时结束。默认每个用户任务最多 40 轮。

工具采用顺序执行，便于审批及保存执行顺序。模型单次返回多个调用时，每个调用都会得到独立结果。参数错误作为观察返回，模型可修正参数继续工作。

## 会话与恢复

文件位于工作目录的 `.miniagent/sessions/<ID>.json`，包括版本、目录、模型信息、完整消息、压缩摘要、活跃上下文起点与状态。以同目录临时文件、flush/fsync、`os.replace` 保存；POSIX 文件权限为仅当前用户读写。密钥不作为配置字段存储，保存前再次脱敏。

进程可能在命令已经产生副作用、但结果尚未落盘时退出。恢复时，缺少 tool 结果的调用会被补成“执行结果未知”，**绝不根据日志重放**。模型必须检查磁盘状态。后台进程不跨 MiniAgent 进程恢复；旧 job ID 只返回未知状态。

`--no-save` 不创建会话目录，不写会话或输入历史。终端增强输入的历史仅驻留内存。恢复已有会话时仍可用 `--no-save` 避免更新原会话。

## 压缩

按请求序列化后的字符数量触发（默认 60,000，属于保守估计，不是精确 token 计数）。较早消息通过一次无工具模型请求压缩为 Goal / Constraints / Completed work / Important findings / Next steps。保留最近消息，并把切分边界调整到完整 assistant/tool 组之前，避免半组调用。

完整消息仍保存在会话文件中，摘要仅影响下次模型请求。摘要请求失败或结果仍太长时保留原上下文并报告问题，可调大 `--context-chars` 后恢复；不会静默丢弃历史。非常大的单次用户输入需要缩短。

## 权限边界

默认逐次确认 Shell 命令和文件 diff。`--trust` 或 `/approval trust` 只信任当前运行，不持久化权限。文件工具禁止越出工作目录、访问密钥及内部数据；Shell 以当前用户身份执行，**没有安全沙箱**，仍能访问用户账号允许访问的范围。

子进程环境去掉 API key/token/secret 类变量及含当前密钥的变量值，输出与会话脱敏。禁止常见的破坏性 Git 命令作为误操作防护，不能把字符串检查视为安全隔离。开发用密钥文件仍在文件系统中，具有当前用户权限的 Shell 理论上可以读到；生产使用隐藏输入更合适。

## 验证与扩展

```bash
python -m unittest discover -s tests -v
python -m compileall -q miniagent agent.py log_reader.py
```

测试使用临时项目和本地 HTTP 服务，覆盖 SSE 断流与多工具分片、重试、密钥泄漏、编辑冲突、补丁、后台进程、超时、中断、会话恢复及压缩，不依赖真实 API。CI 在 Ubuntu/Windows、Python 3.10/3.13 上运行。

新增工具时同步增加 schema 与分派函数，在有副作用的动作前调用审批。扩展模型协议时保留 `ChatClient.complete()` 边界，Responses API 将来可作为另一适配器加入；目前以计划要求的 Chat Completions 为协议，未接入 ChatGPT 网页登录或 Codex 订阅身份。

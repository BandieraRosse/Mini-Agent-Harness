# 终端交互

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

安装后运行 `miniagent`，或在项目目录运行 `python agent.py`。增强终端使用 `prompt-toolkit`，它已列为默认依赖：

```powershell
python -m pip install -e .
python agent.py
```

## 工具摘要与详情

用户消息、模型消息和工具记录之间保留一行空白。增强输入使用边框、整行背景和醒目的 `›` 提示符，多行输入及 `/` 补全仍保留。

输入时和运行界面底部显示上下文剩余百分比、剩余 token 数及窗口总量，并显示本次进程累计的 input、缓存 input、output、reasoning token 数。上下文按实际待发送消息（含固定指令、工具 schema 和压缩后的历史）估算，默认窗口为 256,000 tokens；这些数字不是服务端精确分词结果。每次模型调用的统计显示在对应回复之后、工具执行之前，摘要和详情模式均可见；压缩调用也计入累计统计。

调用 token 数来自 API usage；服务端未提供的字段显示“未知”，累计数以“+未知”表示部分调用缺少数据。缓存 input 是 input 的子集，reasoning 是 output 的子集，不能重复相加。累计数不从恢复的历史会话重建；`/status` 也可查看统计，`--plain` 保留每次调用统计。

默认显示工具动作、路径或命令、执行状态，以及简短输出。读取与搜索显示范围或数量；修改显示文件和增删行数；命令显示工作目录、退出码和耗时。成功为绿色、执行中为黄色、失败为红色，辅助信息使用灰色；diff 的新增、删除和标题分别使用绿、红、青色。

文件读取尚有后续内容时提示“本次仅读取部分文件；剩余内容可继续读取。”，包括按请求行数分段读取和达到单次字符上限的情况。“完成”表示本次工具调用结束，不表示整个文件已读完。

命令因等待时间到期返回时仍显示“执行中”，之后通过轮询更新结果。日志超过捕获上限时摘要优先显示最新尾部，并标明它与前部分页不连续；详情分别展示分页、尾部和省略区间。失败结果附带恢复建议；仅返回文件名的搜索显示匹配文件数量。参数及偏移语义见 [工具参考](tools.md)。

模型的显式中间消息可以单独出现并继续执行，界面隐藏阶段前缀。最终候选先缓冲，必需后台任务结果未齐时显示继续检查的提示，不展示被暂缓的完成文字；恢复会话时同样隐藏该草稿。完整结束判定见 [架构](architecture.md)。

按 **Ctrl+T** 在摘要和详情间切换。运行中可以直接切换；输入时按 Ctrl+T 打开当前会话记录，原输入草稿会保留。详情包含已收到的完整工具参数、返回值和本次审批预览。恢复会话后，也可以查看历史工具参数及返回值。工具本身的输出分页、捕获上限仍有效，界面会标出限制；详情不会凭空补回未返回的内容。

| 按键 | 行为 |
| --- | --- |
| Ctrl+T | 切换工具摘要/详情；输入时打开记录 |
| ↑ / ↓、PgUp / PgDn | 在运行或记录界面滚动 |
| Home / End | 跳到记录开头 / 跟随最新内容 |
| Esc | 记录界面返回输入；运行中请求中断 |
| Ctrl+C | 运行中中断；输入时取消当前输入 |
| y / n | 批准 / 拒绝当前等待确认的操作 |
| a | 命令审批时批准并记住完整命令及工作目录，仅本会话生效；不用于文件修改或脱敏命令 |
| Enter | 提交输入，或采用当前命令候选并提交 |
| Alt+Enter / Ctrl+J | 插入换行 |
| Ctrl+D | 输入为空时退出 |

审批始终展示命令或修改预览，不受摘要模式影响。中断会停止当前命令及其子进程，当前操作完全结束后才能开始下一轮。已建立连接的模型请求可中断阻塞读取；DNS、TCP 连接和 TLS 握手阶段仍可能等到 `--timeout`，界面会保持“正在停止”。

`--verbose` 从详情模式启动。`--plain` 关闭增强界面和颜色，适合管道、重定向和普通终端；此时可用 `--verbose` 查看详细工具输出。设置 `NO_COLOR` 可以保留增强交互并关闭颜色。

## `/` 命令

在输入开头键入 `/` 即显示命令和中文说明，继续输入会过滤候选。↑/↓ 选择，Tab 补全并留下参数输入位置，Enter 采用当前候选并执行。权限、已获取的模型名以及会话 ID 也有参数补全。

| 命令 | 功能 |
| --- | --- |
| `/clear` | 保存旧会话、停止旧后台任务、清屏并新建会话 |
| `/new` | 保存旧会话并新建会话 |
| `/resume [ID\|latest]` | 选择或指定已保存会话；恢复后等待新指令 |
| `/status` | 查看模型、目录、权限、会话和本次 token 统计 |
| `/model [名称]` | GPT 使用本地候选，DeepSeek 获取服务模型列表，或直接指定名称；保留上下文 |
| `/settings` | 配置 API 来源、URL、模型和密钥；查看用户配置目录 |
| `/provider [来源]` | 切换来源并新建会话；停止旧后台任务，清除会话审批规则 |
| `/permissions [ask\|trust\|read-only\|rules\|reset]` | 切换权限，或查看/清除记住的命令 |
| `/compact` | 压缩较早上下文，完整记录仍保存在会话中 |
| `/help` | 显示命令 |
| `/quit` | 保存并退出 |

兼容旧命令 `/exit`、`/approval`、`/sessions`、`/save`、`/paste`。`/paste` 用于普通输入模式，以单独一行 `/end` 提交。GPT 的启动模型菜单、`/model` 和补全使用本地默认候选，不查询网关；具体候选见 [使用指南](usage.md)。DeepSeek 的列表由 `/models` 接口提供。两种来源均支持手动输入模型名。切换模型不切换 provider 或 API key。

首次交互启动显示来源选择；尚未配置凭据也可进入程序。菜单支持方向键、Enter 和 Esc；`--plain` 交互终端支持编号选择和留空取消，重定向输入仅显示选项。配置文本使用独立输入提示，API key 使用隐藏输入，不加入对话或主输入历史。来源和模型选择自动保存到用户目录，重启继续使用。

增强模式的配置文本通过独立 `PromptSession` 输入，复用主界面的输入和输出设备；`input`、`output` 参数传给会话构造器，避免调用快捷函数 `prompt()` 时发生参数错误。

## 中英混合编辑

左右移动、Backspace 和 Delete 按文本字符边界处理，不把中文的屏幕宽度当作字符串长度。中文逐字删除；拉丁字母、组合重音和常见 emoji 序列也使用一致的边界。Ctrl+W 或 Alt+Backspace 删除前一个英文词、一个中文字，或一段标点，并带走相邻的尾部空白。

实现覆盖常见组合音标、emoji 肤色、ZWJ 序列与旗帜，并非完整 Unicode 分词规范。输入法候选框、尚未提交的拼音和实际字体绘制由终端及系统输入法负责；MiniAgent 处理提交进输入框后的文本。

## Codex 参考范围

本轮直接阅读桌面上的 `codex` 源码，参考提交为 `6326163b9abd7802c0e57be4e326e5f898bbba75`（短版本 `6326163`）。参考的是交互行为与展示结构；MiniAgent 使用 Python 独立实现以下子集：

| 参考源码 | 本项目采用的行为 |
| --- | --- |
| [exec_cell/render.rs](https://github.com/openai/codex/blob/6326163b9abd7802c0e57be4e326e5f898bbba75/codex-rs/tui/src/exec_cell/render.rs) | 工具活动摘要、状态颜色、折叠输出提示 |
| [chatwidget.rs](https://github.com/openai/codex/blob/6326163b9abd7802c0e57be4e326e5f898bbba75/codex-rs/tui/src/chatwidget.rs)、[transcript_view.rs](https://github.com/openai/codex/blob/6326163b9abd7802c0e57be4e326e5f898bbba75/codex-rs/tui/src/transcript_view.rs) | Ctrl+T 查看已有记录与当前工具活动 |
| [bottom_pane/command_popup.rs](https://github.com/openai/codex/blob/6326163b9abd7802c0e57be4e326e5f898bbba75/codex-rs/tui/src/bottom_pane/command_popup.rs)、[slash_command.rs](https://github.com/openai/codex/blob/6326163b9abd7802c0e57be4e326e5f898bbba75/codex-rs/tui/src/slash_command.rs) | `/` 候选说明及常用命令名称 |
| [bottom_pane/textarea.rs](https://github.com/openai/codex/blob/6326163b9abd7802c0e57be4e326e5f898bbba75/codex-rs/tui/src/bottom_pane/textarea.rs) | 字符边界与显示列分离、移动与删除保持一致 |

界面结构保持为“输入 → 执行视图 → 返回输入”：主线程处理按键，单个工作线程运行现有 Agent；同一份记录支持两种显示方式。范围限于常用终端交互，未引入 Codex 的任务排队、多 Agent、PTY stdin、插件、语音或完整全屏编辑器体系。

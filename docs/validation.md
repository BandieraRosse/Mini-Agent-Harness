# 验收记录

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

## 移除本地 HTTP 集成测试（2026-10-04）

按用户要求移除 43 项依赖本地 HTTP 服务的 API、分发、安装和启动集成测试及其服务辅助代码。保留模拟传输错误、安装 PATH 配置和其他离线测试。真实 HTTP/SSE 传输、分发路由与下载安装流程不再由当前自动回归覆盖；下文相关验证属于历史记录。

本机 Linux 在受限沙箱内运行 `python -m unittest discover -s tests -v`：182 项，161 项通过，21 项跳过，无失败，无需网络权限。`compileall` 和 `git diff --check` 通过。本次未调用真实模型 API。

## 构建依赖缓存（2026-10-04）

构建依赖默认缓存在 `dist/wheels/`，保存经 PyPI SHA-256 与纯 Python wheel 校验的下载文件及摘要。后续构建校验缓存后复用，不再请求 PyPI；缺失、损坏、不完整的缓存或新的依赖版本触发下载。显式 `--wheel-dir` 保持离线输入语义。进度写入 stderr，构建命令的 stdout 仍为 manifest JSON。

本机 Linux 完整回归 219 项：218 通过，1 项 Windows PowerShell 专属测试跳过，无失败。9 项构建测试覆盖无网络缓存复用、确定性产物、缓存损坏恢复、版本隔离及不缓存不合法 wheel。终端测试使用 `/tmp` 中现有依赖。`compileall` 和 `git diff --check` 通过。

另通过真实 PyPI 下载填充当前服务器的依赖缓存，测试产物写入 `/tmp`，未替换现有发布包。第二次构建将网络函数设置为一旦调用就报错，仍成功生成完全相同的 manifest 与发布包摘要，用时 0.384 秒。当前命令无需新增参数，后续 `--build` 可直接复用已填充的缓存。

额外以不可用代理运行 `distribute.py --build`，使用临时发布目录与 loopback 临时端口，缓存构建及服务启动共 0.442 秒，`/manifest.json` 返回 HTTP 200；验证后停止临时服务。

## 公网 IP 证书与真实 GPT API（2026-10-04）

在实际网关服务器通过 Certbot 5.8.0 的 standalone HTTP 验证申请 Let’s Encrypt 公网 IP 证书，标识为 `124.221.221.10`，使用 `shortlived` profile。测试环境申请、正式申请和 `renew --dry-run` 均成功。正式证书已部署到现有两个用户级网关服务，系统默认 CA 校验通过；无需客户端安装此前的私有 CA。旧证书与私钥保存在原 TLS 目录的 `before-letsencrypt/`，保持 0600 权限，不写入仓库。

启用 `miniagent-gateway-cert-renew.timer`，每天两次检查续期，开机恢复运行。部署钩子验证证书链、IP 与私钥匹配，重启后检查两个端口的新证书，失败回滚。首次正式部署钩子运行成功，定时续期服务首次检查成功，定时器为 enabled/active。

实际接口检查确认：`https://124.221.221.10:8444/` 是用量面板，`/v1/models` 返回 HTML；模型 API 应使用 `https://124.221.221.10:8443/`。通过 MiniAgent 的真实模型列表及 `gpt-6-astra` 一次任务验证，模型返回 `OK`，退出码 0。验证使用服务器已有 key，仅在内存中读取，没有输出或保存到测试文件。使用临时工作目录、`--plain --read-only --no-save`，未执行模型工具。

当前代理访问本机公网 API 超时；直连验证成功，测试通过 `NO_PROXY/no_proxy` 排除该 IP。此前下节中的证书错误及未完成真实调用描述是 2026-10-03 的历史状态，已由本次部署与验证解决。

## API key 来源简化（2026-10-03，当前结果）

环境为本机 Linux、Python 3.12.3。移除 ChatGPT OAuth、Responses 适配器与 JWT 可选依赖；仅提供 DeepSeek API 与指定 URL + API key 的 GPT API。交互启动选择来源、密钥保存方式和模型；保留 `/model` 切换与上下文，兼容旧 custom 配置和接口匹配的 key，旧订阅配置回退到 DeepSeek。

完整运行 `python3 -m unittest discover -s tests -v`：215 项，214 通过，1 项 Windows PowerShell 专属测试跳过，无失败，约 15 秒。终端依赖 prompt-toolkit 3.0.53、wcwidth 0.9.1 放在 `/tmp`，以 `PYTHONPATH` 加载；下载文件核对 PyPI SHA-256，没有修改系统 Python。新增本地 HTTP 端到端测试验证连续两次交互启动、根 URL 到 `/v1` 路由、模型列表、选定模型的流式推理、临时密钥不落盘；配置测试验证保存、临时覆盖不修改旧 key、迁移和地址隔离。`compileall`、主命令帮助和 `git diff --check` 通过。

对用户提供的 HTTPS 服务做不带密钥的 HEAD 检查，代理与直连均报 `unable to get local issuer certificate`。未关闭证书校验，未向服务发送用户密钥，未完成该服务的真实模型列表或推理验收；需要先修正服务证书链及访问地址匹配，或配置受信任 CA。

以下均为历史阶段的结果；其中 OAuth/Responses 功能已在当前版本移除。

历史验证日期：2026-10-03。实际环境：Windows、Python 3.13.15、Windows PowerShell。

## 真实登录后的连接排查

用户完成本地浏览器授权后，使用 MiniAgent 独立保存的登录向官方 `/v1/models` 请求成功，返回 5 个模型，包含 `gpt-6-astra` 与 `gpt-5.6-sol`。诊断未输出或复制凭据。模型请求经过本机 HTTP 代理；另复现多次 `SSLEOFError / UNEXPECTED_EOF_WHILE_READING`，表示 TLS 连接提前关闭，尚不能归因于代理本身或其上游。请求地址已核对官方 SIWC 文档。

最小推理请求曾返回适配后的完成结果但无正文，随后流结构诊断遭遇 TLS EOF，**尚未完成真实可见答复的端到端验收**。新增对无正文且无工具调用的 Responses 结果的明确报错；不当作成功答复，也不自动重试。连接错误新增 TLS EOF、证书、DNS、拒绝连接及超时分类，避免原先统一提示掩盖原因；不输出底层异常中的凭据，不关闭证书校验。

新增测试覆盖上述错误分类、异常中的秘密不泄漏，以及空完成不成功/不重试。认证 CLI 测试补充临时用户配置目录隔离，避免真实登录后测试访问个人配置。此段是实际网络排查证据，不代表云网关或远程登录已验收。

最终 Windows 全量回归 233 项：229 通过、4 项平台/环境相关跳过，无失败，约 28 秒；`compileall`、`git diff --check` 通过。

## Windows / Linux 补充验证

2026-10-03 在本机 Ubuntu WSL 的 Linux Python 3.14.4 中补跑验证。使用 `/tmp/miniagent-cross-platform-check` 独立虚拟环境安装终端与 OAuth 测试依赖后，完整执行 `python -m unittest discover -s tests -v`：**231 项测试，229 项通过，2 项 Windows 专属测试跳过，无失败**，约 16 秒。跳过项是 Windows DPAPI 集成和 Windows 默认 PowerShell 检查；增强终端、OAuth 合成凭据、本地 HTTP 模型适配及配置交互测试均运行通过。

补充检查修正了首次仅保存 API key 时的 POSIX 父目录权限：先创建 0700 用户配置目录，再创建 0700 keys 目录，key 文件为 0600。新增测试在 Linux 原生临时目录和 umask 022 下验证三者权限。修正后 Windows 配置与设置相关 21 项测试中 20 项通过、1 项 POSIX 专属测试跳过；Windows 此前完整回归结果见下节。`git diff --check` 通过。

此次 Linux 验证使用 WSL，并非远程服务器实机或完整人工终端验收；真实 ChatGPT 浏览器授权、真实订阅推理与无浏览器 SSH 登录仍未验证。下面各节中的“Linux 未验证”描述保留为对应历史阶段的状态。

## 交互设置与用户目录配置

启动后的 `/settings`、`/provider`、`/model` 支持来源、模型和凭据配置；首次交互启动可在没有密钥时进入来源菜单。配置与按来源/地址隔离的 API key 保存到用户目录，默认官方来源的旧 key 首次读取时复制迁移，不覆盖已有用户 key；ChatGPT OAuth 沿用独立存储位置。

本轮运行 `.venv/Scripts/python.exe -m unittest discover -s tests -v`：**230 项测试，227 项通过，3 项环境相关跳过，无失败**，约 31 秒。跳过项仍为两项 Windows 符号链接检查和一项真实 DPAPI 集成检查。新增验证配置重启恢复、命令行覆盖不写回、旧 key 迁移与用户 key 优先、地址变更不复用 key、菜单取消、保存/仅内存 key、选择账号登录、普通终端编号选择、无密钥启动、来源切换不携带旧对话及用户目录文件工具保护。自动测试使用临时用户目录，不迁移真实密钥。

`compileall` 与 `git diff --check` 通过。文档同步说明存储位置、迁移规则和更新保留行为。真实 ChatGPT 登录及推理、Linux 实机仍未验证；本轮未启动浏览器授权或部署云网关。以下 ChatGPT 接入记录保留此前阶段的验证结果。

## ChatGPT 独立登录与 Responses 接入

新增独立 OAuth 登录、账户标签、状态查询和退出，固定官方模型端点；不读写 Codex 凭据。新增 Responses 消息/工具适配，复用原工具执行、审批与会话。项目虚拟环境已实际安装 `.[chatgpt]` 可选依赖。

最终运行 `.venv/Scripts/python.exe -m unittest discover -s tests -v`：**219 项测试，216 项通过，3 项环境相关跳过，无失败**，约 27 秒。跳过项为两项 Windows 符号链接检查和一项沙箱中无法访问用户配置的 DPAPI 集成检查。前一轮在用户配置可用的执行环境运行过真实 DPAPI 加密/解密、认证测试及完整 217 项回归（215 通过、两项符号链接跳过）。之后两次提升权限的最终回归请求因自动审核超时而未启动，最终将认证单元测试的存储编解码与独立 DPAPI 集成测试分开，在沙箱内完成上述 219 项回归；生产存储未降级成明文。

新增覆盖本地 OAuth 回调的 state、PKCE 和注册 ID 校验，真实 RSA 签名及 issuer/audience/expiry/nonce 校验，重新授权的账户匹配和本次权限检查，原子凭据保存与锁互斥，刷新令牌轮换，刷新前后脱敏和子进程环境过滤，服务端撤销失败后的本地清理。模型侧通过本地 HTTP 服务验证 namespace 工具、call ID 配对、加密推理项回传、原生消息阶段、失败/断流不交付调用、账户模型列表，以及 CLI 读取文件后返回最终答复和切换模型。

`compileall`、登录及主命令帮助、无 ChatGPT 可选依赖环境下的主命令帮助、`git diff --check` 和文档本地链接检查通过。**未完成真实 ChatGPT 账户授权或真实 Responses 推理**；账户预览资格、实际模型权限和真实服务端协议兼容性仍需用户登录后验收。未运行 Linux 实机或远程 CI；未实现云网关。当前 Responses 文字在完整响应终态后交付，尚不逐字显示。

以下为此前阶段的历史验证。

## 消息阶段、完成门槛与执行效率

参考本地 Codex `6326163` 的显式消息阶段与继续轮次机制，加入纯文字中间消息、最终答复门槛和恢复展示保护。必需任务未结束或终态未返回时，最终候选不展示、不标记完成；显式长期服务单独处理。统一采用 256,000 token 估算窗口和输出预留，大工具结果转换为可通过 `read_tool_result` 读取的预览。

同时加入唯一上下文补丁、审批后内容复查、最多 4 路独立只读并行、只读权限模式、当前会话完整命令记忆，以及 JSONL 增量保存和周期检查点。交互式子进程和沙箱未实现。

最终运行 `.venv/Scripts/python.exe -m unittest discover -s tests -q`：**198 项测试，196 项通过，2 项因 Windows 符号链接权限跳过，无失败**，用时约 27 秒。`python -m compileall -q miniagent agent.py`、`git diff --check` 和 Markdown 本地链接检查通过。

新增覆盖阶段标记跨 SSE 分片、原生阶段/结束信号、无调用中间消息继续、未标记文本不误结束、真实子进程退出但未观察时拦截最终答复、失败退出码进入下一轮、服务例外、256k 整批输出分页与完整结果读取、读取并行但修改保持有序、只读 schema 和运行时双重限制、完整命令/目录匹配、恢复时隐藏未获接受的最终草稿，以及增量日志截断恢复、损坏记录拒绝、旧日志去重、定期检查点、脱敏和恢复不重放。

会话测试已按“JSON 检查点 + JSONL 增量”核实工具执行之前的持久化内容。当前 token 计数为估算；测试使用本地 HTTP 服务、脚本化模型响应和真实本地工具，本轮没有调用真实模型 API，也未运行 Linux 实机或远程 CI。阶段前缀在真实模型上的遵循率仍需后续集成验收。

以下是前一阶段与历史版本记录。

## 工具执行与搜索升级

项目定位调整为轻量、可靠、高效的日常编程工具。本轮增加命令输出头尾保留、绝对字节游标和省略范围、可配置返回预算、自动让出与等待式轮询；统一工具错误码及恢复建议；搜索支持单文件、glob 过滤、上下文行、文件名模式和取消。

最终运行 `.venv/Scripts/python.exe -m unittest discover -s tests -q`：**178 项测试，176 项通过，2 项因 Windows 符号链接权限跳过，无失败**，用时约 48 秒。`python -m compileall -q miniagent agent.py`、`git diff --check` 和 Markdown 本地链接检查通过。

新增验证覆盖超量日志仍可读取末尾结论、头尾总内存和返回预算、Unicode 绝对偏移及滚动游标跳过、任务让出后只执行一次、等待新输出与完成、等待期间取消、搜索过滤/去重分页/脱敏/取消、拒绝与部分写入的错误分类，以及真实工具结果进入下一次模型请求并保存恢复。终端摘要、安装分发和既有会话测试包含在完整回归中。

回归中修复了一个已有超时测试的时序问题：连接建立采用正常等待，收到实际文本后才缩短读取超时，服务端用事件控制剩余响应，避免 Windows 调度延迟使“部分输出后超时”变成“生成前超时”。未修改生产 API 协议；本轮没有调用真实模型 API，也没有执行 Linux 实机或远程 CI 验证。

以下条目是此前版本的历史验收。

## 单会话文件修改：移除哈希校验

移除文件工具的 SHA-256 返回、`expected_sha256` 参数和审批后内容版本复查。保留唯一文本匹配、严格补丁上下文、审批拒绝不写入、CRLF/权限保留和多文件写入前检查。文件修改不保证并发安全；下文散列冲突与版本复查测试属于历史验收。

运行 `.venv/Scripts/python.exe -m unittest discover -s tests -q`：**166 项测试，164 项通过，2 项跳过，无失败**。`python -m compileall -q miniagent agent.py` 和 `git diff --check` 通过。本轮未执行真实模型 API 验证。

## 0.3.1 分发与安装

新增标准库构建器、HTTP 分发服务和 Linux 安装脚本。真实在线构建从 PyPI 下载两份纯 Python wheel，得到与离线构建完全相同的归档，大小为 641,235 字节（约 626 KiB）。发布包不含密钥、Git、测试、缓存或原生二进制，包含终端依赖及许可证。

在本机 HTTP 服务上通过 Git Bash 实际执行 `curl | sh` 安装到含空格的目录，并运行生成的 `miniagent --version`。使用 `python -S` 启动已安装版本、导入包内 `prompt_toolkit`、`wcwidth` 和 Application 成功，证明运行不依赖 site-packages 或 pip。

自动测试覆盖下载 GET/HEAD 与路由白名单、路径/链接拒绝、发布失败保留 manifest、安装校验、失败时保留旧命令、重复安装、升级、Shell bootstrap 和 PATH 配置。Linux 实机未在本机执行；Shell bootstrap 测试也纳入现有 Ubuntu/Windows CI 测试集。

最终完整测试：**166 项，164 项通过，2 项因 Windows 符号链接权限跳过，无失败**。`compileall`、标准库模式下的服务/安装器 CLI 和 `git diff --check` 通过。

## 0.3 终端交互升级

本轮根据本地 Codex `6326163` 的相关源码实现默认工具摘要、状态颜色、Ctrl+T 详情、斜杠命令补全和中英混合编辑。项目根目录 `.venv` 已通过 `pip install -e .` 安装当前源码及终端依赖；可直接运行 `.venv/Scripts/python.exe agent.py`。

验证包括实际 prompt-toolkit Application 中的键盘输入：执行中展开以前的长工具参数/结果；按屏幕行滚动查看 14,400 字符的中英混合参数中段；取消后继续输入；审批时切换详情再接受、拒绝或中断；查看记录后保留中英文草稿与光标；选择器方向键、Enter 和 Esc。HTTP 测试验证已建立连接的阻塞流可主动取消，进程测试验证取消事件能终止真实子进程树。

命令测试覆盖 `/model` 切换后保留上下文、`/permissions` 与旧别名、`/resume latest`、`/clear` 与 `/new` 的显示区别及 `/status`。真实 DeepSeek `/models` 返回模型列表成功；真实模型只读任务展示了简洁的 Read 摘要并正确回答。

最终运行 `.venv/Scripts/python.exe -m unittest discover -s tests -q`：**142 项测试，141 项通过，1 项因 Windows 符号链接权限跳过，无失败**。`compileall` 和 `git diff --check` 通过。

下面记录的是 0.2 的基础验收；0.3 在此基础上增加上述交互验证。

## 自动验证

使用已安装可选终端依赖的项目内虚拟环境运行：

```powershell
tmp/acceptance-venv/Scripts/python.exe -m unittest discover -s tests -q
```

结果：**97 项测试，96 项通过，1 项跳过，无失败**。跳过项为符号链接越界检查：当前 Windows 账号没有创建符号链接的权限。其余路径检查已执行。普通环境无需安装终端依赖也可运行测试；增强输入用例在没有该可选依赖时跳过。

覆盖内容：

- HTTP/SSE 流式文字、多工具参数交错分片、推理字段、refusal、异常状态、断流、长度上限与重试。
- provider 密钥隔离、隐藏输入、子进程环境过滤、流式跨分片和文件分页前脱敏。
- 新建文件不覆盖、唯一匹配、散列冲突、批准后重查、CRLF/权限保留、严格多文件补丁及写入失败。
- 命令退出码、中文输出、后台任务、分页、超时、取消、Ctrl+C 清理与 Windows 子进程树清理。
- 调用前持久化、中断结果补齐、恢复不重放、压缩保留完整调用组、完整消息归档、损坏会话及 no-save。
- CLI 一次任务、权限拒绝、会话切换，以及增强输入的多行、Ctrl+C 后继续输入和 EOF。

`compileall` 与 `git diff --check` 通过。

## 真实 DeepSeek 验证

使用现有 `.deepseek_api_key`，模型 `deepseek-flash`，只在被忽略的 `tmp/live-smoke/` 临时项目中操作。

1. 临时项目的 `add(a, b)` 故意实现为减法，另有两个断言的 unittest 测试。
2. MiniAgent 自动发现文件，读取代码和测试，执行测试并观察退出码 1。
3. 使用 `read_file` 返回的 SHA-256 调用 `replace_text`，展示 diff，把减法改成加法。
4. 再次运行测试，得到 1 个测试方法通过、退出码 0；没有修改测试文件。
5. 退出并在新进程恢复同一会话，重新读取修复后的文件；后台启动测试，通过 `poll_command` 获得结束状态、输出及退出码 0。
6. 对该会话执行 `/compact`，摘要写入成功，完整 21 条历史消息仍在会话文件中。
7. 本地检查确认会话不含真实 API key，tool call/result 对应关系完整，压缩摘要和边界已保存。

复核实际调用记录时还发现 `**/*.py` 没有包含根目录文件，已修复递归 glob 的零级目录语义，并新增根目录、嵌套路径及 `*?[]` 边界测试。

这些临时文件和会话不进入 Git。真实密钥未输出，也未加入配置或提交。

## 安装验证

在仓库内临时虚拟环境运行 `pip install '.[terminal]'` 成功，构建 wheel 并安装 `mini-agent-harness`、`prompt-toolkit` 及其依赖。在源码目录之外运行安装后的 `miniagent --version` 成功，包内系统指令可以正常加载。原入口 `python agent.py` 保留。

## 尚未实际验证的范围

- OpenAI/custom 使用本地 HTTP 服务验证协议和字段兼容，未使用真实 OpenAI key 发起付费请求。
- 本机没有执行 Linux 实机验证。已提供 GitHub Actions 的 Ubuntu/Windows × Python 3.10/3.13 矩阵，包含安装、测试和命令入口；尚未推送或运行远程 CI。
- 第一版提供审批和误操作防护，未提供操作系统沙箱。Shell 权限、后台任务不跨进程恢复、多文件写入非事务等边界见 [架构](architecture.md) 和 [工具参考](tools.md)。

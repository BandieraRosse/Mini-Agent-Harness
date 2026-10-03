# 验收记录

验证日期：2026-10-03。实际环境：Windows、Python 3.13.15、Windows PowerShell。

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

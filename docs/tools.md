# 工具参考

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

工具定义来自 `miniagent/tools.py` 的 `SCHEMAS`，使用 Chat Completions function calling 格式。所有调用通过 `ToolRegistry.execute(name, arguments)` 返回 JSON 对象；`ok: false` 表示失败，细节来自 `error` 或命令的 `output`、`exit_code`。参数拼写、类型、范围错误也返回工具结果，Agent 可以据此修正调用。

失败结果统一包含 `error_code`、`error`、`retryable`、`next_action`。`retryable` 指能否原样重试；当前统一为 `false`，不影响修正参数、重新读取文件或排查失败后继续工作。保留 `denied`、`declined`、`partial_write`、`uncertain` 等原字段以便兼容。

| 错误码 | 恢复方式 |
| --- | --- |
| `INVALID_ARGUMENT` / `UNKNOWN_TOOL` | 修正参数或选择已提供的工具。 |
| `PATH_DENIED` / `COMMAND_DENIED` | 遵守限制，不通过其他工具绕过。 |
| `NOT_FOUND` / `FILE_EXISTS` / `UNSUPPORTED_FILE` | 检查路径、现有内容和文件格式。 |
| `EDIT_CONFLICT` / `PATCH_CONTEXT_MISMATCH` | 重新读取受影响文件，再生成修改。 |
| `APPROVAL_DENIED` / `APPROVAL_FAILED` | 尊重拒绝，或先解决审批故障。 |
| `PERMISSION_DENIED` | 当前为只读模式；需要用户切换权限，不能绕过。 |
| `COMMAND_FAILED` / `COMMAND_TIMEOUT` / `COMMAND_CANCELLED` | 查看退出码、输出及可能已产生的副作用。 |
| `JOB_LIMIT` / `INVALID_OFFSET` | 等待已有任务，或使用返回的输出偏移。 |
| `UNKNOWN_JOB` / `OUTCOME_UNKNOWN` | 检查实际状态，禁止自动重放。 |
| `PARTIAL_WRITE` / `IO_ERROR` / `EXECUTION_FAILED` / `OUTPUT_CAPTURE_FAILED` | 检查真实状态、权限和部分结果后再决定后续动作。 |

## 文件读取与查找

| 工具及参数 | 行为与继续读取方式 |
| --- | --- |
| `list_directory(path=".", offset=0, limit=100)` | 列出目录，目录优先。`entries` 含 `path`、`type`；用 `next_offset` 翻页。 |
| `find_files(pattern="*", path=".", offset=0, limit=100)` | 对工作区相对路径做 glob 匹配，无斜杠的模式匹配文件名。`**` 可匹配零级或多级目录，`*?[]` 不跨目录；返回 `files`，用 `next_offset` 翻页。 |
| `search_text(query, path=".", case_sensitive=true, offset=0, limit=100, include_glob="*", exclude_glob="", context_lines=0, files_only=false)` | 支持单文件或目录；单行、非空、字面文本搜索。返回文件路径、行号、文本；`next_offset` 用于继续。 |
| `read_file(path, offset=1, limit=200, column=0)` | 返回带行号的 `content`。`offset` 从第 1 行起，`column` 从第 0 个字符起。 |
| `read_tool_result(tool_call_id, offset=0, limit=8000)` | 读取当前会话归档中的完整工具结果，不重新执行。偏移按序列化结果的字符计数；`limit` 为 1–24,000；返回 `content`、`next_offset`、`truncated`、`total_chars`。 |

前三个工具的 `limit` 范围为 1–200，`read_file` 为 1–1000。读取输出最多约 24,000 个文本字符；一行过长时，使用返回的 `next_offset` **和** `next_column` 继续读取同一行。`truncated` 表示还有结果。密钥在分页之前脱敏，字符偏移对应脱敏后的文本。

搜索的 `include_glob` / `exclude_glob` 使用与 `find_files` 相同的 glob 规则，始终匹配工作区相对路径，排除优先。`context_lines` 为 0–5，匹配项中的 `context` 含前后行的 `line`、`text`、`line_truncated`；上下文也脱敏并计入输出预算。`files_only=true` 时返回去重的 `files` 数组，不返回上下文，此时 `offset` 计数匹配文件；默认模式计数匹配行。目录发现、文件搜索和匹配行遍历均响应取消。使用标准库搜索，无新增 `rg` 依赖；翻页仍重新扫描，应优先缩小路径及 glob 范围。

文本文件必须是 UTF-8（可有 BOM），单文件上限为 2 MiB；含 NUL 的二进制内容和无效 UTF-8 会被拒绝。搜索结果每条最多约 1,000 个字符，`line_truncated` 表示该行需要用 `read_file` 查看。搜索跳过的不可读、过大或非文本文件计入 `skipped_files`。

发现文件时默认跳过 `.git`、`.miniagent`、`node_modules`、`__pycache__`、`.venv`、`venv`、`build`、`dist`、`target` 及常见缓存目录、二进制扩展名和符号链接。每次发现最多检查 30,000 个目录项、10,000 个文件，达到上限返回 `scan_truncated: true`；此时应缩小 `path` 范围。分页期间若目录内容变化，结果顺序也可能变化。

## 文件修改

| 工具及参数 | 要求 |
| --- | --- |
| `create_file(path, content)` | 仅创建不存在的文件，自动创建父目录；不会覆盖已有文件。 |
| `replace_text(path, old_text, new_text)` | `old_text` 必须非空并且只匹配一次，包括重叠的匹配。修改前应先读取相关文件。 |
| `apply_patch(patch)` | 优先使用唯一上下文定位，无需行号；也兼容严格 unified diff。 |

例如，读取 `src/main.py` 后，可调用精确替换：

```json
{
  "path": "src/main.py",
  "old_text": "timeout = 10",
  "new_text": "timeout = 30"
}
```

推荐的上下文补丁示例：

```diff
*** Begin Patch
*** Update File: src/main.py
@@
-timeout = 10
+timeout = 30
*** End Patch
```

每个 `@@` 块必须包含足够的原文本或上下文行，在原文件中恰好匹配一处。上下文行以空格开头；零匹配、多处匹配、重叠或顺序颠倒都拒绝，不进行模糊猜测。也接受原有带精确行号的格式：

```diff
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-timeout = 10
+timeout = 30
```

对应参数为 `{"patch": "上述补丁文本"}`。带行号格式仍严格要求新旧行号匹配。两种格式均支持多个文件、多个 hunk、插入/移除文本行及 `\ No newline at end of file` 标记。文件级新增、删除、重命名不在补丁支持范围内；新增文件使用 `create_file`。

修改前会展示 diff 并调用权限回调。在确认模式下等待批准，在信任模式下仍展示修改。所有补丁先完整解析并检查上下文，再请求批准。歧义匹配、非法补丁或拒绝批准均不会写入文件。批准后、正式写入前会复查原始内容，发现等待审批期间发生的外部修改则返回 `EDIT_CONFLICT`，本次不写入。没有重新引入读取哈希参数；复查与实际替换之间仍存在竞态，不保证并发修改安全。

写入先在同目录暂存，再原子替换单个文件；保留原文件权限及已有 LF/CRLF 换行，新增文本沿用相应换行形式。新建文件以原子链接发布，若批准期间别人创建了同名文件则拒绝覆盖。写入结果返回文件路径和有长度上限的 diff。

多文件修改不构成文件系统事务：若操作系统在已替换部分文件后报错，结果明确包含 `partial_write`、`applied_files`，不会自动回滚用户文件。此时应重新读取相关文件再决定后续动作。

所有文件路径必须解析到工作区内部。`..`、越界符号链接、NTFS 备用数据流、`.git`、`.miniagent`、`.deepseek_api_key`、`.openai_api_key`、`.api_key`、`.env` 和实际的 `.env.*` 文件都不可通过文件工具访问；`.env.example` 可作为普通示例文件使用。

## Shell 执行

| 工具及参数 | 行为 |
| --- | --- |
| `run_command(command, cwd=".", timeout=600, background=false, yield_time_ms=10000, max_output_bytes=16384, purpose="task")` | 等待命令完成或达到 `yield_time_ms` 后返回。未完成时携带运行中的 `job_id`；`background=true` 等价于本次等待为 0。`timeout` 为大于 0 且不超过 86,400 的秒数，默认 600 秒（10 分钟），独立限制进程总运行时间；轮询不会延长这个期限，长任务应在启动时显式设置。 |
| `poll_command(job_id, offset=0, wait_ms=1000, max_output_bytes=16384)` | 查询原任务，不会重复执行。已有未读输出立即返回，否则等待新输出、结束或 `wait_ms` 到期。用返回的 `next_offset` 继续读取；偏移为经过脱敏后的绝对 UTF-8 字节位置。 |
| `cancel_command(job_id)` | 取消进程及其子进程。任务状态返回 `cancelled`，未完成的命令为 `ok: false`。 |

结果包含 `output`、`exit_code`、`elapsed`、`timeout`、`remaining`、`cwd`、`job_id`、`status`、`complete`、`next_offset` 和截断标志。`elapsed` 为已运行秒数，`timeout` 为启动时设置的总运行时限，`remaining` 为距离该期限的剩余秒数（最少为 0，终态统一为 0）；轮询不会重置期限，终态耗时不再增长。运行中 `exit_code` 为 `null`；`status` 为 `running`、`completed`、`timed_out` 或 `cancelled`。非零退出码、超时和取消都会令 `ok` 为 `false`；超时错误明确说明总运行时限已到且进程树已终止。

Windows 优先使用 `pwsh`，否则使用 Windows PowerShell（都关闭 profile 并设置 UTF-8）；两者均不可用时才回退 cmd。POSIX 固定使用 `/bin/sh`，不依赖登录 Shell。实际可执行文件路径会传入模型的运行环境上下文。

`yield_time_ms` 为 0–60,000 的整数毫秒，默认 10,000，仅控制首次调用的等待时间；`wait_ms` 为 0–300,000 的整数毫秒，默认 1,000，仅控制本次轮询等待时间，有未读输出或任务结束时提前返回。两者到期都不会终止进程，进程总运行期限由 `timeout` 独立控制。`max_output_bytes` 为 256–65,536，默认 16 KiB，限制本次 `output` 和 `output_tail` 的合计 UTF-8 字节数（元数据不计入）。等待可取消；轮询时取消会终止对应任务的进程树。`has_more` 表示已有输出尚未读完，`complete` 表示进程结束，二者分别检查；启动成功不代表测试成功。

每个任务最多保留 1 MiB 脱敏输出。超出后保留约一半开头和一半滚动尾部，按完整 UTF-8 字符切分；不写额外日志文件。`total_bytes` 是累计产生的脱敏输出量，`captured_bytes` 是当前保留量，`dropped_bytes` 是已丢弃量。`head_end_offset` 和 `tail_start_offset` 描述保留边界，`omitted_range` 明确标记未保留的半开字节区间；它们不是连续拼接后的虚拟偏移。

读取开头分页时，若发生捕获截断，会另附最新 `output_tail` 预览以及 `output_tail_offset` / `output_tail_end_offset`，使最终测试结论可见；尾部预览不会推进 `next_offset`。可使用 `tail_start_offset` 读取保留尾部，或用 `output_tail_end_offset` 等待之后的新输出。请求偏移落在丢弃区间时跳至当前尾部起点，返回 `skipped_range`，`offset` 为实际起点。滚动期间尾部起点可能变化，因此旧游标也可能触发显式跳过。绝对偏移超过 `total_bytes` 或切开保留的 UTF-8 字符会报 `INVALID_OFFSET`。

`purpose` 只能为 `task` 或 `service`。默认 `task` 必须返回终态结果才能接受本轮最终答复，已经退出但尚未查询的任务也阻塞收尾。`service` 仅用于明确需要持续运行的服务器等，不能用于测试/构建以绕过等待；它不阻塞收尾，但仍受 timeout 和会话生命周期约束，退出时不会作为守护进程保留。

最多保留 32 个任务槽位，必要时仅回收终态已经返回的任务或已结束的服务，避免丢失尚未读取的必需任务结果。进程、输出和任务 ID 均只存在于当前运行中。

等待通过条件变量接收输出和任务完成通知，安静任务无需每 50 毫秒检查状态。绑定外部取消事件时最多每 250 毫秒检查一次取消标志；输出和完成通知仍立即唤醒等待。

任务 ID 只在当前进程内有效；恢复历史会话不会自动重放命令，未知 ID 返回 `uncertain: true`。命令在执行前展示命令内容与目录并经过权限回调。路径检查用于减少操作失误，Shell 仍具有当前操作系统用户的权限，不是沙箱。

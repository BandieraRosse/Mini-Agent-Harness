# 工具参考

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

工具定义来自 `miniagent/tools.py` 的 `SCHEMAS`，使用 Chat Completions function calling 格式。所有调用通过 `ToolRegistry.execute(name, arguments)` 返回 JSON 对象；`ok: false` 表示失败，细节来自 `error` 或命令的 `output`、`exit_code`。参数拼写、类型、范围错误也返回工具结果，Agent 可以据此修正调用。

## 文件读取与查找

| 工具及参数 | 行为与继续读取方式 |
| --- | --- |
| `list_directory(path=".", offset=0, limit=100)` | 列出目录，目录优先。`entries` 含 `path`、`type`；用 `next_offset` 翻页。 |
| `find_files(pattern="*", path=".", offset=0, limit=100)` | 对工作区相对路径做 glob 匹配，无斜杠的模式匹配文件名。`**` 可匹配零级或多级目录，`*?[]` 不跨目录；返回 `files`，用 `next_offset` 翻页。 |
| `search_text(query, path=".", case_sensitive=true, offset=0, limit=100)` | 单行、非空、字面文本搜索；返回文件路径、行号、文本。`offset` 是跳过的匹配数，`next_offset` 用于继续。 |
| `read_file(path, offset=1, limit=200, column=0)` | 返回带行号的 `content`。`offset` 从第 1 行起，`column` 从第 0 个字符起。 |

前三个工具的 `limit` 范围为 1–200，`read_file` 为 1–1000。读取输出最多约 24,000 个文本字符；一行过长时，使用返回的 `next_offset` **和** `next_column` 继续读取同一行。`truncated` 表示还有结果。密钥在分页之前脱敏，字符偏移对应脱敏后的文本。

文本文件必须是 UTF-8（可有 BOM），单文件上限为 2 MiB；含 NUL 的二进制内容和无效 UTF-8 会被拒绝。搜索结果每条最多约 1,000 个字符，`line_truncated` 表示该行需要用 `read_file` 查看。搜索跳过的不可读、过大或非文本文件计入 `skipped_files`。

发现文件时默认跳过 `.git`、`.miniagent`、`node_modules`、`__pycache__`、`.venv`、`venv`、`build`、`dist`、`target` 及常见缓存目录、二进制扩展名和符号链接。每次发现最多检查 30,000 个目录项、10,000 个文件，达到上限返回 `scan_truncated: true`；此时应缩小 `path` 范围。分页期间若目录内容变化，结果顺序也可能变化。

## 文件修改

| 工具及参数 | 要求 |
| --- | --- |
| `create_file(path, content)` | 仅创建不存在的文件，自动创建父目录；不会覆盖已有文件。 |
| `replace_text(path, old_text, new_text)` | `old_text` 必须非空并且只匹配一次，包括重叠的匹配。修改前应先读取相关文件。 |
| `apply_patch(patch)` | 使用标准 unified diff，严格检查行号和上下文。 |

例如，读取 `src/main.py` 后，可调用精确替换：

```json
{
  "path": "src/main.py",
  "old_text": "timeout = 10",
  "new_text": "timeout = 30"
}
```

补丁结构示例：

```diff
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-timeout = 10
+timeout = 30
```

对应参数为 `{"patch": "上述补丁文本"}`。补丁要求文件路径、上下文和新旧行号完全匹配，不做模糊匹配；支持多个文件、多个 hunk、插入/移除文本行及文件末尾无换行标记。文件级新增、删除、重命名不在补丁支持范围内；新增文件使用 `create_file`。

修改前会展示 diff 并调用权限回调。在确认模式下等待批准，在信任模式下仍展示修改。所有补丁先完整解析并检查上下文，再请求批准。歧义匹配、非法补丁或拒绝批准均不会写入文件。文件修改按单会话工作，不记录读取版本，也不在批准后复查文件内容，不保证并发修改安全。

写入先在同目录暂存，再原子替换单个文件；保留原文件权限及已有 LF/CRLF 换行，新增文本沿用相应换行形式。新建文件以原子链接发布，若批准期间别人创建了同名文件则拒绝覆盖。写入结果返回文件路径和有长度上限的 diff。

多文件修改不构成文件系统事务：若操作系统在已替换部分文件后报错，结果明确包含 `partial_write`、`applied_files`，不会自动回滚用户文件。此时应重新读取相关文件再决定后续动作。

所有文件路径必须解析到工作区内部。`..`、越界符号链接、NTFS 备用数据流、`.git`、`.miniagent`、`.deepseek_api_key`、`.openai_api_key`、`.api_key`、`.env` 和实际的 `.env.*` 文件都不可通过文件工具访问；`.env.example` 可作为普通示例文件使用。

## Shell 执行

| 工具及参数 | 行为 |
| --- | --- |
| `run_command(command, cwd=".", timeout=120, background=false)` | 在指定工作区目录运行当前平台 Shell。`timeout` 为大于 0 且不超过 86,400 的秒数。后台执行立即返回 `job_id`。 |
| `poll_command(job_id, offset=0)` | 查询原任务，不会重复执行。用返回的 `next_offset` 继续读取；这里的偏移单位是经过脱敏后的 UTF-8 字节。 |
| `cancel_command(job_id)` | 取消进程及其子进程。任务状态返回 `cancelled`，未完成的命令为 `ok: false`。 |

结果包含 `output`、`exit_code`、`elapsed`、`cwd`、`job_id`、`status`、`complete`、`next_offset` 和截断标志。运行中 `exit_code` 为 `null`；`status` 为 `running`、`completed`、`timed_out` 或 `cancelled`。非零退出码、超时和取消都会令 `ok` 为 `false`。

Windows 优先使用 `pwsh`，否则使用 Windows PowerShell（都关闭 profile 并设置 UTF-8）；两者均不可用时才回退 cmd。POSIX 固定使用 `/bin/sh`，不依赖登录 Shell。实际可执行文件路径会传入模型的运行环境上下文。

每个任务最多保存 1 MiB 输出，每页最多 16 KiB。`has_more` 表示已有输出尚未读完，`complete` 表示进程结束，二者应分别检查；`output_limit_reached` 和 `dropped_bytes` 表示超过保存上限的输出。最多保留 32 个任务槽位，必要时回收已完成任务。

任务 ID 只在当前进程内有效；恢复历史会话不会自动重放命令，未知 ID 返回 `uncertain: true`。命令在执行前展示命令内容与目录并经过权限回调。路径检查用于减少操作失误，Shell 仍具有当前操作系统用户的权限，不是沙箱。

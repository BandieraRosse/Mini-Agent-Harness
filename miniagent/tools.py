"""Small, bounded workspace tools for single-session file edits.

Path checks help avoid accidents. Shell commands still run with the user's own
permissions: this module is not a security sandbox.
"""
from __future__ import annotations

import difflib
import fnmatch
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable

from .tool_errors import ToolError, tool_failure


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 24_000
MAX_SCAN_FILES = 10_000
MAX_SCAN_ENTRIES = 30_000
IGNORED_DIRS = {".git", ".miniagent", "node_modules", "__pycache__", ".venv", "venv",
                "build", "dist", "target", ".mypy_cache", ".pytest_cache", ".next"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip",
                   ".gz", ".tar", ".7z", ".exe", ".dll", ".so", ".pyc", ".pyo",
                   ".mp3", ".mp4", ".woff", ".woff2", ".ttf", ".sqlite", ".db",
                   ".bin", ".class", ".o", ".obj", ".a", ".lib", ".dylib", ".bmp"}
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\Z")


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


SCHEMAS = [
    _schema("read_tool_result", "Read the full archived result behind a context-truncated result_ref; never executes the original tool again.",
            {"tool_call_id": _string("Original call ID / result_ref"),
             "offset": {"type": "integer", "minimum": 0, "description": "Character offset, default 0"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 24000}}, ["tool_call_id"]),
    _schema("list_directory", "List workspace entries; excludes secrets and generated directories.",
            {"path": _string("Relative directory, default '.'"),
             "offset": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
    _schema("find_files", "Find file paths by glob; default excludes binaries, secrets and generated directories.",
            {"pattern": _string("Glob against workspace-relative path; '**' matches zero or more path components, '*?[]' stay within a component. Slash-free patterns match basenames. Default '*'"),
             "path": _string("Directory to search, default '.'"),
             "offset": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
    _schema("search_text", "Search literal text in UTF-8 files; narrow with path/globs. Returns bounded matches or unique paths.",
            {"query": _string("Nonempty literal text"), "path": _string("File or directory, default '.'"),
             "case_sensitive": {"type": "boolean"},
             "include_glob": _string("Include workspace-relative paths matching this glob; default '*'"),
             "exclude_glob": _string("Exclude workspace-relative paths matching this glob; default none"),
             "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
             "files_only": {"type": "boolean", "description": "Return unique files, not line matches; offset counts files"},
             "offset": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["query"]),
    _schema("read_file", "Read line-numbered UTF-8 text. Continue with next_offset/next_column when truncated.",
            {"path": _string("Workspace-relative file"),
             "offset": {"type": "integer", "minimum": 1, "description": "First line, default 1"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
             "column": {"type": "integer", "minimum": 0, "description": "Character offset within first line; default 0"}}, ["path"]),
    _schema("create_file", "Create a UTF-8 file without overwriting. Shows a diff before approval. Creates missing parent directories.",
            {"path": _string("Workspace-relative new file"), "content": _string("Complete content")}, ["path", "content"]),
    _schema("replace_text", "Replace one unique exact text match. Shows diff before approval.",
            {"path": _string("Workspace-relative file"), "old_text": _string("Nonempty unique existing text"),
             "new_text": _string("Replacement text")},
            ["path", "old_text", "new_text"]),
    _schema("apply_patch", "Edit existing files using unique exact context, without line numbers: *** Begin Patch, *** Update File: path, @@, space/minus/plus lines, *** End Patch. Strict unified diff also supported. All files are preflighted; ambiguous context fails. Use create_file for new files.",
            {"patch": _string("Context patch (preferred) or strict unified diff; no renames/additions/deletions")},
            ["patch"]),
    _schema("run_command", "Run an approved shell command. Returns when complete or yield_time_ms elapses; poll running jobs to completion. Commands run with user permissions.",
            {"command": _string("Shell command"), "cwd": _string("Workspace-relative directory, default '.'"),
             "timeout": {"type": "number", "minimum": 0, "maximum": 86400, "description": "Positive seconds before termination, default 120"},
             "background": {"type": "boolean", "description": "Compatibility shortcut for yield_time_ms=0"},
             "purpose": {"type": "string", "enum": ["task", "service"], "description": "Default task: completion must be observed before final answer. service: intentional long-lived server, never tests/builds; stops when MiniAgent exits."},
             "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 60000, "description": "Wait before returning, default 10000; separate from process timeout"},
             "max_output_bytes": {"type": "integer", "minimum": 256, "maximum": 65536, "description": "Combined UTF-8 budget for output and output_tail, default 16384"}}, ["command"]),
    _schema("poll_command", "Read an existing job without rerunning. Use next_offset; wait_ms waits for new output or completion. Omitted ranges are explicit; output_tail is a separate latest preview.",
            {"job_id": _string("ID from run_command"), "offset": {"type": "integer", "minimum": 0},
             "wait_ms": {"type": "integer", "minimum": 0, "maximum": 60000, "description": "Default 1000; use longer waits for quiet jobs"},
             "max_output_bytes": {"type": "integer", "minimum": 256, "maximum": 65536}}, ["job_id"]),
    _schema("cancel_command", "Cancel a known running job and its child processes.",
            {"job_id": _string("ID from run_command")}, ["job_id"]),
]


def _protected(parts: tuple[str, ...]) -> bool:
    return any(part.lower() in {".git", ".miniagent", ".deepseek_api_key", ".openai_api_key", ".api_key", ".env", "chatgpt-auth.dat", "chatgpt-auth.lock"}
               or part.lower().startswith("chatgpt-auth-")
               or (part.lower().startswith(".env.") and not part.lower().endswith(".example"))
               for part in parts)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _text(data: bytes) -> str:
    if b"\x00" in data:
        raise ToolError("Binary files are not supported by text tools.", "UNSUPPORTED_FILE")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("File is not UTF-8 text; use a shell command for other encodings.", "UNSUPPORTED_FILE") from exc


def _bounded_text(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[truncated]"


def _glob_matches(relative: str, pattern: str) -> bool:
    """Match path components without letting ordinary wildcards cross separators."""
    pattern = pattern.replace("\\", "/")
    components = relative.split("/")
    if "/" not in pattern:
        return fnmatch.fnmatchcase(components[-1], pattern)
    # Each state is the number of path components consumed so far. Keeping
    # states explicitly avoids recursive backtracking with repeated '**'.
    states = {0}
    for token in pattern.split("/"):
        if token == ".":
            continue
        if token == "**":
            states = set(range(min(states), len(components) + 1)) if states else set()
        else:
            states = {index + 1 for index in states
                      if index < len(components) and fnmatch.fnmatchcase(components[index], token)}
        if not states:
            return False
    return len(components) in states


class ToolRegistry:
    parallel_safe = frozenset({"list_directory", "find_files", "search_text", "read_file", "read_tool_result"})
    def __init__(self, workspace: Path, process_manager: Any,
                 approve: Callable[[str, str], bool], redact: Callable[[str], str], permission_mode=None):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be a directory.")
        self.process_manager = process_manager
        self.approve = approve
        self.redact = redact
        self.permission_mode = permission_mode or (lambda: 'ask')
        self._specs = {schema["function"]["name"]: schema["function"]["parameters"] for schema in SCHEMAS}
        self.session = None

    @property
    def schemas(self):
        if self.permission_mode() == 'read-only':
            return [schema for schema in SCHEMAS if schema['function']['name'] in self.parallel_safe | {'poll_command'}]
        return SCHEMAS

    def bind_session(self, session):
        self.session = session

    def _read_tool_result(self, tool_call_id: str, offset: int = 0, limit: int = 8000):
        if self.session is not None:
            for item in reversed(self.session.messages):
                if item.get("role") == "tool" and item.get("tool_call_id") == tool_call_id:
                    content = item["content"]
                    if offset > len(content):
                        raise ToolError("offset exceeds the archived result.", "INVALID_OFFSET")
                    end = min(len(content), offset + limit)
                    return {"ok": True, "tool_call_id": tool_call_id, "content": content[offset:end],
                            "next_offset": end, "truncated": end < len(content), "total_chars": len(content)}
        raise ToolError("No archived result for this tool_call_id.", "NOT_FOUND")

    def execute(self, name: str, arguments: dict) -> dict:
        try:
            if not isinstance(name, str) or name not in self._specs:
                raise ToolError("Unknown tool name.", "UNKNOWN_TOOL")
            if self.permission_mode() == 'read-only' and name not in self.parallel_safe | {'poll_command'}:
                raise ToolError("This operation is unavailable in read-only mode.", "PERMISSION_DENIED")
            self._validate(name, arguments)
            self._check_cancelled()
            result = getattr(self, "_" + name)(**arguments)
            return self._redact(result)
        except (ToolError, OSError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, ToolError) else (
                "FILE_EXISTS" if isinstance(exc, FileExistsError) else
                "NOT_FOUND" if isinstance(exc, FileNotFoundError) else
                "IO_ERROR" if isinstance(exc, OSError) else "INVALID_ARGUMENT")
            return tool_failure(code, _bounded_text(self.redact(str(exc)), 2000))

    def _check_cancelled(self) -> None:
        event = getattr(self.process_manager, "cancel_event", None)
        if event is not None and event.is_set():
            raise KeyboardInterrupt()

    def _approve_file(self, detail: str) -> bool:
        try:
            approved = self.approve("file", self.redact(detail))
        except Exception as exc:
            raise ToolError(f"File approval failed: {exc}", "APPROVAL_FAILED") from exc
        self._check_cancelled()
        return approved

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _validate(self, name: str, args: dict) -> None:
        if not isinstance(args, dict):
            raise ToolError("Tool arguments must be an object.")
        spec = self._specs[name]
        if set(args) - set(spec["properties"]):
            raise ToolError("Unknown arguments: " + ", ".join(str(k) for k in set(args) - set(spec["properties"])))
        for key in spec["required"]:
            if key not in args:
                raise ToolError(f"Missing required argument: {key}")
        for key, value in args.items():
            rule = spec["properties"][key]
            kind = rule["type"]
            valid = ((kind == "string" and isinstance(value, str))
                     or (kind == "integer" and type(value) is int)
                     or (kind == "number" and type(value) in (int, float))
                     or (kind == "boolean" and type(value) is bool)
                     or (kind == "object" and isinstance(value, dict)))
            if not valid:
                raise ToolError(f"{key} must be {kind}.")
            if "enum" in rule and value not in rule["enum"]:
                raise ToolError(f"{key} must be one of {rule['enum']}.")
            if kind == "number" and not math.isfinite(value):
                raise ToolError(f"{key} must be finite.")
            if "minimum" in rule and value < rule["minimum"]:
                raise ToolError(f"{key} must be at least {rule['minimum']}.")
            if "maximum" in rule and value > rule["maximum"]:
                raise ToolError(f"{key} must be at most {rule['maximum']}.")
            if isinstance(value, str) and len(value) > MAX_FILE_BYTES:
                raise ToolError(f"{key} exceeds the {MAX_FILE_BYTES}-character limit.")

    def _path(self, value: str, *, directory: bool = False) -> Path:
        if not value or "\x00" in value:
            raise ToolError("Path must be nonempty and contain no NUL characters.")
        raw = Path(value)
        if ".." in raw.parts:
            raise ToolError("Parent traversal ('..') is not allowed.", "PATH_DENIED")
        candidate = raw if raw.is_absolute() else self.workspace / raw
        try:
            lexical = candidate.relative_to(self.workspace)
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(self.workspace)
        except (ValueError, RuntimeError) as exc:
            raise ToolError("Path is outside the workspace or traverses an invalid symlink.", "PATH_DENIED") from exc
        if _protected(lexical.parts) or _protected(relative.parts):
            raise ToolError("Protected secret, Git metadata, or session path is unavailable.", "PATH_DENIED")
        from .config import user_directory
        if resolved.is_relative_to(user_directory().resolve()):
            raise ToolError("MiniAgent user configuration and credentials are protected.", "PATH_DENIED")
        # Windows NTFS alternate streams must not bypass the filename checks.
        if any(":" in part for part in lexical.parts):
            raise ToolError("Alternate data streams are not supported.", "PATH_DENIED")
        if directory and not resolved.is_dir():
            raise ToolError("Directory does not exist.", "NOT_FOUND")
        return resolved

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def _read(self, path: Path) -> tuple[bytes, str]:
        if not path.is_file():
            raise ToolError("File does not exist or is not a regular file.", "NOT_FOUND")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(f"File exceeds {MAX_FILE_BYTES} bytes; use a bounded shell read.", "UNSUPPORTED_FILE")
        return data, _text(data)

    def _safe_entry(self, entry: Path) -> bool:
        return (not entry.is_symlink() and not _protected((entry.name,))
                and entry.name.lower() not in IGNORED_DIRS
                and (entry.is_dir() or entry.suffix.lower() not in BINARY_SUFFIXES))

    def _walk(self, base: Path):
        """Deterministic bounded discovery; yields a sentinel if scanning was capped."""
        pending = [base]
        scanned = found = 0
        while pending:
            self._check_cancelled()
            folder = pending.pop()
            entries = []
            with os.scandir(folder) as iterator:
                for item in iterator:
                    self._check_cancelled()
                    scanned += 1
                    if scanned > MAX_SCAN_ENTRIES:
                        yield None
                        return
                    entries.append(Path(item.path))
            directories = []
            for entry in sorted(entries, key=lambda p: p.name.lower()):
                self._check_cancelled()
                if not self._safe_entry(entry):
                    continue
                try:
                    resolved = self._path(str(entry))
                    if resolved.is_dir():
                        directories.append(resolved)
                    elif resolved.is_file() and resolved.suffix.lower() not in BINARY_SUFFIXES:
                        found += 1
                        if found > MAX_SCAN_FILES:
                            yield None
                            return
                        yield resolved
                except OSError:
                    continue
            pending.extend(reversed(directories))

    def _list_directory(self, path: str = ".", offset: int = 0, limit: int = 100) -> dict:
        base = self._path(path, directory=True)
        entries = []
        scan_truncated = False
        with os.scandir(base) as iterator:
            for index, item in enumerate(iterator):
                self._check_cancelled()
                if index >= MAX_SCAN_ENTRIES:
                    scan_truncated = True
                    break
                entry = Path(item.path)
                if self._safe_entry(entry):
                    entries.append(entry)
        entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
        result = []
        chars = 0
        for entry in entries[offset:offset + limit]:
            relative = self._relative(entry)
            if chars + len(relative) > MAX_OUTPUT_CHARS and result:
                break
            result.append({"path": relative, "type": "directory" if entry.is_dir() else "file"})
            chars += len(relative)
        next_offset = offset + len(result)
        return {"ok": True, "entries": result, "next_offset": next_offset,
                "truncated": next_offset < len(entries) or scan_truncated,
                "scan_truncated": scan_truncated}

    def _find_files(self, pattern: str = "*", path: str = ".", offset: int = 0, limit: int = 100) -> dict:
        base = self._path(path, directory=True)
        files = []
        total = chars = 0
        scan_truncated = more = False
        for entry in self._walk(base):
            if entry is None:
                scan_truncated = True
                break
            relative = self._relative(entry)
            if not _glob_matches(relative, pattern):
                continue
            total += 1
            if total <= offset:
                continue
            if len(files) >= limit or chars + len(relative) > MAX_OUTPUT_CHARS:
                more = True
                break
            files.append(relative)
            chars += len(relative)
        return {"ok": True, "files": files, "next_offset": offset + len(files),
                "truncated": more or scan_truncated, "scan_truncated": scan_truncated}

    def _search_text(self, query: str, path: str = ".", case_sensitive: bool = True,
                     offset: int = 0, limit: int = 100, include_glob: str = "*",
                     exclude_glob: str = "", context_lines: int = 0, files_only: bool = False) -> dict:
        if not query or "\n" in query or "\r" in query:
            raise ToolError("query must be nonempty literal text on one line.")
        base = self._path(path)
        if not base.exists():
            raise ToolError("Search path does not exist.", "NOT_FOUND")
        needle = query if case_sensitive else query.casefold()
        matches = []
        count = chars = skipped = 0
        scan_truncated = more = False
        for entry in self._walk(base) if base.is_dir() else [base]:
            self._check_cancelled()
            if entry is None:
                scan_truncated = True
                break
            relative = self._relative(entry)
            if not _glob_matches(relative, include_glob) or (exclude_glob and _glob_matches(relative, exclude_glob)):
                continue
            try:
                _, content = self._read(entry)
            except (OSError, ToolError):
                skipped += 1
                continue
            lines = content.splitlines()
            for number, line in enumerate(lines, 1):
                self._check_cancelled()
                if needle not in (line if case_sensitive else line.casefold()):
                    continue
                count += 1
                if count <= offset:
                    if files_only:
                        break
                    continue
                excerpt = _bounded_text(self.redact(line), 1000)
                item = relative if files_only else {"path": relative, "line": number, "text": excerpt,
                                                    "line_truncated": len(self.redact(line)) > 1000}
                item_chars = len(relative) if files_only else len(relative) + len(excerpt)
                if context_lines and not files_only:
                    context = [{"line": index + 1, "text": _bounded_text(self.redact(lines[index]), 1000),
                                "line_truncated": len(self.redact(lines[index])) > 1000}
                               for index in range(max(0, number - 1 - context_lines), min(len(lines), number + context_lines))
                               if index != number - 1]
                    item["context"] = context
                    item_chars += sum(len(row["text"]) for row in context)
                if len(matches) >= limit or chars + item_chars > MAX_OUTPUT_CHARS:
                    more = True
                    break
                matches.append(item)
                chars += item_chars
                if files_only:
                    break
            if more:
                break
        return {"ok": True, "files" if files_only else "matches": matches, "next_offset": offset + len(matches),
                "truncated": more or scan_truncated, "scan_truncated": scan_truncated,
                "skipped_files": skipped}

    def _read_file(self, path: str, offset: int = 1, limit: int = 200, column: int = 0) -> dict:
        target = self._path(path)
        _, content = self._read(target)
        # Redact before paging so a key split across two pages cannot be reconstructed.
        content = self.redact(content)
        lines = content.splitlines(keepends=True)
        if offset > len(lines) + 1:
            raise ToolError(f"offset exceeds the end of file ({len(lines)} lines).")
        index = offset - 1
        if column and (index >= len(lines) or column >= len(lines[index].rstrip("\r\n"))):
            raise ToolError("column exceeds the selected line.")
        output = []
        used = consumed = 0
        next_column = column
        while index < len(lines) and consumed < limit:
            line = lines[index].rstrip("\r\n")
            prefix = f"{index + 1}: "
            available = MAX_OUTPUT_CHARS - used - len(prefix) - 1
            if available <= 0:
                break
            fragment = line[next_column:next_column + available]
            output.append(prefix + fragment)
            used += len(prefix) + len(fragment) + 1
            if next_column + len(fragment) < len(line):
                next_column += len(fragment)
                break
            index += 1
            consumed += 1
            next_column = 0
        return {"ok": True, "path": self._relative(target),
                "content": "\n".join(output), "total_lines": len(lines), "offset": offset,
                "next_offset": index + 1, "next_column": next_column, "truncated": index < len(lines)}

    def _diff(self, path: Path, before: str, after: str) -> str:
        name = self._relative(path)
        parts = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                    fromfile="a/" + name, tofile="b/" + name)
        return self.redact("".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                                  for line in parts))

    def _stage(self, target: Path, data: bytes, mode: int | None) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=".miniagent-edit-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                temporary.chmod(mode)
            return temporary
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _create_file(self, path: str, content: str) -> dict:
        target = self._path(path)
        if target.exists():
            raise ToolError("File already exists; use replace_text or apply_patch.", "FILE_EXISTS")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ToolError("New file exceeds the file size limit.")
        _text(data)
        diff = self._diff(target, "", content)
        if not self._approve_file(f"Create {self._relative(target)}\n{diff}"):
            return tool_failure("APPROVAL_DENIED", "User declined file creation.", declined=True)
        if self._path(path) != target:
            raise ToolError("Path changed while awaiting approval; retry after reading the workspace.", "EDIT_CONFLICT")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._stage(target, data, None)
        try:
            # Hard linking publishes the complete file atomically and fails if it exists.
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"ok": True, "path": self._relative(target),
                "diff": _bounded_text(diff), "diff_truncated": len(diff) > MAX_OUTPUT_CHARS}

    def _replace_text(self, path: str, old_text: str, new_text: str) -> dict:
        target = self._path(path)
        original, before = self._read(target)
        if not old_text:
            raise ToolError("old_text must not be empty.")
        # Prefer the literal match; allow LF input against a CRLF file as a convenience.
        if old_text not in before:
            old_text = old_text.replace("\r\n", "\n").replace("\n", _newline(before))
        newline = _newline(old_text) if "\n" in old_text else _newline(before)
        new_text = new_text.replace("\r\n", "\n").replace("\n", newline)
        first = before.find(old_text)
        if first < 0:
            raise ToolError("old_text matched 0 times; exactly one match is required.", "EDIT_CONFLICT")
        if before.find(old_text, first + 1) >= 0:
            raise ToolError("old_text matched at least 2 times; exactly one match is required.", "EDIT_CONFLICT")
        after = before.replace(old_text, new_text, 1)
        return self._commit([(target, original, after.encode("utf-8"))])

    def _commit(self, changes: list[tuple[Path, bytes, bytes]]) -> dict:
        for _, _, data in changes:
            if len(data) > MAX_FILE_BYTES:
                raise ToolError("Result exceeds the file size limit.")
            _text(data)
        changes = [change for change in changes if change[1] != change[2]]
        if not changes:
            return {"ok": True, "changed": False, "files": [], "diff": ""}
        diff = "\n".join(self._diff(path, _text(before), _text(after)) for path, before, after in changes)
        if not self._approve_file(diff):
            return tool_failure("APPROVAL_DENIED", "User declined file changes.", declined=True)
        staged: list[tuple[Path, Path]] = []
        applied = []
        try:
            # Stage everything before the first write.
            for target, original, updated in changes:
                if self._path(str(target)) != target:
                    raise ToolError("Path changed while awaiting approval.", "EDIT_CONFLICT")
                if self._read(target)[0] != original:
                    raise ToolError("File changed while awaiting approval; no files were written.", "EDIT_CONFLICT")
                staged.append((target, self._stage(target, updated, stat.S_IMODE(target.stat().st_mode))))
            for target, temporary in staged:
                os.replace(temporary, target)
                applied.append(self._relative(target))
        except OSError as exc:
            # Multi-file filesystem transactions do not exist here; explicitly report partial writes.
            return tool_failure("PARTIAL_WRITE" if applied else "IO_ERROR", str(exc), applied_files=applied,
                                partial_write=bool(applied), guidance="Read affected files before retrying; existing changes were not rolled back.")
        finally:
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)
        return {"ok": True, "changed": True,
                "files": [{"path": self._relative(path)} for path, _, _ in changes],
                "diff": _bounded_text(diff), "diff_truncated": len(diff) > MAX_OUTPUT_CHARS}

    def _apply_patch(self, patch: str) -> dict:
        context_format = patch.startswith("*** Begin Patch")
        sections = _parse_context_patch(patch) if context_format else _parse_patch(patch)
        changes = []
        seen: set[Path] = set()
        for name, hunks in sections:
            target = self._path(name)
            if target in seen:
                raise ToolError("A patch must contain only one section per file.")
            seen.add(target)
            original, content = self._read(target)
            if context_format:
                hunks = _locate_context_hunks(content, hunks)
            updated = _apply_hunks(content, hunks)
            changes.append((target, original, updated.encode("utf-8")))
        return self._commit(changes)

    def _run_command(self, command: str, cwd: str = ".", timeout: float = 120, background: bool = False,
                     yield_time_ms: int = 10_000, max_output_bytes: int = 16_384, purpose: str = "task") -> dict:
        if not command.strip():
            raise ToolError("command must not be empty.")
        self._path(cwd, directory=True)
        return self.process_manager.run(command=command, cwd=cwd, timeout=timeout, background=background,
                                        yield_time_ms=yield_time_ms, max_output_bytes=max_output_bytes, purpose=purpose)

    def completion_blockers(self) -> list[dict]:
        return self.process_manager.completion_blockers()

    def _poll_command(self, job_id: str, offset: int = 0, wait_ms: int = 1000,
                      max_output_bytes: int = 16_384) -> dict:
        return self.process_manager.poll(job_id, offset=offset, wait_ms=wait_ms, max_output_bytes=max_output_bytes)

    def _cancel_command(self, job_id: str) -> dict:
        return self.process_manager.cancel(job_id)

    def close(self) -> None:
        self.process_manager.close()


def _patch_path(header: str, prefix: str) -> str:
    value = header[4:].split("\t", 1)[0]
    if value == "/dev/null":
        raise ToolError("Patch additions/deletions are unsupported; use create_file for additions.")
    if value.startswith(prefix + "/"):
        value = value[2:]
    if not value or value.startswith('"'):
        raise ToolError("Use unquoted workspace-relative patch paths.")
    return value


def _parse_context_patch(patch: str) -> list:
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ToolError("Context patch requires Begin Patch and End Patch markers.")
    sections = []
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        if not header.startswith("*** Update File: "):
            raise ToolError("Expected *** Update File: path; only existing files are supported.")
        path = header[len("*** Update File: "):]
        index += 1
        hunks = []
        while index < len(lines) - 1 and lines[index] == "@@":
            index += 1
            body = []
            while index < len(lines) - 1 and lines[index] != "@@" and not lines[index].startswith("*** "):
                line = lines[index]
                if line == "\\ No newline at end of file":
                    if not body or not body[-1][2]:
                        raise ToolError("Misplaced no-newline marker.")
                    operation, text, _ = body[-1]
                    body[-1] = operation, text, False
                elif line and line[0] in " +-":
                    body.append((line[0], line[1:], True))
                else:
                    raise ToolError("Context hunk lines require a space, minus or plus prefix.")
                index += 1
            if not any(operation in " -" for operation, _, _ in body):
                raise ToolError("Each context hunk needs existing lines to locate it uniquely.")
            hunks.append(body)
        if not hunks:
            raise ToolError("Context file section requires @@ and at least one hunk.")
        sections.append((path, hunks))
    if not sections:
        raise ToolError("Patch is empty.")
    return sections


def _locate_context_hunks(content: str, bodies: list) -> list:
    source = [(line.rstrip("\r\n"), line.endswith(("\n", "\r"))) for line in content.splitlines(keepends=True)]
    hunks = []
    cursor = delta = 0
    for body in bodies:
        old = [(text, terminated) for operation, text, terminated in body if operation in " -"]
        positions = []
        for index in range(len(source) - len(old) + 1):
            if source[index:index + len(old)] == old:
                positions.append(index)
                if len(positions) > 1:
                    break
        if len(positions) != 1:
            raise ToolError("Context matched zero or multiple locations; read the file and supply more context.", "PATCH_CONTEXT_MISMATCH")
        position = positions[0]
        if position < cursor:
            raise ToolError("Context hunks overlap or are out of file order.")
        new_count = sum(operation in " +" for operation, _, _ in body)
        new_start = position + delta + (1 if new_count else 0)
        hunks.append((position + 1, len(old), new_start, new_count, body))
        delta += new_count - len(old)
        cursor = position + len(old)
    return hunks


def _parse_patch(patch: str) -> list:
    if not patch.strip():
        raise ToolError("Patch is empty.")
    lines = patch.splitlines()
    sections = []
    index = 0
    while index < len(lines):
        while index < len(lines) and (lines[index].startswith("diff --git ") or lines[index].startswith("index ")):
            index += 1
        if index >= len(lines) or not lines[index].startswith("--- "):
            raise ToolError("Expected unified diff '---' header.")
        old_path = _patch_path(lines[index], "a")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ToolError("Expected unified diff '+++' header.")
        new_path = _patch_path(lines[index], "b")
        if new_path != old_path:
            raise ToolError("Patch renames are unsupported.")
        index += 1
        hunks = []
        while index < len(lines) and lines[index].startswith("@@"):
            match = _HUNK.fullmatch(lines[index])
            if not match:
                raise ToolError("Invalid unified diff hunk header.")
            old_start, old_count, new_start, new_count = (int(value) if value is not None else 1 for value in match.groups())
            index += 1
            body = []
            old_seen = new_seen = 0
            while index < len(lines):
                line = lines[index]
                if line == "\\ No newline at end of file":
                    if not body or not body[-1][2]:
                        raise ToolError("Misplaced no-newline marker.")
                    operation, text, _ = body[-1]
                    body[-1] = (operation, text, False)
                    index += 1
                    continue
                if old_seen == old_count and new_seen == new_count:
                    break
                if not line or line[0] not in " +-":
                    raise ToolError("Invalid or incomplete patch hunk body.")
                operation, text = line[0], line[1:]
                old_seen += operation in " -"
                new_seen += operation in " +"
                if old_seen > old_count or new_seen > new_count:
                    raise ToolError("Patch hunk line counts do not match its header.")
                body.append((operation, text, True))
                index += 1
            if old_seen != old_count or new_seen != new_count or not body:
                raise ToolError("Patch hunk line counts do not match its header.")
            hunks.append((old_start, old_count, new_start, new_count, body))
        if not hunks:
            raise ToolError("Patch file section has no hunks.")
        sections.append((new_path, hunks))
    return sections


def _apply_hunks(content: str, hunks: list) -> str:
    source = content.splitlines(keepends=True)
    output = []
    cursor = 0
    newline = _newline(content)
    for old_start, old_count, new_start, new_count, body in hunks:
        position = old_start if old_count == 0 else old_start - 1
        new_position = new_start if new_count == 0 else new_start - 1
        if position < cursor or position > len(source) or (old_count and old_start < 1):
            raise ToolError("Patch hunks overlap or use invalid source line numbers.")
        output.extend(source[cursor:position])
        if new_position != len(output):
            raise ToolError("Patch new line numbers do not match the resulting file.")
        cursor = position
        for operation, text, terminated in body:
            if operation in " -":
                if cursor >= len(source):
                    raise ToolError("Patch refers past the end of the file.", "PATCH_CONTEXT_MISMATCH")
                current = source[cursor]
                actual_terminated = current.endswith(("\n", "\r"))
                if current.rstrip("\r\n") != text or actual_terminated != terminated:
                    raise ToolError(f"Patch context does not match at line {cursor + 1}; read the file again.", "PATCH_CONTEXT_MISMATCH")
                cursor += 1
                if operation == " ":
                    output.append(current)
            elif operation == "+":
                output.append(text + (newline if terminated else ""))
    output.extend(source[cursor:])
    if any(not line.endswith(("\n", "\r")) for line in output[:-1]):
        raise ToolError("A no-newline marker may only describe the final line of a file.")
    return "".join(output)

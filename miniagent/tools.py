"""Small, bounded workspace tools with optimistic concurrency for file edits.

Path checks help avoid accidents. Shell commands still run with the user's own
permissions: this module is not a security sandbox.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable


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
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\Z")


class ToolError(ValueError):
    """An actionable tool failure suitable for returning to the model."""


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


SCHEMAS = [
    _schema("list_directory", "List workspace entries; excludes secrets and generated directories.",
            {"path": _string("Relative directory, default '.'"),
             "offset": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
    _schema("find_files", "Find file paths by glob; default excludes binaries, secrets and generated directories.",
            {"pattern": _string("Glob against workspace-relative path; '**' matches zero or more path components, '*?[]' stay within a component. Slash-free patterns match basenames. Default '*'"),
             "path": _string("Directory to search, default '.'"),
             "offset": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
    _schema("search_text", "Search literal text in UTF-8 files; returns bounded line matches.",
            {"query": _string("Nonempty literal text"), "path": _string("Directory, default '.'"),
             "case_sensitive": {"type": "boolean"},
             "offset": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["query"]),
    _schema("read_file", "Read line-numbered UTF-8 text and its sha256. Continue with next_offset/next_column when truncated.",
            {"path": _string("Workspace-relative file"),
             "offset": {"type": "integer", "minimum": 1, "description": "First line, default 1"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
             "column": {"type": "integer", "minimum": 0, "description": "Character offset within first line; default 0"}}, ["path"]),
    _schema("create_file", "Create a UTF-8 file without overwriting. Shows a diff before approval. Creates missing parent directories.",
            {"path": _string("Workspace-relative new file"), "content": _string("Complete content")}, ["path", "content"]),
    _schema("replace_text", "Replace one unique exact text match. Supply sha256 from read_file; stale files are rejected. Shows diff before approval.",
            {"path": _string("Workspace-relative file"), "old_text": _string("Nonempty unique existing text"),
             "new_text": _string("Replacement text"), "expected_sha256": _string("Required hash from read_file")},
            ["path", "old_text", "new_text", "expected_sha256"]),
    _schema("apply_patch", "Apply strict unified diff to existing files, with exact context and no fuzz. All files are preflighted before writing. No renames/additions/deletions; use create_file for additions.",
            {"patch": _string("Unified diff using --- a/path and +++ b/path headers"),
             "expected_sha256": {"type": "object", "description": "Map each relative file path to hash from read_file", "additionalProperties": {"type": "string"}}},
            ["patch", "expected_sha256"]),
    _schema("run_command", "Run a shell command after user approval. Use background for long work, then poll_command. Commands run with user permissions.",
            {"command": _string("Shell command"), "cwd": _string("Workspace-relative directory, default '.'"),
             "timeout": {"type": "number", "minimum": 0, "maximum": 86400, "description": "Positive seconds before termination, default 120"},
             "background": {"type": "boolean"}}, ["command"]),
    _schema("poll_command", "Read a background job's output without rerunning it; continue from next_offset.",
            {"job_id": _string("ID from run_command"), "offset": {"type": "integer", "minimum": 0}}, ["job_id"]),
    _schema("cancel_command", "Cancel a known running job and its child processes.",
            {"job_id": _string("ID from run_command")}, ["job_id"]),
]


def _protected(parts: tuple[str, ...]) -> bool:
    return any(part.lower() in {".git", ".miniagent", ".deepseek_api_key", ".openai_api_key", ".api_key", ".env"}
               or (part.lower().startswith(".env.") and not part.lower().endswith(".example"))
               for part in parts)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _text(data: bytes) -> str:
    if b"\x00" in data:
        raise ToolError("Binary files are not supported by text tools.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("File is not UTF-8 text; use a shell command for other encodings.") from exc


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
    def __init__(self, workspace: Path, process_manager: Any,
                 approve: Callable[[str, str], bool], redact: Callable[[str], str]):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be a directory.")
        self.process_manager = process_manager
        self.approve = approve
        self.redact = redact
        self.schemas = SCHEMAS
        self._specs = {schema["function"]["name"]: schema["function"]["parameters"] for schema in SCHEMAS}

    def execute(self, name: str, arguments: dict) -> dict:
        try:
            if not isinstance(name, str) or name not in self._specs:
                raise ToolError("Unknown tool name.")
            self._validate(name, arguments)
            result = getattr(self, "_" + name)(**arguments)
            return self._redact(result)
        except (ToolError, OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": _bounded_text(self.redact(str(exc)), 2000)}

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
            raise ToolError("Parent traversal ('..') is not allowed.")
        candidate = raw if raw.is_absolute() else self.workspace / raw
        try:
            lexical = candidate.relative_to(self.workspace)
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(self.workspace)
        except (ValueError, RuntimeError) as exc:
            raise ToolError("Path is outside the workspace or traverses an invalid symlink.") from exc
        if _protected(lexical.parts) or _protected(relative.parts):
            raise ToolError("Protected secret, Git metadata, or session path is unavailable.")
        # Windows NTFS alternate streams must not bypass the filename checks.
        if any(":" in part for part in lexical.parts):
            raise ToolError("Alternate data streams are not supported.")
        if directory and not resolved.is_dir():
            raise ToolError("Directory does not exist.")
        return resolved

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def _read(self, path: Path) -> tuple[bytes, str]:
        if not path.is_file():
            raise ToolError("File does not exist or is not a regular file.")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(f"File exceeds {MAX_FILE_BYTES} bytes; use a bounded shell read.")
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
            folder = pending.pop()
            entries = []
            with os.scandir(folder) as iterator:
                for item in iterator:
                    scanned += 1
                    if scanned > MAX_SCAN_ENTRIES:
                        yield None
                        return
                    entries.append(Path(item.path))
            directories = []
            for entry in sorted(entries, key=lambda p: p.name.lower()):
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
                     offset: int = 0, limit: int = 100) -> dict:
        if not query or "\n" in query or "\r" in query:
            raise ToolError("query must be nonempty literal text on one line.")
        base = self._path(path, directory=True)
        needle = query if case_sensitive else query.casefold()
        matches = []
        count = chars = skipped = 0
        scan_truncated = more = False
        for entry in self._walk(base):
            if entry is None:
                scan_truncated = True
                break
            try:
                _, content = self._read(entry)
            except (OSError, ToolError):
                skipped += 1
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if needle not in (line if case_sensitive else line.casefold()):
                    continue
                count += 1
                if count <= offset:
                    continue
                excerpt = _bounded_text(self.redact(line), 1000)
                relative = self._relative(entry)
                if len(matches) >= limit or chars + len(excerpt) + len(relative) > MAX_OUTPUT_CHARS:
                    more = True
                    break
                matches.append({"path": relative, "line": number, "text": excerpt,
                                "line_truncated": len(line) > 1000})
                chars += len(excerpt) + len(relative)
            if more:
                break
        return {"ok": True, "matches": matches, "next_offset": offset + len(matches),
                "truncated": more or scan_truncated, "scan_truncated": scan_truncated,
                "skipped_files": skipped}

    def _read_file(self, path: str, offset: int = 1, limit: int = 200, column: int = 0) -> dict:
        target = self._path(path)
        data, content = self._read(target)
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
        return {"ok": True, "path": self._relative(target), "sha256": _sha(data),
                "content": "\n".join(output), "total_lines": len(lines), "offset": offset,
                "next_offset": index + 1, "next_column": next_column, "truncated": index < len(lines)}

    def _checked(self, path: Path, expected_sha256: str) -> tuple[bytes, str]:
        if not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
            raise ToolError("expected_sha256 must be the SHA-256 returned by read_file.")
        data, content = self._read(path)
        if _sha(data) != expected_sha256.lower():
            raise ToolError("File changed since it was read. Read it again before editing.")
        return data, content

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
            raise ToolError("File already exists; use replace_text or apply_patch with its current hash.")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ToolError("New file exceeds the file size limit.")
        _text(data)
        diff = self._diff(target, "", content)
        if not self.approve("file", self.redact(f"Create {self._relative(target)}\n{diff}")):
            return {"ok": False, "error": "User declined file creation.", "declined": True}
        if self._path(path) != target:
            raise ToolError("Path changed while awaiting approval; retry after reading the workspace.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._stage(target, data, None)
        try:
            # Hard linking publishes the complete file atomically and fails if it exists.
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"ok": True, "path": self._relative(target), "sha256": _sha(data),
                "diff": _bounded_text(diff), "diff_truncated": len(diff) > MAX_OUTPUT_CHARS}

    def _replace_text(self, path: str, old_text: str, new_text: str, expected_sha256: str) -> dict:
        target = self._path(path)
        original, before = self._checked(target, expected_sha256)
        if not old_text:
            raise ToolError("old_text must not be empty.")
        # Prefer the literal match; allow LF input against a CRLF file as a convenience.
        if old_text not in before:
            old_text = old_text.replace("\r\n", "\n").replace("\n", _newline(before))
        newline = _newline(old_text) if "\n" in old_text else _newline(before)
        new_text = new_text.replace("\r\n", "\n").replace("\n", newline)
        first = before.find(old_text)
        if first < 0:
            raise ToolError("old_text matched 0 times; exactly one match is required.")
        if before.find(old_text, first + 1) >= 0:
            raise ToolError("old_text matched at least 2 times; exactly one match is required.")
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
        if not self.approve("file", self.redact(diff)):
            return {"ok": False, "error": "User declined file changes.", "declined": True}
        staged: list[tuple[Path, Path]] = []
        applied = []
        try:
            # Stage everything and recheck all inputs after approval, before the first write.
            for target, original, updated in changes:
                if self._path(str(target)) != target:
                    raise ToolError("Path changed while awaiting approval.")
                self._checked(target, _sha(original))
                staged.append((target, self._stage(target, updated, stat.S_IMODE(target.stat().st_mode))))
            for target, original, _ in changes:
                self._checked(target, _sha(original))
            for target, temporary in staged:
                os.replace(temporary, target)
                applied.append(self._relative(target))
        except OSError as exc:
            # Multi-file filesystem transactions do not exist here; explicitly report partial writes.
            return {"ok": False, "error": str(exc), "applied_files": applied,
                    "partial_write": bool(applied), "guidance": "Read affected files before retrying; existing changes were not rolled back."}
        finally:
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)
        return {"ok": True, "changed": True,
                "files": [{"path": self._relative(path), "sha256": _sha(after)} for path, _, after in changes],
                "diff": _bounded_text(diff), "diff_truncated": len(diff) > MAX_OUTPUT_CHARS}

    def _apply_patch(self, patch: str, expected_sha256: dict) -> dict:
        sections = _parse_patch(patch)
        changes = []
        seen: set[Path] = set()
        keys: set[str] = set()
        for name, hunks in sections:
            target = self._path(name)
            if target in seen:
                raise ToolError("A patch must contain only one section per file.")
            seen.add(target)
            key = self._relative(target)
            keys.add(key)
            if key not in expected_sha256:
                raise ToolError(f"Missing expected_sha256 for {key}.")
            original, content = self._checked(target, expected_sha256[key])
            updated = _apply_hunks(content, hunks)
            changes.append((target, original, updated.encode("utf-8")))
        if set(expected_sha256) != keys:
            raise ToolError("expected_sha256 keys must exactly match the patch's workspace-relative paths.")
        return self._commit(changes)

    def _run_command(self, command: str, cwd: str = ".", timeout: float = 120, background: bool = False) -> dict:
        if not command.strip():
            raise ToolError("command must not be empty.")
        self._path(cwd, directory=True)
        return self.process_manager.run(command=command, cwd=cwd, timeout=timeout, background=background)

    def _poll_command(self, job_id: str, offset: int = 0) -> dict:
        return self.process_manager.poll(job_id, offset=offset)

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
                    raise ToolError("Patch refers past the end of the file.")
                current = source[cursor]
                actual_terminated = current.endswith(("\n", "\r"))
                if current.rstrip("\r\n") != text or actual_terminated != terminated:
                    raise ToolError(f"Patch context does not match at line {cursor + 1}; read the file again.")
                cursor += 1
                if operation == " ":
                    output.append(current)
            elif operation == "+":
                output.append(text + (newline if terminated else ""))
    output.extend(source[cursor:])
    if any(not line.endswith(("\n", "\r")) for line in output[:-1]):
        raise ToolError("A no-newline marker may only describe the final line of a file.")
    return "".join(output)

"""Bounded shell jobs owned by one MiniAgent run.

The working-directory check and destructive-Git guard are conveniences, not a
sandbox. An approved shell command has the operating-system user's privileges.
"""

from __future__ import annotations

import codecs
import ctypes
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable
import uuid


MAX_CAPTURE_BYTES = 1024 * 1024
PAGE_BYTES = 16 * 1024
MAX_JOBS = 32


def shell_description() -> str:
    """Return the executable used for command tools, not the parent terminal."""
    if os.name == "nt":
        return (shutil.which("pwsh") or shutil.which("powershell")
                or os.environ.get("COMSPEC", "cmd.exe"))
    return "/bin/sh"


def _shell_arguments(shell: str, command: str) -> list[str]:
    if os.name != "nt":
        return [shell, "-c", command]
    if Path(shell).stem.lower() in {"pwsh", "powershell"}:
        # PowerShell 5 defaults to legacy encodings. Use a consistent UTF-8
        # boundary for its own output and for native programs attached to it.
        prefix = ("[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
                  "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                  "$OutputEncoding = [Console]::OutputEncoding; "
                  "$ErrorActionPreference = 'Stop'; $global:LASTEXITCODE = 0;\n")
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                prefix + command + "\nexit $LASTEXITCODE"]
    return [shell, "/d", "/s", "/c", command]


class _WindowsJob:
    """A kernel job keeps descendants attached, including after shell exit."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.TerminateJobObject.restype = wintypes.BOOL
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise error

    def attach_and_resume(self, process: subprocess.Popen) -> None:
        from ctypes import wintypes

        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

        # Popen closes its primary thread handle. Find it while the new process
        # is still suspended, attach the process to our job, and only then run it.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        api = self.api
        api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.Thread32First.restype = wintypes.BOOL
        api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.Thread32Next.restype = wintypes.BOOL
        api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenThread.restype = wintypes.HANDLE
        api.ResumeThread.argtypes = [wintypes.HANDLE]
        api.ResumeThread.restype = wintypes.DWORD
        snapshot = api.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == process.pid:
                    thread = api.OpenThread(2, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        api.CloseHandle(thread)
                more = api.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("Could not find the suspended shell thread")
        finally:
            api.CloseHandle(snapshot)

    def stop(self) -> None:
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            self.api.CloseHandle(self.handle)
            self.handle = None


class _Job:
    def __init__(self, job_id: str, process: subprocess.Popen, cwd: Path,
                 timeout: float, windows_job: _WindowsJob | None) -> None:
        self.job_id = job_id
        self.process = process
        self.cwd = cwd
        self.timeout = timeout
        self.windows_job = windows_job
        self.started = time.monotonic()
        self.ended: float | None = None
        self.output = bytearray()
        self.dropped_bytes = 0
        self.reason: str | None = None
        self.capture_error: str | None = None
        self.lock = threading.RLock()
        self.kill_lock = threading.Lock()
        self.done = threading.Event()
        self.reader: threading.Thread | None = None
        self.monitor: threading.Thread | None = None


class ProcessManager:
    """Run approved shell commands; job IDs are valid only in this instance.

    Output offsets count UTF-8 bytes after secret redaction. At most 1 MiB is
    retained per job, and each response contains at most 16 KiB. Older completed
    jobs are evicted when the 32-job limit is reached. No output log is written.
    """

    def __init__(self, workspace: Path, approve: Callable[[str, str], bool],
                 redact: Callable[[str], str], secrets: tuple[str, ...] = ()) -> None:
        self.workspace = Path(workspace).resolve()
        self.shell = shell_description()
        self.approve = approve
        self.redact = redact
        self.secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
        self._jobs: dict[str, _Job] = {}
        self._closed = False

    def _safe(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return self.redact(value)

    def _error(self, message: str, **extra: object) -> dict:
        return {"ok": False, "error": self._safe(message), **extra}

    def _environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items()
                       if not re.search(r"api[_-]?key", key, re.IGNORECASE)
                       and not any(secret in value for secret in self.secrets)}
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    @staticmethod
    def _destructive_git(command: str) -> bool:
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            # Malformed quoting is left to the shell; retain a conservative
            # fallback for obvious destructive operations.
            tokens = command.replace(";", " ; ").split()
        for index, token in enumerate(tokens):
            if token.replace("\\", "/").rsplit("/", 1)[-1].lower() not in {"git", "git.exe"}:
                continue
            arguments = []
            for argument in tokens[index + 1:]:
                if argument and all(char in ";&|\n" for char in argument):
                    break
                arguments.append(argument)
            for subindex, subcommand in enumerate(arguments):
                if subcommand not in {"reset", "clean", "checkout", "restore"}:
                    continue
                rest = arguments[subindex + 1:]
                if subcommand == "reset" and "--hard" in rest:
                    return True
                if subcommand == "clean" and any(arg == "--force" or
                        (arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:])
                        for arg in rest):
                    return True
                if subcommand in {"checkout", "restore"} and any(arg in {".", "./", ".\\", ":/"} for arg in rest):
                    return True
                break
        return False

    def run(self, command: str, cwd: str = ".", timeout: float = 120,
            background: bool = False) -> dict:
        if self._closed:
            return self._error("This process manager has been closed.")
        if not isinstance(command, str) or not command.strip():
            return self._error("command must be a nonempty string.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
            return self._error("timeout must be a positive number of seconds, at most 86400 (one day).")
        if not isinstance(background, bool):
            return self._error("background must be a boolean.")
        if cwd is not None and not isinstance(cwd, str):
            return self._error("cwd must be a path string.")
        try:
            directory = (self.workspace / (cwd or ".")).resolve()
            directory.relative_to(self.workspace)
            if not directory.is_dir():
                return self._error("cwd must be an existing directory.")
        except (ValueError, OSError, RuntimeError) as exc:
            return self._error(f"cwd must resolve inside the workspace: {exc}")
        if self._destructive_git(command):
            return self._error("Refused a destructive Git operation. Run it yourself if intended; MiniAgent preserves existing work.")
        try:
            approved = self.approve("shell", self._safe(f"cwd: {directory}\ncommand: {command}"))
        except Exception as exc:
            return self._error(f"Command approval failed: {exc}")
        if not approved:
            return self._error("Shell command was not approved.", denied=True)
        if len(self._jobs) >= MAX_JOBS:
            oldest = next((key for key, job in self._jobs.items() if job.done.is_set()), None)
            if oldest is None:
                return self._error(f"All {MAX_JOBS} job slots are active; cancel or wait for a job first.")
            del self._jobs[oldest]

        process = None
        windows_job = None
        job = None
        try:
            options: dict = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 4}  # CREATE_SUSPENDED
            if os.name == "nt":
                windows_job = _WindowsJob()
            process = subprocess.Popen(_shell_arguments(self.shell, command), shell=False, cwd=directory,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, env=self._environment(),
                                       bufsize=0, **options)
            if windows_job is not None:
                windows_job.attach_and_resume(process)
            job_id = uuid.uuid4().hex[:12]
            job = _Job(job_id, process, directory, float(timeout), windows_job)
            self._jobs[job_id] = job
            job.reader = threading.Thread(target=self._capture, args=(job,), daemon=True)
            job.monitor = threading.Thread(target=self._watch, args=(job,), daemon=True)
            job.reader.start()
            job.monitor.start()
            if not background:
                job.done.wait()
            return self._result(job, 0)
        except BaseException as exc:
            if job is not None:
                self._cancel(job)
            elif process is not None:
                if windows_job is not None:
                    windows_job.stop()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()
            elif windows_job is not None:
                windows_job.stop()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return self._error(f"Could not execute command: {exc}")

    def _append(self, job: _Job, text: str) -> None:
        data = self._safe(text).encode("utf-8")
        with job.lock:
            remaining = 0 if job.dropped_bytes else MAX_CAPTURE_BYTES - len(job.output)
            kept = data[:remaining]
            # Do not retain a partial UTF-8 code point at the capture boundary.
            kept = kept.decode("utf-8", errors="ignore").encode("utf-8")
            job.output.extend(kept)
            job.dropped_bytes += len(data) - len(kept)

    def _capture(self, job: _Job) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            assert job.process.stdout is not None
            while True:
                chunk = job.process.stdout.read(4096)
                if not chunk:
                    pending += decoder.decode(b"", final=True)
                    self._append(job, pending)
                    break
                pending = self._safe(pending + decoder.decode(chunk))
                cut = len(pending)
                # Retain only a suffix that could become a secret. Holding an
                # arbitrary fixed tail would hide short progress messages until
                # the process exits, even when they contain no secret prefix.
                for secret in self.secrets:
                    for length in range(min(len(secret) - 1, len(pending)), 0, -1):
                        if pending.endswith(secret[:length]):
                            cut = min(cut, len(pending) - length)
                            break
                if cut:
                    self._append(job, pending[:cut])
                    pending = pending[cut:]
        except Exception as exc:
            with job.lock:
                job.capture_error = self._safe(f"Output capture failed: {exc}")
        finally:
            if job.process.stdout:
                job.process.stdout.close()

    @staticmethod
    def _stop_tree(job: _Job) -> None:
        with job.kill_lock:
            if job.windows_job is not None:
                job.windows_job.stop()
            else:
                try:
                    os.killpg(job.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def _watch(self, job: _Job) -> None:
        try:
            try:
                job.process.wait(timeout=max(0.001, job.timeout - (time.monotonic() - job.started)))
            except subprocess.TimeoutExpired:
                with job.lock:
                    if job.reason is None:
                        job.reason = "timed_out"
            finally:
                # A shell can exit while its descendants still hold stdout.
                # The job owns those descendants and cleans them up as well.
                self._stop_tree(job)
                job.process.wait()
                if job.reader is not None:
                    job.reader.join(timeout=3)
        finally:
            with job.lock:
                job.ended = time.monotonic()
            job.done.set()

    def _result(self, job: _Job, offset: int) -> dict:
        with job.lock:
            size = len(job.output)
            if offset > size:
                return self._error("offset exceeds the captured output; use next_offset from a previous result.", job_id=job.job_id)
            if offset < size and job.output[offset] & 0xC0 == 0x80:
                return self._error("offset splits a UTF-8 character; use next_offset from a previous result.", job_id=job.job_id)
            end = min(offset + PAGE_BYTES, size)
            data = bytes(job.output[offset:end])
            if end < size:
                # The next page starts on a complete UTF-8 character.
                text = data.decode("utf-8", errors="ignore")
                end = offset + len(text.encode("utf-8"))
            else:
                text = data.decode("utf-8", errors="replace")
            complete = job.done.is_set()
            status = (job.reason or "completed") if complete else "running"
            code = job.process.returncode if complete else None
            result = {"ok": job.capture_error is None and status not in {"timed_out", "cancelled"} and (code in (None, 0)),
                      "job_id": job.job_id, "status": status, "complete": complete,
                      "output": self._safe(text), "exit_code": code,
                      "elapsed": round((job.ended or time.monotonic()) - job.started, 3),
                      "cwd": self._safe(str(job.cwd)), "offset": offset, "next_offset": end,
                      "has_more": end < size, "truncated": end < size or job.dropped_bytes > 0,
                      "captured_bytes": size, "dropped_bytes": job.dropped_bytes,
                      "output_limit_reached": job.dropped_bytes > 0}
            if job.capture_error:
                result["error"] = job.capture_error
            return result

    def poll(self, job_id: str, offset: int = 0) -> dict:
        if not isinstance(job_id, str) or job_id not in self._jobs:
            return self._error("Unknown job ID in this run. Its previous outcome is uncertain; the command has not been replayed.", uncertain=True)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return self._error("offset must be a nonnegative byte offset.")
        return self._result(self._jobs[job_id], offset)

    def _cancel(self, job: _Job) -> None:
        if job.done.is_set():
            return
        with job.lock:
            if job.reason is None:
                job.reason = "cancelled"
        self._stop_tree(job)
        job.process.wait()
        # No monitor exists if thread creation failed during startup.
        if job.monitor is None or job.monitor.ident is None:
            if job.reader is not None and job.reader.ident is not None:
                job.reader.join(timeout=3)
            elif job.process.stdout:
                job.process.stdout.close()
            job.ended = time.monotonic()
            job.done.set()
        else:
            job.done.wait(timeout=5)

    def cancel(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or job_id not in self._jobs:
            return self._error("Unknown job ID in this run. No process was cancelled; its previous outcome is uncertain.", uncertain=True)
        job = self._jobs[job_id]
        self._cancel(job)
        return self._result(job, 0)

    def close(self) -> None:
        self._closed = True
        for job in list(self._jobs.values()):
            self._cancel(job)

    def __enter__(self) -> ProcessManager:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

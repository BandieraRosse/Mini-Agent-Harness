#!/usr/bin/env python3
"""Install a portable MiniAgent release using only the Python standard library."""

import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen


MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
LAUNCHER_MARKER = "# MiniAgent managed launcher"
WINDOWS = os.name == "nt"


def default_prefix():
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "MiniAgent"
    return Path.home() / ".local/share/miniagent"


def launcher_text(release):
    if WINDOWS:
        # UTF-8 also supports non-ASCII user/project paths. Disable delayed
        # expansion so exclamation marks in paths and arguments survive.
        paths = [sys.executable, str(release / "agent.py")]
        if any(any(char in path for char in '\"\r\n') for path in paths):
            raise ValueError("Unsupported quote or newline in launcher path")
        python, entry = (path.replace("%", "%%") for path in paths)
        return ("@echo off\nsetlocal DisableDelayedExpansion\n"
                "rem " + LAUNCHER_MARKER + "\n"
                'for /f "tokens=2 delims=:" %%G in (\'chcp\') do set "miniagent_cp=%%G"\n'
                'chcp 65001 >nul\n'
                f'"{python}" "{entry}" %*\n'
                'set "miniagent_exit=%errorlevel%"\n'
                'chcp %miniagent_cp% >nul\nexit /b %miniagent_exit%\n')
    return ("#!/bin/sh\n" + LAUNCHER_MARKER + "\nexec " + shlex.quote(sys.executable)
            + " " + shlex.quote(str(release / "agent.py")) + ' "$@"\n')


def path_command(bin_dir):
    directory = str(Path(bin_dir).expanduser().resolve())
    if WINDOWS:
        return "$env:Path = '" + directory.replace("'", "''") + ";' + $env:Path"
    return "export PATH=" + shlex.quote(directory) + ':"$PATH"'


def notify_environment_change():
    """Tell desktop applications to refresh the persisted user environment."""
    import ctypes
    from ctypes import wintypes

    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))


def base_url(value):
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or any(char.isspace() for char in value)):
        raise ValueError("Use an HTTP(S) distribution URL without credentials or query parameters")
    return value.rstrip("/")


def download(url, limit):
    with urlopen(url, timeout=30) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Download exceeds the size limit")
    return content


def release_manifest(content):
    data = json.loads(content)
    version = data.get("version", "") if isinstance(data, dict) else ""
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("Invalid release version")
    if data.get("filename") != f"miniagent-{version}.tar.gz":
        raise ValueError("Invalid release filename")
    if not isinstance(data.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
        raise ValueError("Invalid release checksum")
    if type(data.get("size")) is not int or not 0 < data["size"] <= MAX_DOWNLOAD:
        raise ValueError("Invalid release size")
    minimum = data.get("python_min", "")
    if not isinstance(minimum, str) or not re.fullmatch(r"3\.\d+", minimum):
        raise ValueError("Invalid Python requirement")
    if sys.version_info[:2] < tuple(map(int, minimum.split("."))):
        raise ValueError(f"This release needs Python {minimum} or newer")
    return data


def unpack(archive, destination, version):
    """Extract regular files only, with one known top-level directory."""
    top = f"miniagent-{version}"
    total, seen = 0, set()
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle:
            path = PurePosixPath(member.name)
            if (not path.parts or path.parts[0] != top or path.is_absolute()
                    or ".." in path.parts or "\\" in member.name or ":" in member.name
                    or not (member.isfile() or member.isdir())
                    or member.name in seen or len(seen) >= 10_000):
                raise ValueError("Unsafe or invalid release archive")
            seen.add(member.name)
            total += member.size
            if member.size < 0 or total > MAX_UNPACKED:
                raise ValueError("Unpacked release exceeds the size limit")
            target = destination.joinpath(*path.parts)
            target.resolve().relative_to(destination.resolve())
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)
    root = destination / top
    if not (root / "agent.py").is_file() or not (root / "miniagent" / "__init__.py").is_file():
        raise ValueError("Release is missing the MiniAgent entry point")
    return root


def install(url, prefix, bin_dir):
    url = base_url(url)
    prefix, bin_dir = Path(prefix).expanduser().resolve(), Path(bin_dir).expanduser().resolve()
    launcher = bin_dir / ("miniagent.cmd" if WINDOWS else "miniagent")
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or not launcher.is_file():
            raise ValueError(f"Existing command is not managed by MiniAgent: {launcher}")
        if LAUNCHER_MARKER not in launcher.read_text(encoding="utf-8"):
            raise ValueError(f"Existing command is not managed by MiniAgent: {launcher}")
    manifest = release_manifest(download(url + "/manifest.json", 16_384))
    archive = download(url + "/" + manifest["filename"], manifest["size"])
    if len(archive) != manifest["size"] or hashlib.sha256(archive).hexdigest() != manifest["sha256"]:
        raise ValueError("Release checksum/size mismatch; existing installation was kept")
    prefix.mkdir(parents=True, exist_ok=True)
    releases = prefix / "releases"
    if releases.is_symlink():
        raise ValueError("Release storage must not be a symbolic link")
    releases.mkdir(exist_ok=True)
    releases.resolve().relative_to(prefix)
    release = releases / (manifest["version"] + "-" + manifest["sha256"][:16])
    # Staging and cleanup stay within the explicit installation directory.
    with tempfile.TemporaryDirectory(prefix=".install-", dir=prefix) as temporary:
        staging = Path(temporary).resolve()
        staging.relative_to(prefix)
        bundle = staging / "release.tar.gz"
        bundle.write_bytes(archive)
        extracted = unpack(bundle, staging, manifest["version"])
        marker = release / ".release-sha256"
        if release.exists() or release.is_symlink():
            if release.is_symlink() or not marker.is_file() or marker.read_text() != manifest["sha256"]:
                raise ValueError("Existing release directory is not a verified MiniAgent installation")
        else:
            (extracted / ".release-sha256").write_text(manifest["sha256"], encoding="ascii")
            extracted.rename(release)
        bin_dir.mkdir(parents=True, exist_ok=True)
        text = launcher_text(release)
        fd, name = tempfile.mkstemp(prefix=".miniagent-", dir=bin_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n" if WINDOWS else "\n") as handle:
                handle.write(text)
            os.chmod(name, 0o755)
            os.replace(name, launcher)
        finally:
            Path(name).unlink(missing_ok=True)
    return manifest, launcher


def add_to_path(bin_dir):
    """Append one idempotent PATH line; do not rewrite existing shell settings."""
    if WINDOWS:
        import winreg

        directory = str(Path(bin_dir).expanduser().resolve())
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                value, kind = "", winreg.REG_EXPAND_SZ
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                raise ValueError("User PATH is not a string")
            normalize = lambda path: ntpath.normcase(ntpath.normpath(os.path.expandvars(path)))
            if normalize(directory) not in {normalize(part) for part in value.split(";") if part}:
                winreg.SetValueEx(key, "Path", 0, kind, value + (";" if value and not value.endswith(";") else "") + directory)
        notify_environment_change()
        return path_command(bin_dir)
    line = "export PATH=" + shlex.quote(str(Path(bin_dir).expanduser().resolve())) + ':"$PATH"'
    shell = Path(os.environ.get("SHELL", "sh")).name
    files = [Path.home() / ".profile"]
    if shell in {"bash", "zsh"}:
        files.append(Path.home() / (".bashrc" if shell == "bash" else ".zshrc"))
    for path in files:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        if line not in content.splitlines():
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n# MiniAgent command\n" + line + "\n")
    return line


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Distribution service URL, e.g. http://server:8765")
    parser.add_argument("--prefix", type=Path, default=default_prefix())
    parser.add_argument("--bin-dir", type=Path, default=default_prefix() / "bin" if WINDOWS else Path.home() / ".local/bin")
    parser.add_argument("--add-to-path", action="store_true", help="Add the command directory to user PATH or shell startup files")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.exit(1, "MiniAgent requires Python 3.10 or newer\n")
    try:
        manifest, launcher = install(args.url, args.prefix, args.bin_dir)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"Install failed: {error}\n")
    print(f"Installed MiniAgent {manifest['version']}: {launcher}")
    if args.add_to_path:
        try:
            line = add_to_path(args.bin_dir)
            print("User PATH configured. For this shell, run:\n  " + line)
        except (OSError, ValueError):
            print("Installed successfully; PATH could not be configured. Run:\n  " + path_command(launcher.parent))
    elif str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print("Add the command to this shell:\n  " + path_command(launcher.parent))
    print("Run: miniagent -C " + (r"C:\path\to\project" if WINDOWS else "/path/to/project")
          + "\nThe API key is requested privately on first start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

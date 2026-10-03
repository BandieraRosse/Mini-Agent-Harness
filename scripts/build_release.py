"""Build a portable, pure Python MiniAgent release using only the standard library."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = {"prompt_toolkit": "3.0.53", "wcwidth": "0.9.1"}
PYTHON_MIN = "3.10"
MAX_WHEEL = 8 * 1024 * 1024
MAX_UNPACKED = 32 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024
WHEEL_CACHE = ROOT / "dist" / "wheels"


def _cached_wheel(source):
    checksum = source.with_suffix(source.suffix + ".sha256")
    try:
        if source.is_symlink() or checksum.is_symlink():
            return None
        with checksum.open("rb") as stream:
            expected = stream.read(66).strip()
        if not re.fullmatch(rb"[0-9a-f]{64}", expected):
            return None
        with source.open("rb") as stream:
            data = stream.read(MAX_WHEEL + 1)
        if len(data) <= MAX_WHEEL and hashlib.sha256(data).hexdigest().encode() == expected:
            return data
    except OSError:
        pass
    return None


def _fetch(url, limit):
    if urlsplit(url).scheme != "https":
        raise ValueError("Dependency downloads require HTTPS")
    with urlopen(url, timeout=60) as response:
        if urlsplit(response.geturl()).scheme != "https":
            raise ValueError("Dependency download redirected away from HTTPS")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Dependency download exceeds size limit")
    return data


def _wheel(name, version, wheel_dir):
    filename = f"{name}-{version}-py3-none-any.whl"
    if wheel_dir is not None:
        source = Path(wheel_dir) / filename
        if source.stat().st_size > MAX_WHEEL:
            raise ValueError(f"Wheel exceeds size limit: {filename}")
        print(f"Using local dependency: {filename}", file=sys.stderr, flush=True)
        return source.read_bytes()
    source = WHEEL_CACHE / filename
    cached = _cached_wheel(source)
    if cached is not None:
        print(f"Using cached dependency: {filename}", file=sys.stderr, flush=True)
        return cached
    print(f"Downloading dependency: {filename}", file=sys.stderr, flush=True)
    metadata = json.loads(_fetch(f"https://pypi.org/pypi/{name}/{version}/json", 1024 * 1024))
    matches = [item for item in metadata["urls"] if item["filename"] == filename]
    if len(matches) != 1 or matches[0].get("yanked"):
        raise ValueError(f"No supported pure Python wheel found: {filename}")
    item = matches[0]
    if urlsplit(item["url"]).hostname != "files.pythonhosted.org":
        raise ValueError("Unexpected PyPI download host")
    data = _fetch(item["url"], MAX_WHEEL)
    if hashlib.sha256(data).hexdigest() != item["digests"]["sha256"]:
        raise ValueError(f"SHA256 mismatch: {filename}")
    # Store only verified portable wheels. Writing the checksum last makes
    # interrupted updates a cache miss on the next build.
    _wheel_files(data, name, version)
    WHEEL_CACHE.mkdir(parents=True, exist_ok=True)
    _atomic_write(source, data)
    _atomic_write(source.with_suffix(source.suffix + ".sha256"),
                  (item["digests"]["sha256"] + "\n").encode("ascii"))
    return data


def _wheel_files(data, name, version):
    """Validate before unpacking; never extract a wheel onto the build machine."""
    dist_info = f"{name}-{version}.dist-info"
    result = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        entries = wheel.infolist()
        if len(entries) > 4096:
            raise ValueError("Wheel contains too many files")
        for info in entries:
            path = PurePosixPath(info.filename)
            parts = path.parts
            if (not parts or info.orig_filename != info.filename or path.is_absolute()
                    or "\\" in info.filename or ":" in info.filename
                    or any(ord(char) < 32 for char in info.filename)
                    or any(part in {".", "..", ""} for part in info.filename.rstrip("/").split("/"))
                    or parts[0] not in {name, dist_info}):
                raise ValueError(f"Unsafe or unexpected wheel path: {info.filename}")
            mode = info.external_attr >> 16
            if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(f"Wheel contains a special file: {info.filename}")
            if info.is_dir():
                continue
            if info.filename in result:
                raise ValueError(f"Duplicate wheel file: {info.filename}")
            if "__pycache__" in parts or path.suffix.lower() in {
                ".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib", ".exe", ".pth"
            }:
                raise ValueError(f"Wheel contains nonportable code: {info.filename}")
            if parts[0] == name and path.suffix not in {".py", ".pyi"} and path.name != "py.typed":
                raise ValueError(f"Unexpected dependency package file: {info.filename}")
            total += info.file_size
            if info.file_size > MAX_FILE or total > MAX_UNPACKED:
                raise ValueError("Unpacked wheel exceeds size limit")
            result[info.filename] = wheel.read(info)
    from email.parser import BytesParser
    parser = BytesParser()
    metadata = parser.parsebytes(result.get(f"{dist_info}/METADATA", b""))
    wheel_metadata = parser.parsebytes(result.get(f"{dist_info}/WHEEL", b""))
    normalized_name = re.sub(r"[-_.]+", "_", metadata.get("Name", "")).lower()
    if normalized_name != name or metadata.get("Version") != version:
        raise ValueError("Wheel package name or version does not match the pinned dependency")
    if (wheel_metadata.get("Root-Is-Purelib", "").lower() != "true"
            or wheel_metadata.get_all("Tag") != ["py3-none-any"]):
        raise ValueError("Only py3-none-any pure Python wheels are supported")
    if not any(path.startswith(f"{dist_info}/") and "LICENSE" in path.upper() for path in result):
        raise ValueError("Dependency wheel is missing its license")
    return result


def _source_files():
    paths = [ROOT / "agent.py", ROOT / "miniagent" / "instructions.md"]
    paths.extend(sorted((ROOT / "miniagent").glob("*.py")))
    files = {}
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ValueError(f"Invalid release source: {path.name}")
        files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    return files


def _atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_release(output, wheel_dir=None):
    """Publish archive then manifest; reuse verified wheels or accept offline inputs."""
    print("Building MiniAgent release...", file=sys.stderr, flush=True)
    version_text = (ROOT / "miniagent" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*[\'"]([0-9]+\.[0-9]+\.[0-9]+)[\'"]', version_text, re.M)
    if not match:
        raise ValueError("Cannot read MiniAgent version")
    version = match.group(1)
    files = _source_files()
    for name, dependency_version in DEPENDENCIES.items():
        files.update(_wheel_files(_wheel(name, dependency_version, wheel_dir), name, dependency_version))
    prefix = f"miniagent-{version}"
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content in sorted(files.items()):
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size, info.mode, info.mtime = len(content), 0o644, 0
                archive.addfile(info, io.BytesIO(content))
    data = buffer.getvalue()
    manifest = {"version": version, "python_min": PYTHON_MIN, "filename": f"{prefix}.tar.gz",
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    _atomic_write(output / manifest["filename"], data)
    _atomic_write(output / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    print(f"Release ready: {output / manifest['filename']}", file=sys.stderr, flush=True)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "releases")
    parser.add_argument("--wheel-dir", type=Path, help="Offline directory of the two pinned py3-none-any wheels")
    args = parser.parse_args(argv)
    try:
        manifest = build_release(args.output, args.wheel_dir)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Build failed: {error}\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

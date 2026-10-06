"""Serve the small MiniAgent release using only the Python standard library."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def _regular_file(directory: Path, name: str) -> Path:
    """Accept a direct regular child; never follow a published symlink."""
    path = directory / name
    if path.is_symlink() or path.resolve().parent != directory:
        raise FileNotFoundError(name)
    if not stat.S_ISREG(path.stat().st_mode):
        raise FileNotFoundError(name)
    return path


def _manifest(directory: Path) -> dict:
    path = _regular_file(directory, "manifest.json")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("manifest is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid manifest")
    version = value.get("version")
    filename = value.get("filename")
    digest = value.get("sha256")
    size = value.get("size")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ValueError("invalid release version")
    if filename != f"miniagent-{version}.tar.gz":
        raise ValueError("invalid release filename")
    if value.get("python_min") != "3.10":
        raise ValueError("unsupported Python requirement")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid release checksum")
    if type(size) is not int or size < 0:
        raise ValueError("invalid release size")
    if _regular_file(directory, filename).stat().st_size != size:
        raise ValueError("release size does not match manifest")
    return {key: value[key] for key in ("version", "python_min", "filename", "sha256", "size")}


def create_server(
    directory: str | Path = "dist/releases",
    bind: str = "127.0.0.1",
    port: int = 8765,
    scripts_directory: str | Path | None = None,
) -> ThreadingHTTPServer:
    """Create an unstarted server; port=0 is useful for local integration tests."""
    releases = Path(directory).resolve()
    scripts = Path(scripts_directory or Path(__file__).parent).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "MiniAgentDistribution/1"
        sys_version = ""

        def do_GET(self) -> None:
            self._serve(head=False)

        def do_HEAD(self) -> None:
            self._serve(head=True)

        def _serve(self, head: bool) -> None:
            # Match the literal URL path: encoded separators and traversal never
            # become file paths, and no directory listing is implemented.
            try:
                route = urlsplit(self.path).path
            except ValueError:
                self.send_error(400)
                return
            file_path = None
            try:
                if route == "/":
                    payload = (
                        "MiniAgent distribution\n\n"
                        "curl -fsSL http://HOST:8765/install.sh | sh -s -- http://HOST:8765\n\n"
                        "Windows PowerShell:\n"
                        "$server='http://HOST:8765'; & ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing \""
                        "$server/install.ps1\").Content)) -Url $server -AddToPath\n\n"
                        "Available: /install.sh /install.ps1 /install.py /manifest.json and the release archive.\n"
                    ).encode("utf-8")
                    content_type = "text/plain; charset=utf-8"
                elif route in ("/install.sh", "/install.ps1", "/install.py"):
                    file_path = _regular_file(scripts, route[1:])
                    content_type = "text/plain; charset=utf-8"
                else:
                    manifest = _manifest(releases)
                    if route == "/manifest.json":
                        payload = (json.dumps(manifest, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
                        content_type = "application/json; charset=utf-8"
                    elif route == "/" + manifest["filename"]:
                        file_path = _regular_file(releases, manifest["filename"])
                        content_type = "application/gzip"
                    else:
                        self.send_error(404)
                        return
                if file_path is None:
                    self._headers(content_type, len(payload))
                    if not head:
                        self.wfile.write(payload)
                else:
                    with file_path.open("rb") as stream:
                        self._headers(content_type, file_path.stat().st_size)
                        if not head:
                            shutil.copyfileobj(stream, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (OSError, ValueError, TypeError):
                self.send_error(404, "Release file unavailable")

        def _headers(self, content_type: str, size: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

    return ThreadingHTTPServer((bind, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="listen address (default: local machine only)")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--directory", default="dist/releases", type=Path)
    parser.add_argument("--build", action="store_true", help="build a release before serving")
    parser.add_argument("--wheel-dir", type=Path, help="use already downloaded dependency wheels when building")
    args = parser.parse_args(argv)
    if args.wheel_dir and not args.build:
        parser.error("--wheel-dir requires --build")
    if args.build:
        from build_release import build_release

        build_release(args.directory, args.wheel_dir)
    try:
        _manifest(args.directory.resolve())
        server = create_server(args.directory, args.bind, args.port)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"Cannot serve release: {error}\n")
    print(f"MiniAgent distribution: http://{args.bind}:{server.server_port}", flush=True)
    print(f"Release directory: {args.directory.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

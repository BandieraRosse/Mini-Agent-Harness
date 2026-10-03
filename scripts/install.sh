#!/bin/sh
# Bootstrap with system Python; curl/wget is only needed to fetch this file.
set -eu
if [ -n "${PYTHON:-}" ]; then
    python_bin=$PYTHON
elif command -v python3 >/dev/null 2>&1; then
    python_bin=python3
elif command -v python >/dev/null 2>&1; then
    python_bin=python
else
    echo "MiniAgent requires Python 3.10 or newer." >&2
    exit 1
fi
exec "$python_bin" - "$@" <<'PY'
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen

if sys.version_info < (3, 10):
    sys.exit("MiniAgent requires Python 3.10 or newer.")
if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    sys.exit("Usage: sh install.sh http://server:8765 [--add-to-path] [--prefix DIR] [--bin-dir DIR]")
url = sys.argv[1].rstrip("/")
parts = urlsplit(url)
if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username
        or parts.password or parts.query or parts.fragment or any(c.isspace() for c in url)):
    sys.exit("Use an HTTP(S) distribution URL without credentials or query parameters.")
try:
    with urlopen(url + "/install.py", timeout=30) as response:
        source = response.read(128 * 1024 + 1)
    if len(source) > 128 * 1024:
        sys.exit("Installer exceeds the size limit.")
except OSError as error:
    sys.exit("Unable to download installer: " + str(error))
exec(compile(source, "miniagent-install", "exec"), {"__name__": "__main__"})
PY

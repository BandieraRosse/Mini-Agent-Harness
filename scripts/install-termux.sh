#!/bin/sh
# Run explicitly with sh inside Termux; no Android system shebang is required.
set -eu
server=${1:-http://124.221.221.10:8765}
server=${server%/}
if [ -z "${PREFIX:-}" ] || ! command -v pkg >/dev/null 2>&1; then
    echo "Run this installer inside Termux." >&2
    exit 1
fi
pkg update -y </dev/null
pkg install -y python curl termux-api </dev/null
temporary=$(mktemp)
trap 'rm -f "$temporary"' 0
trap 'exit 1' HUP INT TERM
curl -fsSL "$server/install.sh" -o "$temporary"
sh "$temporary" "$server" --add-to-path
echo 'Installation complete. Open a new Termux session, then run: miniagent'
echo 'Clipboard: also install the Termux:API Android app from the same source as Termux.'

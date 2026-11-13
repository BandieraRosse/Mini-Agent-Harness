#!/bin/sh
# Run explicitly with sh inside Termux; no Android system shebang is required.
set -eu
server=${1:-http://124.221.221.10:8765}
server=${server%/}
if [ -z "${PREFIX:-}" ] || ! command -v pkg >/dev/null 2>&1; then
    echo "Run this installer inside Termux." >&2
    exit 1
fi
yellow=''
green=''
reset=''
if [ -t 1 ] && [ -z "${NO_COLOR+x}" ] && [ "${TERM:-}" != dumb ]; then
    yellow=$(printf '\033[33m')
    green=$(printf '\033[32m')
    reset=$(printf '\033[0m')
fi
printf '%s安装或更新需要升级 Termux 软件包并下载文件，速度可能较慢；建议使用加速器或更换软件源。%s\n' "$yellow" "$reset"
# Avoid partial upgrades and pkg mirror checks that need a working curl.
export DEBIAN_FRONTEND=noninteractive
# Resume interrupted configuration and keep existing conffiles without prompts.
dpkg --force-confold --configure --pending </dev/null
printf '%s正在更新软件包索引并升级已有软件包…%s\n' "$yellow" "$reset"
apt update -y </dev/null
apt full-upgrade -y -o Dpkg::Options::=--force-confold </dev/null
apt install -y -o Dpkg::Options::=--force-confold python curl termux-api </dev/null
printf '%s正在下载并安装 MiniAgent；更新会保留用户配置和项目会话。%s\n' "$yellow" "$reset"
temporary=$(mktemp)
trap 'rm -f "$temporary"' 0
trap 'exit 1' HUP INT TERM
curl -fsSL "$server/install.sh" -o "$temporary"
sh "$temporary" "$server" --add-to-path
printf '%s安装完毕！重启终端后，输入 miniagent 即可使用。%s\n' "$green" "$reset"
echo 'Clipboard: also install the Termux:API Android app from the same source as Termux.'

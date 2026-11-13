# 分发与服务器安装

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

分发端和使用端均只要求 **Python 3.10+ 标准安装**。使用端无需 Git、pip、虚拟环境或编译器。发布包附带纯 Python 的 `prompt-toolkit`、`wcwidth` 及其许可证，保留完整终端交互。


## 1. 启动分发服务

把本项目源码放在一台其他服务器可以访问的机器上，在项目根目录运行：

```bash
python3 scripts/distribute.py --build --bind 0.0.0.0 --port 8765
```

第一次构建需要访问 PyPI，下载固定版本的两个纯 Python wheel 并校验 SHA-256；不调用 pip、不安装到系统环境。依赖缓存在项目的 `dist/wheels/`，后续 `--build` 校验本地摘要后直接复用，不再联网。缺失、损坏或不完整的缓存会重新下载；依赖版本变化只下载缺少的新版本。构建时显示下载、缓存命中及完成进度。构建后发布文件位于 `dist/releases/`。服务默认监听 `127.0.0.1`，上面的 `--bind 0.0.0.0` 用于让其他机器访问。

以后启动已有发布包，不需要访问 PyPI：

```bash
python3 scripts/distribute.py --bind 0.0.0.0 --port 8765
```

服务只提供 `/install.sh`、`/install.ps1`、`/install.py`、`/manifest.json` 和当前发布包，根路径显示 Linux、Windows 和 Termux 安装命令。项目文件、密钥、会话和目录列表不会作为下载内容提供。服务是公开下载服务，不带登录；在公网使用时，可放在已有 HTTPS 反向代理后，客户端填写对应 HTTPS 地址。

## 2. 新 Linux 服务器安装

把下面的 `SERVER` 换成分发机器的地址：

```bash
curl -fsSL http://SERVER:8765/install.sh | sh -s -- http://SERVER:8765 --add-to-path
```

也可以使用 wget：

```bash
wget -qO- http://SERVER:8765/install.sh | sh -s -- http://SERVER:8765 --add-to-path
```

安装器使用系统 Python 下载和解包，校验发布包大小与 SHA-256，再创建启动命令。默认路径：

| 内容 | 路径 |
| --- | --- |
| 程序及依赖 | `~/.local/share/miniagent/releases/<版本>-<摘要>/` |
| 启动命令 | `~/.local/bin/miniagent` |
| 项目会话 | 目标项目的 `.miniagent/sessions/` |

`--add-to-path` 向 `~/.profile` 以及当前 Bash/Zsh 的配置文件追加一条 PATH 设置，重复执行不重复添加。新终端会自动生效；当前终端执行：

```bash
export PATH="$HOME/.local/bin:$PATH"
miniagent -C /path/to/project
```

启动后使用 `/settings` 选择 API 来源、填写 GPT 地址或隐藏输入 API key。来源和模型保存于用户配置目录，key 可选择保存或仅本次使用；目录与安装版本分开，升级后仍然有效，详见 [用户配置与密钥](usage.md#用户配置与密钥)。分发包不带用户配置或密钥，安装器不询问或保存密钥。

可指定路径，或在有多个 Python 时指定解释器：

```bash
curl -fsSL http://SERVER:8765/install.sh -o install-miniagent.sh
PYTHON=/usr/bin/python3.11 sh install-miniagent.sh http://SERVER:8765 \
  --prefix "$HOME/apps/miniagent" --bin-dir "$HOME/bin" --add-to-path
```

安装命令会保留实际使用的解释器路径。未传 `--add-to-path` 时不修改 Shell 配置，会打印当前终端的 PATH 命令。Python 低于 3.10 会提示退出，不自动升级系统 Python。

### Android / Termux

同一条安装命令也用于更新，保留用户配置和项目会话；更新后退出旧 MiniAgent 并重新启动。入口开始以黄色提示包升级和下载可能较慢，建议使用加速器或更换软件源；安装完成以绿色提示重启终端并运行 `miniagent`。重定向输出、`TERM=dumb` 或设置 `NO_COLOR` 时使用无色文字。

云服务器更新代码、构建发布包并重启分发服务后，在手机 Termux 输入这一行即可安装或升级（默认分发端口 8765）：

```bash
curl -fsSL http://124.221.221.10:8765/install-termux.sh | sh
```

专用入口使用 apt 更新包索引并完整升级已有 Termux 包，再安装 Python/curl/termux-api 命令包并安装 MiniAgent、配置 PATH。完成后新开一个 Termux 会话，输入 `miniagent` 启动；首次配置模型和密钥。Termux:API Android 应用仍需单独安装，见下面第 1 步。若当前 Termux 没有 curl，先执行 `apt update && apt full-upgrade && apt install curl`。服务器需开放实际使用的分发端口；此入口须部署后才能使用，尚未验证公网连通性。其他地址可传参：`curl -fsSL https://example.com/install-termux.sh | sh -s -- https://example.com`。

若出现 `CANNOT LINK EXECUTABLE "curl": cannot locate symbol "SSL_..."`，说明 libcurl 与实际加载的 OpenSSL 库不匹配，常见于部分升级。先直接用 apt 修复，避免 pkg 的镜像检查调用已失效的 curl：

```bash
apt update
apt full-upgrade
curl --version
```

每步成功后再执行下一步。若完整升级后仍报相同错误，执行 `apt install --reinstall openssl libcurl curl`，再检查 `curl --version`。若 apt 报错，应保留完整输出排查软件源或依赖，不能继续安装。恢复后重新运行安装命令，无需清空 Termux 数据。此流程尚未在 Android 真机验证。

一行安装通过管道运行，包操作不能等待键盘输入。入口先用 `dpkg --force-confold --configure --pending` 恢复未完成的配置，再执行完整升级和依赖安装；这些步骤设置非交互模式，并向 dpkg 传递保留已有配置文件的选项，避免 `openssl.cnf` 冲突导致 `end of file on stdin at conffile prompt`。发生该错误后，只要 curl 能启动，即可重新执行同一行安装命令恢复。若 curl 也无法启动，先手动执行 `dpkg --force-confold --configure --pending`，再执行上面的 apt 恢复步骤。其他包错误会停止安装，需根据输出排查。

Android 适配目标是 Termux 中的 Python 3.10+ 终端环境，无需 root、Ubuntu/proot 或编译器。发布包附带纯 Python 依赖，可复用 Linux 安装器。安装和项目目录放在 Termux 私有 HOME 下；文件创建使用硬链接，不应假定共享存储支持相同的文件系统能力。

1. 按 [Termux 官方安装说明](https://github.com/termux/termux-app#installation) 安装 Termux；需要复制到手机系统剪贴板时，同时安装 [Termux:API Android 应用](https://github.com/termux/termux-api)。两者必须来自同一渠道（例如都来自 F-Droid），避免签名不兼容；终端内的命令包不能代替 Android 应用。
2. 在 Termux 准备 Python、下载工具及剪贴板命令：

```bash
apt update
apt full-upgrade
apt install python curl termux-api
python --version
```

3. 将 `http://SERVER:8765` 换成实际分发地址；若分发服务配置 HTTPS，使用对应的 HTTPS 地址：

```bash
server='http://SERVER:8765'
curl -fsSL "$server/install.sh" -o "$HOME/install-miniagent.sh"
sh "$HOME/install-miniagent.sh" "$server" --add-to-path
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$HOME/projects/demo"
miniagent -C "$HOME/projects/demo"
```

默认安装到 `~/.local/share/miniagent`，启动器在 `~/.local/bin/miniagent`，无需修改 Termux 系统目录。首次启动使用 `/settings` 配置 API 来源、模型和密钥；分发下载地址与模型 API 地址是两个独立配置。项目也可通过可选的 `pkg install git` 后克隆到 `~/projects/`。源码用户在仓库根目录安装依赖后，可用 `python -m miniagent` 启动。

剪贴板可先独立检查：

```bash
printf '%s' 'MiniAgent 中文复制测试' | termux-clipboard-set
```

到手机其他应用粘贴确认内容；MiniAgent 中选中文字后 Ctrl+C 触发复制。手机粘贴到输入框由 Termux 处理。若命令不可用，检查 Android 应用与命令包是否都已安装、来源是否一致；MiniAgent 调用失败或超时会回退 OSC 52，实际效果取决于终端。手机键盘缺少 Ctrl/Esc 时可使用 Termux 的扩展按键；中文输入或显示异常时可先用 `miniagent --plain -C "$HOME/projects/demo"` 排查。

升级时重复第 3 步下载并执行最新安装器，然后退出旧 MiniAgent 再启动。用户配置和项目会话保留；可用 `miniagent -C "$HOME/projects/demo" --resume` 恢复会话。安装时使用的 Python 路径写入启动器，Python 环境移动或重装后应重新安装 MiniAgent。

安装器生成的启动脚本使用安装时 PATH 中的 `sh` 绝对路径，找不到时回退 `/bin/sh`。`install.sh` 应显式通过 `sh` 运行，避免其静态 shebang 对 Android 系统路径的依赖。Windows 安装行为不变。

目前完成路径和剪贴板命令适配与模拟测试，尚未完成 Android 真机验收。手机上需验证：启动及中文输入、文件创建和替换、命令输出、后台任务轮询、超时和取消后的子进程清理、真实 API 连接取消，以及剪贴板复制。Termux:API 配置见[终端交互](terminal.md)。锁屏后持续运行不属于支持要求。

## 3. Windows 一键安装

Windows PowerShell 5.1 或 PowerShell 7 中执行，把 `SERVER` 替换为分发机器地址：

```powershell
$server='http://SERVER:8765'; & ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing "$server/install.ps1").Content)) -Url $server -AddToPath
miniagent -C "C:\path\to\project"
```

需要已安装 Python 3.10+，无需 sh、Git、pip 或管理员权限。入口依次检测 `py -3`、`python`、`python3`；也可用 `$env:PYTHON` 指定解释器完整路径。下载发布包后与 Linux 共用大小、SHA-256 校验及解包流程。

程序及依赖默认位于 `%LOCALAPPDATA%\MiniAgent\releases\<版本>-<摘要>\`，启动命令为 `%LOCALAPPDATA%\MiniAgent\bin\miniagent.cmd`，保留安装时使用的 Python 解释器。`-AddToPath` 将命令目录加入用户 PATH，保留原有内容且重复执行不重复添加，同时更新当前 PowerShell 的 PATH。已有终端窗口仍需重新打开或手动更新 PATH；部分终端宿主需退出并重新启动。启动后用 `/settings` 配置 API，配置与安装版本分开保存。

自定义安装和启动命令目录：

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing "$server/install.ps1").Content)) -Url $server -Prefix "$env:USERPROFILE\apps\MiniAgent" -BinDir "$env:USERPROFILE\bin" -AddToPath
```

不传 `-AddToPath` 时不修改用户 PATH，安装器打印当前 PowerShell 的设置命令。直接运行 `install.py` 也支持 Windows，参数为 `--prefix`、`--bin-dir`、`--add-to-path`；子进程无法修改当前 PowerShell 的环境，需执行其打印的 PATH 命令。

已有分发服务需更新服务器上的 `scripts/` 并重启服务，才能开放新增的 `/install.ps1` 路由；仅重建发布包不会更新运行中的路由代码。

## 4. 发布更新

本次 Termux 支持更新需要同时同步 `miniagent/`、`scripts/` 和文档到云服务器。在服务器项目目录构建发布包后，重启现有分发服务，使首页的 Android 安装指引生效；不要额外启动第二个占用相同端口的服务。若使用本文的 systemd 用户服务，可执行：

```bash
python3 scripts/build_release.py
systemctl --user restart miniagent-distribute.service
systemctl --user status miniagent-distribute.service --no-pager
curl -fsS http://127.0.0.1:8765/
curl -fsS http://127.0.0.1:8765/manifest.json
```

其他托管方式按现有部署重启；监听地址、端口和反向代理保持实际配置。确认首页包含 `Android / Termux`，manifest 能正常返回，再从手机执行安装。构建产物包含此前的 Shell、剪贴板和 Windows API 取消修复；同版本源码变更也会生成不同摘要，安装器据摘要创建新的发布目录。手机真机验收尚未完成，不将桌面测试视为 Android 验收。

在分发机器上更新源码，再构建一次：

```bash
python3 scripts/build_release.py
```

运行中的分发服务会读取新的 manifest，无需重启。客户端重新执行同一条安装命令即可升级；程序放到新的版本目录，最后替换启动命令。下载、校验或解包失败时旧命令保留；不会覆盖同名的非 MiniAgent 命令。旧版本目录保留，已运行的会话不受启动命令切换影响。

构建步骤仅打包源码白名单与两个依赖，版本来自 `miniagent/__init__.py`。`manifest.json` 记录版本、最低 Python 版本、文件名、大小和 SHA-256。发布包不包含本地 `.venv` 或操作系统相关的原生扩展。

## 5. 离线构建或安装

若 `dist/wheels/` 已有完整缓存，可直接按通常方式使用 `--build`，无需指定 `--wheel-dir`。后者用于明确指定外部 wheel 目录，不会联网下载或写入默认缓存。

没有缓存且分发机器无外网时，预先提供下面两份 wheel：

- `prompt_toolkit-3.0.53-py3-none-any.whl`
- `wcwidth-0.9.1-py3-none-any.whl`

```bash
python3 scripts/distribute.py --build --wheel-dir /path/to/wheels --bind 0.0.0.0
```

使用端只需能访问分发服务；使用模型仍需能访问相应 API。完全通过文件传输安装时，可复制生成的 `miniagent-<版本>.tar.gz`，用系统 Python 解包官方生成的包后直接运行：

```bash
python3 -m tarfile -e miniagent-0.3.1.tar.gz "$HOME/apps"
python3 "$HOME/apps/miniagent-0.3.1/agent.py" -C /path/to/project
```

## 6. 作为后台服务

分发程序以前台方式运行，可交给现有进程管理器。Linux 使用 systemd 用户服务时，例如创建 `~/.config/systemd/user/miniagent-distribute.service`：

```ini
[Unit]
Description=MiniAgent distribution

[Service]
WorkingDirectory=%h/Mini-Agent-Harness
ExecStart=/usr/bin/python3 %h/Mini-Agent-Harness/scripts/distribute.py --bind 0.0.0.0 --port 8765
Restart=on-failure

[Install]
WantedBy=default.target
```

先构建发布包，再运行 `systemctl --user daemon-reload` 和 `systemctl --user enable --now miniagent-distribute`。按实际源码位置修改路径；需要退出登录后仍运行时，由服务器管理员为该用户启用 linger。systemd 是可选托管方式，分发程序本身不依赖它。

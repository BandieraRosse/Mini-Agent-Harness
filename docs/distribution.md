# 分发与服务器安装

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

分发端和使用端均只要求 **Python 3.10+ 标准安装**。使用端无需 Git、pip、虚拟环境或编译器。发布包附带纯 Python 的 `prompt-toolkit`、`wcwidth` 及其许可证，保留完整终端交互。


## 1. 启动分发服务

把本项目源码放在一台其他服务器可以访问的机器上，在项目根目录运行：

```bash
python3 scripts/distribute.py --build --bind 0.0.0.0 --port 8765
```

第一次构建需要访问 PyPI，下载固定版本的两个纯 Python wheel 并校验 SHA-256；不调用 pip、不安装到系统环境。构建后文件位于 `dist/releases/`。服务默认监听 `127.0.0.1`，上面的 `--bind 0.0.0.0` 用于让其他机器访问。

以后启动已有发布包，不需要访问 PyPI：

```bash
python3 scripts/distribute.py --bind 0.0.0.0 --port 8765
```

服务只提供 `/install.sh`、`/install.py`、`/manifest.json` 和当前发布包，根路径显示使用说明。项目文件、密钥、会话和目录列表不会作为下载内容提供。服务是公开下载服务，不带登录；在公网使用时，可放在已有 HTTPS 反向代理后，客户端填写对应 HTTPS 地址。

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

启动后使用 `/settings` 选择 API 来源并登录或隐藏输入 API key。来源和模型保存于用户配置目录，key 可选择保存或仅本次使用；目录与安装版本分开，升级后仍然有效，详见 [用户配置与密钥](usage.md#用户配置与密钥)。分发包不带用户配置或密钥，安装器不询问或保存密钥。

可指定路径，或在有多个 Python 时指定解释器：

```bash
curl -fsSL http://SERVER:8765/install.sh -o install-miniagent.sh
PYTHON=/usr/bin/python3.11 sh install-miniagent.sh http://SERVER:8765 \
  --prefix "$HOME/apps/miniagent" --bin-dir "$HOME/bin" --add-to-path
```

安装命令会保留实际使用的解释器路径。未传 `--add-to-path` 时不修改 Shell 配置，会打印当前终端的 PATH 命令。Python 低于 3.10 会提示退出，不自动升级系统 Python。

## 3. 发布更新

在分发机器上更新源码，再构建一次：

```bash
python3 scripts/build_release.py
```

运行中的分发服务会读取新的 manifest，无需重启。客户端重新执行同一条安装命令即可升级；程序放到新的版本目录，最后替换启动命令。下载、校验或解包失败时旧命令保留；不会覆盖同名的非 MiniAgent 命令。旧版本目录保留，已运行的会话不受启动命令切换影响。

构建步骤仅打包源码白名单与两个依赖，版本来自 `miniagent/__init__.py`。`manifest.json` 记录版本、最低 Python 版本、文件名、大小和 SHA-256。发布包不包含本地 `.venv` 或操作系统相关的原生扩展。

## 4. 离线构建或安装

分发机器无外网时，预先提供下面两份 wheel：

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

## 5. 作为后台服务

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

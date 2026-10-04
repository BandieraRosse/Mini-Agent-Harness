# 开发指南

[文档索引](README.md) · [仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

## 本地开发

需要 Python 3.10+。在仓库根目录安装当前源码：

Linux/macOS 下运行 `make` 或 `make run`，会自动准备 `.venv`、安装增强终端依赖并启动当前源码。安装复用分发构建的 `dist/wheels/` 校验缓存，缓存缺失或损坏时从官方 PyPI 下载，再通过 pip 离线安装固定版本；源码启动无需安装项目或下载 setuptools。`make install` 可单独安装或刷新依赖；`PYTHON` 指定用于创建虚拟环境的解释器，`VENV` 指定虚拟环境目录，`ARGS` 传递启动参数。启动命令保留终端输入输出，不默认启用 `--plain`。

```bash
python -m pip install -e .
python -m miniagent --help
```

建议使用虚拟环境。模型和密钥配置见 [使用指南](usage.md)，构建发布包见 [分发与安装](distribution.md)。自动测试使用临时目录和本地 HTTP 服务，不需要真实 API key。

## 修改流程

1. 从 [AGENTS.md](../AGENTS.md) 按任务找到相关专题，再阅读实现与已有测试。
2. 在现有模块责任内完成修改，保持核心循环可追踪。模块分工与行为边界见 [架构](architecture.md)。
3. 行为变化补充或调整相关测试，尤其注意工具调用结果配对、审批、密钥脱敏、编辑冲突和恢复不重放。
4. 更新对应文档，运行与改动相关的检查，交付时说明实际结果及未验证范围。

## 验证

可先运行相关模块，例如修改会话逻辑时：

```bash
python -m unittest discover -s tests -p "test_sessions.py" -v
```

涉及多个模块或准备交付代码改动时，运行完整回归和语法检查：

```bash
python -m unittest discover -s tests -v
python -m compileall -q miniagent agent.py
git diff --check
```

仅修改文档时检查相对链接、命令与当前实现的一致性，以及 `git diff --check`。真实 API 验证按任务需要执行；记录 provider/model、环境、操作和结果，不记录密钥。既有验收证据见 [验收记录](validation.md)，其中的版本结果是历史记录。

## 文档维护

| 修改内容 | 同步更新 |
| --- | --- |
| 启动、配置、交互命令 | [使用指南](usage.md)；快速开始变化时更新 [仓库首页](../README.md) |
| 核心循环、上下文、会话、权限边界 | [架构](architecture.md) |
| 工具 schema、参数、编辑或执行行为 | [工具参考](tools.md) |
| 输入、快捷键、渲染、审批界面 | [终端交互](terminal.md) |
| 构建、安装、发布流程 | [分发与安装](distribution.md) |
| 新的版本验收或真实验证 | [验收记录](validation.md)，写明日期、环境和局限 |
| 文档新增、移除、重命名或职责改变 | [文档索引](README.md)、[AGENTS.md](../AGENTS.md) 和引用该页面的链接 |

详细说明放在对应专题，其他页面通过相对链接引用，避免多处复制容易过期的参数表或验证数字。新专题加入索引，并提供返回索引、仓库首页和 Agent 入口的链接。

`AGENTS.md` 是开发指令，`miniagent/instructions.md` 是内置运行指令，`docs/` 是按需读取的说明；三者的用途不同。文档链接不会自动注入模型上下文，加载行为见 [架构](architecture.md#一轮任务)。

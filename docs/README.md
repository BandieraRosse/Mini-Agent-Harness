# 文档索引

[仓库首页](../README.md) · [Agent 开发入口](../AGENTS.md)

MiniAgent 是面向日常编程的轻量终端工具，重点是可靠的核心能力、高效工具调用和方便使用。本目录按主题解释使用、实现和验证；开发 Agent 从根目录的 `AGENTS.md` 开始，按任务读取相关专题。

## 文档分工

| 文档 | 内容 | 适合何时阅读 |
| --- | --- | --- |
| [仓库首页](../README.md) | 项目概览、快速开始 | 首次了解项目 |
| [Agent 开发入口](../AGENTS.md) | 项目约定、任务到文档和代码的映射 | 开始仓库开发任务 |
| [开发指南](development.md) | 本地开发、验证、文档维护 | 修改代码或文档 |
| [使用指南](usage.md) | 两种 API 来源、URL、API key、命令与会话恢复 | 运行 MiniAgent |
| [架构与开发](architecture.md) | 消息阶段与结束判定、只读并行、256k 预算、增量保存和权限 | 理解内部机制 |
| [工具参考](tools.md) | 工具参数、文件编辑、分页及进程限制 | 使用或修改工具 |
| [终端交互](terminal.md) | 摘要与详情、快捷键、输入及设计参考 | 修改终端体验 |
| [分发与安装](distribution.md) | 构建、服务器分发、安装与升级 | 发布或部署 |
| [验收记录](validation.md) | 已执行的验证、历史结果及未验证范围 | 评估验证证据 |

## 使用与维护路径

1. 从 [使用指南](usage.md) 运行一次任务，观察模型如何读取文件、调用工具并继续回答。
2. 阅读 [架构的一轮任务](architecture.md#一轮任务)，对照 `miniagent/core.py`、`messages.py` 和 `api.py` 追踪消息阶段、请求与结果。
3. 阅读 [工具参考](tools.md)，对照 `miniagent/tools.py` 和 `processes.py` 理解参数校验、审批与真实执行。
4. 阅读 [架构的会话与恢复](architecture.md#会话与恢复) 和压缩说明，对照 `sessions.py`、`budget.py` 与 `tests/test_sessions.py`、`tests/test_turn_lifecycle.py`。
5. 按 [开发指南](development.md) 做一次小改动并验证；需要界面或部署知识时，再读取对应专题。

## 维护原则

根 `AGENTS.md` 保持简短的约定和导航；本索引负责文档地图；专题文档承载详细事实。实现行为以当前代码和测试为依据，历史验收不能替代本次验证。新增、重命名或删除专题时，更新入口及相关链接，具体规则见 [开发指南](development.md#文档维护)。

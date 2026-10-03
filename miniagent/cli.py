"""CLI wiring; credentials live only for the lifetime of this process."""

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .api import APIError, ChatClient
from .config import Config, load_api_key
from .context import fixed_messages
from .core import Agent
from .processes import ProcessManager
from .security import Redactor
from .sessions import Session
from .tools import ToolRegistry
from .ui import HELP, Terminal


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def parser():
    result = argparse.ArgumentParser(description="MiniAgent: lightweight terminal coding assistant")
    result.add_argument("task", nargs="?", help="Run one task and exit")
    result.add_argument("-p", "--prompt", help="Run one task and exit (alternative to positional task)")
    result.add_argument("-C", "--workspace", type=Path, default=Path.cwd())
    result.add_argument("--provider", choices=["deepseek", "openai", "custom"], default="deepseek")
    result.add_argument("--model", help="Override the provider's model")
    result.add_argument("--base-url", help="Chat Completions base URL, e.g. https://api.openai.com/v1")
    result.add_argument("--timeout", type=positive_int, default=120, help="HTTP timeout in seconds")
    result.add_argument("--max-rounds", type=positive_int, default=40)
    result.add_argument("--context-chars", type=positive_int, default=60_000, help="Approximate context size in characters")
    result.add_argument("--trust", action="store_true", help="Approve commands and edits for this invocation")
    result.add_argument("--no-save", action="store_true", help="Keep this session only in memory")
    result.add_argument("--resume", nargs="?", const="latest", metavar="ID")
    result.add_argument("--list-sessions", action="store_true")
    result.add_argument("--plain", action="store_true", help="Disable colors and enhanced input")
    result.add_argument("--version", action="version", version=f"MiniAgent {__version__}")
    return result


def main(argv=None):
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    argparser = parser()
    args = argparser.parse_args(argv)
    if args.task and args.prompt:
        argparser.error("use either a positional task or --prompt")
    if args.context_chars < 16_000:
        argparser.error("--context-chars must be at least 16000")
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        argparser.error("workspace must be an existing directory")
    processes = None
    ui = Terminal(Redactor(), approval="trust" if args.trust else "ask", plain=args.plain)
    try:
        config = Config(provider=args.provider, model=args.model, base_url=args.base_url, timeout=args.timeout)
        session = Session(workspace, config.provider, config.model, save=not args.no_save)
        if args.list_sessions:
            show_sessions(ui, session)
            return 0
        ui.print(f"MiniAgent {__version__} · {config.provider}/{config.model}", "36")
        ui.notice(f"目录: {workspace} | 权限: {ui.approval} | 保存: {'关闭' if args.no_save else '开启'}")
        ui.notice("/help 查看命令；Ctrl+C 中断当前工作。")
        fixed = fixed_messages(workspace)
        key = load_api_key(config, workspace)
        redact = Redactor(key)
        ui.redact, session.redact = redact, redact
        client = ChatClient(config, key)
        processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
        registry = ToolRegistry(workspace, processes, ui.approve, redact)
        if args.resume:
            session.load(args.resume)
            ui.notice(f"已恢复 {session.data['id']}；后台任务不跨进程恢复。")
        agent = Agent(client, session, registry, redact.value(fixed), ui, args.max_rounds, args.context_chars)
        task = args.prompt or args.task
        if task:
            return 0 if agent.run(task) else 1
        while True:
            try:
                prompt = ui.read()
                if not prompt:
                    continue
                if prompt.startswith("/"):
                    command, _, argument = prompt.partition(" ")
                    argument = argument.strip()
                    if command in {"/exit", "/quit"}:
                        session.save()
                        break
                    if command == "/help":
                        ui.print(HELP)
                    elif command in {"/new", "/clear"}:
                        session.save()
                        processes.close()
                        processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
                        registry = ToolRegistry(workspace, processes, ui.approve, redact)
                        session = Session(workspace, config.provider, config.model, save=not args.no_save, redact=redact)
                        agent.session, agent.tools = session, registry
                        ui.notice("已新建会话，旧会话可通过 /resume 恢复。" if session.enabled else "已新建内存会话。")
                    elif command == "/sessions" or command == "/resume" and not argument:
                        show_sessions(ui, session)
                    elif command == "/resume":
                        session.save()
                        candidate = Session(workspace, config.provider, config.model, save=not args.no_save, redact=redact)
                        candidate.load(argument)
                        processes.close()
                        processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
                        registry = ToolRegistry(workspace, processes, ui.approve, redact)
                        session = candidate
                        agent.session, agent.tools = session, registry
                        ui.notice(f"已恢复 {session.data['id']}；等待新指令，不自动执行历史操作。")
                    elif command == "/save":
                        session.save()
                        ui.notice(f"已保存 {session.path}" if session.enabled else "当前为 --no-save 模式，会话仅保存在内存。")
                    elif command == "/compact":
                        if not agent.compact(force=True):
                            ui.notice("当前没有需要压缩的较早上下文。")
                    elif command == "/approval" and argument in {"ask", "trust"}:
                        ui.approval = argument
                        ui.notice(f"权限模式: {argument}")
                    elif command == "/status":
                        ui.print(f"目录: {workspace}\n模型: {config.provider}/{config.model}\n权限: {ui.approval}\n"
                                 f"会话: {session.data['id']}\n状态: {session.data['status']}\n"
                                 f"消息数: {len(session.messages)}\n保存: {session.enabled}")
                    elif command == "/paste" and ui.editor:
                        ui.notice("增强输入支持 Alt+Enter/Ctrl+J 换行，也可直接粘贴多行。")
                    else:
                        ui.error("未知命令或参数；输入 /help 查看帮助。")
                    continue
                agent.run(prompt)
            except KeyboardInterrupt:
                ui.end_stream()
                processes.close()
                processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
                registry = ToolRegistry(workspace, processes, ui.approve, redact)
                agent.tools = registry
                ui.notice("当前工作已中断；可以继续输入。后台任务已停止。")
            except EOFError:
                session.save()
                ui.print()
                break
            except (APIError, ValueError, OSError) as error:
                ui.error(str(error))
        return 0
    except KeyboardInterrupt:
        ui.end_stream()
        ui.notice("已中断。")
        return 130
    except (APIError, ValueError, OSError, EOFError) as error:
        ui.error(str(error))
        return 1
    finally:
        if processes is not None:
            processes.close()


def show_sessions(ui, session):
    items = session.list_saved()
    if not items:
        ui.notice("当前项目没有保存的会话。")
    for item in items[:30]:
        ui.print(f"{item['id']}  {item['title']}")

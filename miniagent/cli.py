"""CLI wiring and interactive connection settings."""

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .api import APIError, ChatClient
from .config import GPT_MODELS, Preferences, load_api_key, key_path
from .settings import select_source, credentials, DisconnectedClient
from .context import fixed_messages
from .core import Agent
from .budget import CONTEXT_TOKENS
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
    result.add_argument("--provider", choices=["deepseek", "openai"])
    result.add_argument("--model", help="Override the provider's model")
    result.add_argument("--base-url", help="Chat Completions base URL (GPT default: https://124.221.221.10:8443/v1)")
    result.add_argument("--timeout", type=positive_int, default=120, help="HTTP timeout in seconds")
    result.add_argument("--max-rounds", type=positive_int, default=40)
    result.add_argument("--context-tokens", type=positive_int, default=CONTEXT_TOKENS, help="Context window in estimated tokens, default 256000")
    result.add_argument("--context-chars", type=positive_int, default=None, help="Legacy character-budget override; prefer --context-tokens")
    permissions = result.add_mutually_exclusive_group()
    permissions.add_argument("--trust", action="store_true", help="Approve commands and edits for this invocation")
    permissions.add_argument("--ask", action="store_true", help="Ask before each command and file edit (default: trust)")
    permissions.add_argument("--read-only", action="store_true", help="Allow inspection tools only; disable edits and shell commands")
    result.add_argument("--no-save", action="store_true", help="Keep this session only in memory")
    result.add_argument("--resume", nargs="?", const="latest", metavar="ID")
    result.add_argument("--list-sessions", action="store_true")
    result.add_argument("--plain", action="store_true", help="Disable colors and enhanced input")
    result.add_argument("--verbose", action="store_true", help="Show complete tool arguments and results")
    result.add_argument("--version", action="version", version=f"MiniAgent {__version__}")
    return result


def main(argv=None):
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"login", "logout", "login-status"}:
        parser().error("ChatGPT login has been removed; use --provider openai --base-url URL and configure an API key")
    argparser = parser()
    args = argparser.parse_args(arguments)
    if args.task and args.prompt:
        argparser.error("use either a positional task or --prompt")
    if args.context_chars is not None and args.context_chars < 16_000:
        argparser.error("--context-chars must be at least 16000")
    if args.context_tokens < 16_384:
        argparser.error("--context-tokens must be at least 16384")
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        argparser.error("workspace must be an existing directory")
    processes = None
    ui = Terminal(Redactor(), approval="read-only" if args.read_only else "ask" if args.ask else "trust", plain=args.plain)
    ui.detailed = args.verbose
    try:
        preferences = Preferences()
        config = preferences.config(provider=args.provider, model=args.model, base_url=args.base_url, timeout=args.timeout)
        session = Session(workspace, config.provider, config.model, save=not args.no_save)
        if args.list_sessions:
            show_sessions(ui, session)
            return 0
        ui.print(f"MiniAgent {__version__} · {config.provider}/{config.model}", "36")
        ui.notice(f"目录: {workspace} | 权限: {ui.approval} | 保存: {'关闭' if args.no_save else '开启'}")
        ui.notice("/settings 配置 URL 和 API key · /model 切换模型 · / 查看命令")
        selected = None
        interactive = not (args.task or args.prompt)
        if interactive and ui.tty and args.provider is None and args.model is None and args.base_url is None:
            selected = select_source(ui, preferences, config)
            if selected:
                preferences.save(selected)
                config = selected
                session.data.update(provider=config.provider, model=config.model)
        fixed = fixed_messages(workspace)
        redact = Redactor()
        key = None
        connected = False
        if not (interactive and ui.tty):
            key = load_api_key(config, workspace,
                               prompt=(lambda label: ui.ask(label, secret=True)) if ui.editor else None)
            connected = True
        redact.add(key)
        ui.redact, session.redact = redact, redact
        def make_client(settings):
            if key is None:
                return DisconnectedClient(settings)
            return ChatClient(settings, key)
        client = make_client(config)
        ui.model = config.model
        known_models = default_models(config)
        if ui.completer is not None:
            ui.completer.choices["model"] = lambda: known_models
            ui.completer.choices["resume"] = lambda: ["latest", *[item["id"] for item in session.list_saved()[:30]]]
        processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
        registry = ToolRegistry(workspace, processes, ui.approve, redact, lambda: ui.approval)
        if args.resume:
            session.load(args.resume)
            session.data.update(provider=config.provider, model=config.model)
            session.save()
            ui.restore(session.messages)
            ui.notice(f"已恢复 {session.data['id']}；后台任务不跨进程恢复。")
        agent = Agent(client, session, registry, redact.value(fixed), ui, args.max_rounds, args.context_chars,
                      args.context_tokens)
        ui.context_provider = agent.context_usage

        def connect(edit=False):
            nonlocal key, client, connected
            if connected and not edit:
                return
            new_key = credentials(ui, config, workspace, redact, edit=edit)
            key = new_key
            client = make_client(config)
            agent.client = client
            agent.fixed = redact.value(fixed)
            connected = True

        if selected:
            try:
                connect()
                if config.provider != "openai":
                    try:
                        available = ui.run_action(client.list_models, client=client)
                        known_models[:] = list(dict.fromkeys([config.model, *available]))
                    except (APIError, ValueError, OSError) as error:
                        ui.notice(f"未能获取模型列表：{error}；可手动输入模型名称。")
                model = choose_model(ui, config, known_models)
                if model:
                    config = replace(config, model=model)
                    preferences.save(config)
                    client = make_client(config)
                    agent.client = client
                    ui.model = config.model
                    session.data.update(provider=config.provider, model=config.model)
            except (APIError, ValueError, OSError, EOFError) as error:
                ui.error(str(error))

        task = args.prompt or args.task
        if task:
            return 0 if ui.run_action(lambda: agent.run(task), client=client, processes=processes) else 1
        while True:
            try:
                prompt = ui.read()
                if not prompt:
                    continue
                if prompt.startswith("/"):
                    parts = prompt.split(maxsplit=1)
                    command, argument = parts[0], parts[1].strip() if len(parts) == 2 else ""
                    if command in {"/exit", "/quit"}:
                        session.save()
                        break
                    if command == "/help":
                        ui.print(HELP)
                    elif command in {"/new", "/clear"}:
                        session.save()
                        processes.close()
                        processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
                        registry = ToolRegistry(workspace, processes, ui.approve, redact, lambda: ui.approval)
                        ui.approval_rules.clear()
                        session = Session(workspace, config.provider, config.model, save=not args.no_save, redact=redact)
                        agent.session, agent.tools = session, registry
                        ui.clear_history(clear_screen=command == "/clear")
                        ui.notice("已新建会话，旧会话可通过 /resume 恢复。" if session.enabled else "已新建内存会话。")
                    elif command == "/sessions":
                        show_sessions(ui, session)
                    elif command == "/resume":
                        if not argument:
                            choices = [(item["id"], f"{item['id']}  {item['title']}") for item in session.list_saved()[:30]]
                            if not choices:
                                ui.notice("当前项目没有保存的会话。")
                                continue
                            argument = ui.choose("恢复会话", choices)
                            if not argument:
                                continue
                        # Resolve 'latest' before saving the current conversation.
                        candidate = Session(workspace, config.provider, config.model, save=False, redact=redact)
                        candidate.load(argument)
                        session.save()
                        candidate.enabled = not args.no_save
                        candidate.data.update(provider=config.provider, model=config.model)
                        candidate.save()
                        processes.close()
                        processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
                        registry = ToolRegistry(workspace, processes, ui.approve, redact, lambda: ui.approval)
                        ui.approval_rules.clear()
                        session = candidate
                        agent.session, agent.tools = session, registry
                        ui.restore(session.messages)
                        ui.notice(f"已恢复 {session.data['id']}；等待新指令，不自动执行历史操作。")
                    elif command == "/save":
                        session.save()
                        ui.notice(f"已保存 {session.path}" if session.enabled else "当前为 --no-save 模式，会话仅保存在内存。")
                    elif command == "/compact":
                        connect()
                        if not ui.run_action(lambda: agent.compact(force=True), client=client, processes=processes):
                            ui.notice("当前没有需要压缩的较早上下文。")
                    elif command in {"/permissions", "/approval"}:
                        if not argument:
                            argument = ui.choose(f"权限 · 当前 {ui.approval}", [
                                ("ask", "ask — 每次文件修改和命令执行前确认"),
                                ("trust", "trust — 信任当前项目会话（Shell 使用当前用户权限）"),
                                ("read-only", "read-only — 仅允许读取，不执行命令或修改文件"),
                            ])
                            if not argument:
                                continue
                        if argument == 'rules':
                            ui.print('\n\n'.join(detail for _, detail in sorted(ui.approval_rules)) or '没有记住的命令。')
                            continue
                        if argument == 'reset':
                            ui.approval_rules.clear()
                            ui.notice('已清除本会话记住的命令。')
                            continue
                        if argument not in {"ask", "trust", "read-only"}:
                            raise ValueError("用法：/permissions ask|trust|read-only|rules|reset")
                        ui.approval = argument
                        ui.notice(f"权限模式: {argument}")
                    elif command in {"/settings", "/provider"}:
                        action = "provider" if command == "/provider" else ui.choose(
                            f"设置 · {config.provider}/{config.model}", [
                                ("provider", "切换 API 来源 / 编辑 GPT 地址"),
                                ("model", "切换模型"), ("credentials", "设置 API key（临时使用 / 长期保存）"),
                                ("location", "查看配置和密钥文件位置"),
                            ])
                        if action == "model":
                            command, argument = "/model", ""
                        elif action == "provider":
                            updated = select_source(ui, preferences, config, argument if command == "/provider" else "")
                            if updated:
                                preferences.save(updated)
                                session.save()
                                processes.close()
                                processes = ProcessManager(workspace, ui.approve, redact)
                                registry = ToolRegistry(workspace, processes, ui.approve, redact, lambda: ui.approval)
                                ui.approval_rules.clear()
                                config, key, connected = updated, None, False
                                client = make_client(config)
                                session = Session(workspace, config.provider, config.model, save=not args.no_save, redact=redact)
                                agent.client, agent.session, agent.tools = client, session, registry
                                registry.bind_session(session)
                                ui.model = config.model
                                known_models[:] = default_models(config)
                                ui.clear_history()
                                ui.notice(f"已切换来源: {config.provider}/{config.model}；已新建会话，下次请求时验证凭据。")
                        elif action == "credentials":
                            connect(edit=True)
                            preferences.save(config)
                            ui.notice("凭据已就绪。")
                        elif action == "location":
                            ui.print(f"用户配置: {preferences.path}\n当前 API key: {key_path(config)}")
                        if command != "/model":
                            continue
                    if command == "/model":
                        if not argument:
                            if config.provider == "openai":
                                known_models[:] = list(dict.fromkeys([*default_models(config), *known_models]))
                            else:
                                try:
                                    connect()
                                    available = ui.run_action(client.list_models, client=client)
                                    known_models[:] = list(dict.fromkeys([config.model, *available]))
                                except (APIError, ValueError, OSError) as error:
                                    ui.notice(f"未能获取模型列表：{error}；仍可输入 /model 模型名称。")
                            argument = choose_model(ui, config, known_models)
                            if not argument:
                                continue
                        updated_config = replace(config, model=argument)
                        updated_client = make_client(updated_config)
                        preferences.save(updated_config)
                        config, client = updated_config, updated_client
                        agent.client = client
                        ui.model = config.model
                        if config.model not in known_models:
                            known_models.append(config.model)
                        session.data.update(provider=config.provider, model=config.model)
                        session.save()
                        ui.notice(f"已切换模型: {config.provider}/{config.model}；对话上下文保留。")
                    elif command == "/status":
                        ui.print(f"目录: {workspace}\n模型: {config.provider}/{config.model}\n权限: {ui.approval}\n"
                                 f"会话: {session.data['id']}\n状态: {session.data['status']}\n"
                                 f"消息数: {len(session.messages)}\n保存: {session.enabled}")
                        ui.print(f"上下文窗口: {args.context_tokens} estimated tokens | 输入预算: {agent.input_budget}" +
                                 (' chars (legacy override)' if args.context_chars is not None else ' estimated tokens'))
                        ui.print(f"本次运行 tokens: {ui.tokens['prompt']} in | {ui.tokens['completion']} out")
                        ui.print(ui.statistics())
                    elif command == "/paste" and ui.editor:
                        ui.notice("增强输入支持 Alt+Enter/Ctrl+J 换行，也可直接粘贴多行。")
                    elif command not in {"/help", "/new", "/clear", "/sessions", "/resume", "/save", "/compact", "/permissions", "/approval"}:
                        ui.error("未知命令或参数；输入 /help 查看帮助。")
                    continue
                connect()
                ui.run_action(lambda: agent.run(prompt), client=client, processes=processes)
            except KeyboardInterrupt:
                ui.end_stream()
                processes.close()
                processes = ProcessManager(workspace, ui.approve, redact, secrets=(key,))
                registry = ToolRegistry(workspace, processes, ui.approve, redact, lambda: ui.approval)
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
        try:
            if processes is not None:
                processes.close()
        finally:
            ui.close()


def show_sessions(ui, session):
    items = session.list_saved()
    if not items:
        ui.notice("当前项目没有保存的会话。")
    for item in items[:30]:
        ui.print(f"{item['id']}  {item['title']}")


def default_models(config):
    return list(dict.fromkeys([config.model, *(GPT_MODELS if config.provider == "openai" else ())]))


def choose_model(ui, config, known_models):
    manual = "__miniagent_manual_model__"
    options = [(name, name + ("（当前）" if name == config.model else "")) for name in known_models]
    options.append((manual, "输入其他模型名称…"))
    choice = ui.choose(f"模型 · {config.provider}", options)
    return ui.ask("模型名称") if choice == manual else choice

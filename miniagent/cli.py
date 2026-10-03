"""CLI wiring and interactive connection settings."""

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .api import APIError, ChatClient
from .config import Config, Preferences, load_api_key, user_directory, key_path
from .settings import select_source, credentials, DisconnectedClient
from .chatgpt_auth import ChatGPTAuth
from .responses import ResponsesClient
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
    result = argparse.ArgumentParser(description="MiniAgent: lightweight terminal coding assistant",
                                     epilog="ChatGPT authentication: miniagent login | logout | login-status [--account LABEL]")
    result.add_argument("task", nargs="?", help="Run one task and exit")
    result.add_argument("-p", "--prompt", help="Run one task and exit (alternative to positional task)")
    result.add_argument("-C", "--workspace", type=Path, default=Path.cwd())
    result.add_argument("--provider", choices=["deepseek", "openai", "chatgpt", "custom"])
    result.add_argument("--account", help="Override the saved MiniAgent ChatGPT account label")
    result.add_argument("--model", help="Override the provider's model")
    result.add_argument("--base-url", help="Chat Completions base URL, e.g. https://api.openai.com/v1")
    result.add_argument("--timeout", type=positive_int, default=120, help="HTTP timeout in seconds")
    result.add_argument("--max-rounds", type=positive_int, default=40)
    result.add_argument("--context-tokens", type=positive_int, default=CONTEXT_TOKENS, help="Context window in estimated tokens, default 256000")
    result.add_argument("--context-chars", type=positive_int, default=None, help="Legacy character-budget override; prefer --context-tokens")
    permissions = result.add_mutually_exclusive_group()
    permissions.add_argument("--trust", action="store_true", help="Approve commands and edits for this invocation")
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
        return auth_main(arguments)
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
    ui = Terminal(Redactor(), approval="read-only" if args.read_only else "trust" if args.trust else "ask", plain=args.plain)
    ui.detailed = args.verbose
    try:
        preferences = Preferences()
        account = args.account or preferences.data.get("account", "default")
        config = preferences.config(provider=args.provider, model=args.model, base_url=args.base_url, timeout=args.timeout)
        session = Session(workspace, config.provider, config.model, save=not args.no_save)
        if args.list_sessions:
            show_sessions(ui, session)
            return 0
        ui.print(f"MiniAgent {__version__} · {config.provider}/{config.model}", "36")
        ui.notice(f"目录: {workspace} | 权限: {ui.approval} | 保存: {'关闭' if args.no_save else '开启'}")
        ui.notice("/settings 配置来源和登录 · /model 切换模型 · / 查看命令")
        interactive = not (args.task or args.prompt)
        if interactive and ui.tty and not preferences.path.exists() and args.provider is None:
            selected = select_source(ui, preferences, config)
            if selected:
                preferences.save(selected, account)
                config = selected
                session.data.update(provider=config.provider, model=config.model)
        fixed = fixed_messages(workspace)
        redact = Redactor()
        auth = None
        key = None
        connected = False
        if not (interactive and ui.tty):
            auth = ChatGPTAuth(account=account, redact=redact) if config.provider == "chatgpt" else None
            key = None if auth else load_api_key(config, workspace)
            if auth:
                auth.access_token()
            connected = True
        redact.add(key)
        ui.redact, session.redact = redact, redact
        def make_client(settings):
            if key is None and auth is None:
                return DisconnectedClient(settings)
            return ResponsesClient(settings, auth) if settings.provider == "chatgpt" else ChatClient(settings, key)
        client = make_client(config)
        if auth:
            ui.notice(f"ChatGPT account: {account} (MiniAgent independent login)")
        ui.model = config.model
        known_models = [config.model]
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

        def connect(edit=False):
            nonlocal key, auth, client, connected
            if connected and not edit:
                return
            new_key, new_auth = credentials(ui, config, workspace, account, redact, edit=edit)
            key, auth = new_key, new_auth
            client = make_client(config)
            agent.client = client
            agent.fixed = redact.value(fixed)
            connected = True

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
                                ("provider", "切换 API 来源 / 编辑自定义地址"),
                                ("model", "切换模型"), ("credentials", "登录 ChatGPT / 设置 API key"),
                                ("account", "切换 ChatGPT 账号标签"), ("logout", "退出当前 ChatGPT 账号"),
                                ("location", "查看配置和密钥文件位置"),
                            ])
                        if action == "model":
                            command, argument = "/model", ""
                        elif action == "provider":
                            updated = select_source(ui, preferences, config, argument if command == "/provider" else "")
                            if updated:
                                preferences.save(updated, account)
                                session.save()
                                processes.close()
                                processes = ProcessManager(workspace, ui.approve, redact)
                                registry = ToolRegistry(workspace, processes, ui.approve, redact, lambda: ui.approval)
                                ui.approval_rules.clear()
                                config, key, auth, connected = updated, None, None, False
                                client = make_client(config)
                                session = Session(workspace, config.provider, config.model, save=not args.no_save, redact=redact)
                                agent.client, agent.session, agent.tools = client, session, registry
                                registry.bind_session(session)
                                ui.model = config.model
                                known_models[:] = [config.model]
                                ui.clear_history()
                                ui.notice(f"已切换来源: {config.provider}/{config.model}；已新建会话，下次请求时验证凭据。")
                        elif action == "credentials":
                            connect(edit=True)
                            preferences.save(config, account)
                            ui.notice("凭据已就绪。")
                        elif action == "account":
                            label = ui.ask("ChatGPT 账号标签（留空取消）")
                            if label:
                                ChatGPTAuth(account=label, redact=redact)
                                preferences.save(config, label)
                                account = label
                                if config.provider == "chatgpt":
                                    key, auth, connected = None, None, False
                                ui.notice(f"ChatGPT 账号标签: {account}")
                        elif action == "logout":
                            revoked = ChatGPTAuth(account=account, redact=redact).logout()
                            if config.provider == "chatgpt":
                                auth, connected = None, False
                            ui.notice("已退出本地 ChatGPT 登录。" + ("" if revoked else " 请在 ChatGPT 设置中确认断开 MiniAgent。"))
                        elif action == "location":
                            ui.print(f"用户配置: {preferences.path}\nChatGPT 凭据: {user_directory() / 'chatgpt-auth.dat'}")
                            if config.provider != "chatgpt":
                                ui.print(f"当前 API key: {key_path(config)}")
                        if command != "/model":
                            continue
                    if command == "/model":
                        if not argument:
                            try:
                                connect()
                                available = ui.run_action(client.list_models, client=client)
                                known_models[:] = list(dict.fromkeys([config.model, *available]))
                            except (APIError, ValueError, OSError) as error:
                                ui.notice(f"未能获取模型列表：{error}；仍可输入 /model 模型名称。")
                            custom = "__miniagent_manual_model__"
                            options = [(name, name + ("（当前）" if name == config.model else "")) for name in known_models]
                            options.append((custom, "输入其他模型名称…"))
                            argument = ui.choose(f"模型 · {config.provider}", options)
                            if argument == custom:
                                argument = ui.ask("模型名称")
                            if not argument:
                                continue
                        updated_config = replace(config, model=argument)
                        updated_client = make_client(updated_config)
                        preferences.save(updated_config, account)
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
        if processes is not None:
            processes.close()


def show_sessions(ui, session):
    items = session.list_saved()
    if not items:
        ui.notice("当前项目没有保存的会话。")
    for item in items[:30]:
        ui.print(f"{item['id']}  {item['title']}")


def auth_main(arguments):
    command = arguments[0]
    cli = argparse.ArgumentParser(prog=f"miniagent {command}", description="Manage MiniAgent's independent ChatGPT login")
    cli.add_argument("--provider", choices=["chatgpt"], default="chatgpt")
    cli.add_argument("--account", help="Separate account label; defaults to the saved label")
    args = cli.parse_args(arguments[1:])
    try:
        args.account = args.account or Preferences().data.get("account", "default")
        auth = ChatGPTAuth(account=args.account)
        if command == "login":
            auth.login()
            print(f"ChatGPT account '{args.account}' signed in. Model access is checked on the first request.")
        elif command == "login-status":
            print(auth.status())
        else:
            revoked = auth.logout()
            print(f"ChatGPT account '{args.account}' signed out locally.")
            if not revoked:
                print("Remote revocation was not confirmed. Disconnect MiniAgent in ChatGPT Settings.")
        return 0
    except KeyboardInterrupt:
        print("ChatGPT login operation interrupted.", file=sys.stderr)
        return 130
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

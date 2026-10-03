"""Interactive connection setup; no model prompts or conversation history involved."""

from .config import Config, PROVIDERS, load_api_key, save_api_key, key_path
from .chatgpt_auth import ChatGPTAuth


class DisconnectedClient:
    def __init__(self, config):
        self.config = config

    def complete(self, *args, **kwargs):
        raise ValueError("Configure credentials in /settings before sending a request")

    def list_models(self):
        raise ValueError("Configure credentials in /settings before listing models")


def select_source(ui, preferences, current, argument=""):
    provider = argument or ui.choose("API 来源", [
        ("deepseek", "DeepSeek API"), ("openai", "OpenAI API（API key）"),
        ("chatgpt", "ChatGPT 订阅（独立登录）"), ("custom", "自定义兼容 API / 中转"),
    ])
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider; use deepseek, openai, chatgpt, or custom")
    if provider == "custom":
        profile = preferences.data.get("profiles", {}).get(provider, {})
        url = ui.ask("API 地址（留空使用已保存地址）") or profile.get("base_url")
        if not url:
            return None
        model = ui.ask("模型名称（留空使用已保存模型）") or profile.get("model")
        if not model:
            return None
        return Config(provider=provider, base_url=url, model=model, timeout=current.timeout)
    return preferences.config(provider=provider, timeout=current.timeout)


def credentials(ui, config, workspace, account, redact, *, edit=False):
    if config.provider == "chatgpt":
        auth = ChatGPTAuth(account=account, redact=redact)
        if edit or f"{account}: signed in" not in auth.status().splitlines():
            auth.login(notify=ui.notice)
        auth.access_token()
        return None, auth
    if edit:
        # getpass is deliberately separate from prompt history and visible input.
        import getpass
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                key = getpass.getpass("API key (hidden): ")
            except getpass.GetPassWarning:
                raise ValueError("Hidden key input requires an interactive terminal") from None
        from .config import _clean_key
        key = _clean_key(key)
    else:
        key = load_api_key(config, workspace)
    redact.add(key)
    if edit or not key_path(config).exists():
        choice = ui.choose("保存 API key", [("save", "保存到用户目录，下次自动使用"),
                                               ("memory", "仅本次运行使用")])
        if choice == "save":
            save_api_key(config, key)
    return key, None

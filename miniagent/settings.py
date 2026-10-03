"""Interactive connection setup; no model prompts or conversation history involved."""

from .config import Config, PROVIDERS, load_api_key, save_api_key, key_path


class DisconnectedClient:
    def __init__(self, config):
        self.config = config

    def complete(self, *args, **kwargs):
        raise ValueError("Configure credentials in /settings before sending a request")

    def list_models(self):
        raise ValueError("Configure credentials in /settings before listing models")


def select_source(ui, preferences, current, argument=""):
    provider = argument or ui.choose("选择模型来源", [
        ("deepseek", "DeepSeek API"), ("openai", "GPT API（指定 URL + API key）"),
    ])
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider; use deepseek or openai")
    saved = preferences.config(provider=provider, timeout=current.timeout)
    if provider == "openai" and ui.tty:
        url = ui.ask(f"GPT API 地址（留空使用 {saved.base_url}）") or saved.base_url
        return Config(provider=provider, base_url=url, model=saved.model, timeout=current.timeout)
    return saved


def credentials(ui, config, workspace, redact, *, edit=False):
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
    return key

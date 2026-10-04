"""Small, explicit configuration. Credentials never become configuration fields."""

from __future__ import annotations

import getpass
import ipaddress
import math
import os
import json
import hashlib
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PROVIDERS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-flash", ".deepseek_api_key"),
    "openai": ("https://api.openai.com/v1", "gpt-6-astra", ".openai_api_key"),
}
INSTALL_ROOT = Path(__file__).resolve().parent.parent

# Local GPT menu; gateways may use custom names via manual model entry.
GPT_MODELS = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol")


def user_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "MiniAgent"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "miniagent"


def write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing to write configuration through a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".miniagent-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Preferences:
    """Non-secret connection preferences, independent of install and workspace."""

    def __init__(self):
        self.path = user_directory() / "config.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(self.data, dict) or not isinstance(self.data.get("provider", "deepseek"), str)
                    or self.data.get("provider", "deepseek") not in {*PROVIDERS, "custom", "chatgpt"}
                    or not isinstance(self.data.get("profiles", {}), dict)):
                raise ValueError()
            # Upgrade old API profiles; subscription credentials are never reused.
            profiles = self.data.get("profiles", {})
            if "custom" in profiles:
                custom = profiles.pop("custom")
                if self.data.get("provider") == "custom" or "openai" not in profiles:
                    profiles["openai"] = custom
            profiles.pop("chatgpt", None)
            if self.data.get("provider") == "custom":
                self.data["provider"] = "openai"
            elif self.data.get("provider") == "chatgpt":
                self.data["provider"] = "deepseek"
            for provider, profile in profiles.items():
                if provider not in PROVIDERS or not isinstance(profile, dict):
                    raise ValueError()
                Config(provider=provider, model=profile.get("model"), base_url=profile.get("base_url"))
        except FileNotFoundError:
            self.data = {}
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid user config.json; repair or rename it before starting MiniAgent") from None

    def config(self, provider=None, model=None, base_url=None, timeout=120):
        provider = provider or self.data.get("provider", "deepseek")
        profile = self.data.get("profiles", {}).get(provider, {})
        return Config(provider=provider, model=model or profile.get("model"),
                      base_url=base_url or profile.get("base_url"), timeout=timeout)

    def save(self, config):
        profiles = dict(self.data.get("profiles", {}))
        profiles[config.provider] = {"model": config.model, "base_url": config.base_url}
        data = {"version": 1, "provider": config.provider, "profiles": profiles}
        write_private(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        self.data = data


def key_path(config: Config) -> Path:
    # Bind credentials to the exact endpoint; editing a URL never forwards a saved key.
    suffix = hashlib.sha256(config.endpoint.encode("utf-8")).hexdigest()[:24]
    return user_directory() / "keys" / f"{config.provider}-{suffix}.key"


def save_api_key(config: Config, key: str) -> None:
    # mkdir(parents=True) applies mode only to the last directory, not its parents.
    user_directory().mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(key_path(config), _clean_key(key) + "\n")


def _validate_base_url(value: str) -> str:
    # Do not echo invalid URLs: they can contain a pasted credential.
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("base_url must be an absolute HTTP(S) URL without whitespace")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("Invalid base_url") from None
    if (not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in value or "#" in value):
        raise ValueError("base_url must not contain credentials, a query, or a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid base_url port")
    local = host.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("base_url requires HTTPS; HTTP is allowed only on loopback")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    provider: str = "deepseek"
    model: str | None = None
    base_url: str | None = None
    timeout: float = 120.0
    max_retries: int = 2
    max_tokens: int = 8192

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError("provider must be deepseek or openai")
        default_url, default_model, _ = PROVIDERS[self.provider]
        model = self.model if self.model is not None else default_model
        base_url = self.base_url if self.base_url is not None else default_url
        if not isinstance(model, str) or not model.strip() or any(c.isspace() for c in model):
            raise ValueError("A model name is required")
        if base_url is None:
            raise ValueError("An API base URL is required")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ValueError("timeout must be a positive finite number")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries must be an integer between 0 and 5")
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "base_url", _validate_base_url(base_url))

    @property
    def endpoint(self) -> str:
        base = str(self.base_url)
        if self.provider == "openai" and not urlsplit(base).path:
            base += "/v1"
        return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _clean_key(value: str) -> str:
    key = value.strip()
    if not key or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("API key must be a nonempty single line of printable ASCII")
    return key


def load_api_key(config: Config, workspace: Path, *, prompt=None) -> str:
    """User keys first; import legacy default-provider files once, never overwrite."""
    filename = PROVIDERS[config.provider][2]
    target = key_path(config)
    paths = [target]
    if config.provider == "openai":
        # Old custom keys remain bound to precisely the same endpoint.
        paths.append(target.with_name(target.name.replace("openai-", "custom-", 1)))
    if config.base_url == PROVIDERS[config.provider][0]:
        paths.extend(directory / filename for directory in
                     dict.fromkeys((Path(workspace).resolve(), INSTALL_ROOT.resolve())))
    for path in paths:
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                value = handle.read(8193)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            raise ValueError(f"Unable to read {filename}; check the key file and permissions") from None
        if len(value) > 8192:
            raise ValueError(f"{filename} is too large to be an API key")
        key = _clean_key(value)
        if path != target:
            save_api_key(config, key)
        return key
    try:
        if prompt is not None:
            return _clean_key(prompt(f"{config.provider} API key (hidden)"))
        # getpass otherwise falls back to visible stdin when no TTY is available.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return _clean_key(getpass.getpass(f"{config.provider} API key (hidden): "))
    except getpass.GetPassWarning:
        raise ValueError(f"Hidden input is unavailable; configure credentials in /settings or create {target}") from None
    except EOFError:
        raise ValueError(f"No API key available; configure credentials in /settings or create {target}") from None

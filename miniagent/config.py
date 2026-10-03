"""Small, explicit configuration. Credentials never become configuration fields."""

from __future__ import annotations

import getpass
import ipaddress
import math
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PROVIDERS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-flash", ".deepseek_api_key"),
    "openai": ("https://api.openai.com/v1", "gpt-6-astra", ".openai_api_key"),
    "custom": (None, None, ".api_key"),
}
INSTALL_ROOT = Path(__file__).resolve().parent.parent


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
            raise ValueError("provider must be deepseek, openai, or custom")
        default_url, default_model, _ = PROVIDERS[self.provider]
        model = self.model if self.model is not None else default_model
        base_url = self.base_url if self.base_url is not None else default_url
        if not isinstance(model, str) or not model.strip() or any(c.isspace() for c in model):
            raise ValueError("A model name is required; custom providers need --model")
        if base_url is None:
            raise ValueError("Custom providers need --base-url")
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
        return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _clean_key(value: str) -> str:
    key = value.strip()
    if not key or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("API key must be a nonempty single line of printable ASCII")
    return key


def load_api_key(config: Config, workspace: Path) -> str:
    """Read a development key file or prompt invisibly; never consult the environment.

    The workspace wins over the installation directory. Only the selected provider's
    file is read, so a custom endpoint cannot accidentally pick up another key.
    """
    filename = PROVIDERS[config.provider][2]
    directories = dict.fromkeys((Path(workspace).resolve(), INSTALL_ROOT.resolve()))
    for directory in directories:
        path = directory / filename
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                value = handle.read(8193)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            raise ValueError(f"Unable to read {filename}; check the key file and permissions") from None
        if len(value) > 8192:
            raise ValueError(f"{filename} is too large to be an API key")
        return _clean_key(value)
    try:
        # getpass otherwise falls back to visible stdin when no TTY is available.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return _clean_key(getpass.getpass(f"{config.provider} API key (hidden): "))
    except getpass.GetPassWarning:
        raise ValueError(f"Hidden input is unavailable; create {filename} or use an interactive terminal") from None
    except EOFError:
        raise ValueError(f"No API key available; create {filename} or use an interactive terminal") from None

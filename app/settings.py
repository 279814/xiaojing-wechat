from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_NAME = "XiaojingAutosale"
# Backend hosts offered on the login page; the client talks to "<host>/api".
BACKEND_HOST_CHOICES = ("https://www.jujingbuluo123.com", "http://127.0.0.1:3001")
DEFAULT_BACKEND_HOST = "http://127.0.0.1:3001"
BACKEND_API_SUFFIX = "/api"
# Local development only: an agent that answers when the Java backend is down.
AGENT_CHAT_URL_ENV = "AUTOSALE_AGENT_CHAT_URL"


def app_data_dir() -> Path:
    base = os.getenv("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".xiaojing_autosale"


def backend_host(api_base: str) -> str:
    """``api_base`` without its trailing slash and one trailing ``/api``."""
    host = str(api_base or "").strip().rstrip("/")
    if host.lower().endswith(BACKEND_API_SUFFIX):
        host = host[: -len(BACKEND_API_SUFFIX)].rstrip("/")
    return host


def backend_api_base_for_host(host: str) -> str:
    return str(host or "").strip().rstrip("/") + BACKEND_API_SUFFIX


def backend_host_options(saved_api_base: str) -> tuple[list[tuple[str, str]], int]:
    """(label, api_base) rows for the backend dropdown and the row to select.

    The fixed hosts are always offered. A saved value matching neither is kept as
    an extra row with its exact api_base, so opening the page never rewrites it.
    """
    options = [(host, backend_api_base_for_host(host)) for host in BACKEND_HOST_CHOICES]
    saved = str(saved_api_base or "").strip().rstrip("/")
    saved_host = backend_host(saved)
    for index, (host, _api_base) in enumerate(options):
        if saved_host.lower() == host.lower():
            return options, index
    if saved_host:
        options.append((saved_host, saved))
        return options, len(options) - 1
    return options, BACKEND_HOST_CHOICES.index(DEFAULT_BACKEND_HOST)


@dataclass
class AppSettings:
    # Defaults stay local. Existing %APPDATA% settings.json is not rewritten.
    backend_api_base: str = backend_api_base_for_host(DEFAULT_BACKEND_HOST)
    backend_chat_path: str = "/autosale/chat"
    sales_agent_user_id: str = "demo"
    poll_interval_seconds: float = 3.0
    debounce_quiet_seconds: float = 7.0
    first_line_quiet_seconds: float = 3.0
    auto_send_enabled: bool = True
    require_permission_code: str = "AUTOSALE_CLIENT_USE"
    allow_admin_roles: tuple[str, ...] = ("SUPER_ADMIN", "ADMIN")
    customer_remark_keyword: str = "顾客"
    wechat_cli_config: str = ""
    wechat_search_delay_ms: int = 800

    @classmethod
    def load(cls) -> "AppSettings":
        path = settings_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        defaults = asdict(cls())
        merged: dict[str, Any] = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
        roles = merged.get("allow_admin_roles")
        if isinstance(roles, list):
            merged["allow_admin_roles"] = tuple(str(x) for x in roles)
        return cls(**merged)

    def save(self) -> None:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["allow_admin_roles"] = list(self.allow_admin_roles)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def agent_chat_url(self) -> str:
        """Agent fallback URL; set only through the environment, never in the UI."""
        return os.getenv(AGENT_CHAT_URL_ENV, "").strip()

    @property
    def backend_chat_url(self) -> str:
        return self.backend_api_base.rstrip("/") + "/" + self.backend_chat_path.strip("/")

    @property
    def auth_login_url(self) -> str:
        return self.backend_api_base.rstrip("/") + "/auth/login"

    @property
    def auth_me_url(self) -> str:
        return self.backend_api_base.rstrip("/") + "/auth/me"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def state_path() -> Path:
    return app_data_dir() / "state.json"

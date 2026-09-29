from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


APP_NAME = "XiaojingAutosale"
PRODUCTION_AGENT_HOSTS = frozenset(
    host.strip().lower()
    for host in os.getenv("AUTOSALE_PRODUCTION_AGENT_HOSTS", "").split(",")
    if host.strip()
)


def app_data_dir() -> Path:
    base = os.getenv("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".xiaojing_autosale"


def is_production_agent_host(host: str) -> bool:
    return str(host or "").strip().lower() in PRODUCTION_AGENT_HOSTS


@dataclass
class AppSettings:
    # Defaults stay local. Existing %APPDATA% settings.json is not rewritten.
    backend_api_base: str = "http://127.0.0.1:3001/api"
    backend_chat_path: str = "/autosale/chat"
    agent_chat_url: str = ""
    sales_agent_user_id: str = "demo"
    poll_interval_seconds: float = 3.0
    debounce_quiet_seconds: float = 7.0
    first_line_quiet_seconds: float = 3.0
    auto_send_enabled: bool = True
    # Opt-in only: never silently call the known production agent host.
    allow_production_agent_fallback: bool = False
    require_permission_code: str = "AUTOSALE_CLIENT_USE"
    allow_admin_roles: tuple[str, ...] = ("SUPER_ADMIN", "ADMIN")
    customer_remark_keyword: str = "顾客"
    wechat_cli_config: str = ""
    wechat_search_delay_ms: int = 450

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
    def backend_chat_url(self) -> str:
        return self.backend_api_base.rstrip("/") + "/" + self.backend_chat_path.strip("/")

    @property
    def auth_login_url(self) -> str:
        return self.backend_api_base.rstrip("/") + "/auth/login"

    @property
    def auth_me_url(self) -> str:
        return self.backend_api_base.rstrip("/") + "/auth/me"

    def agent_host(self) -> str:
        return (urlparse(str(self.agent_chat_url or "")).hostname or "").strip().lower()


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def state_path() -> Path:
    return app_data_dir() / "state.json"

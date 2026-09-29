from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .settings import AppSettings
from .state_store import StoredUser


@dataclass
class AuthResult:
    ok: bool
    message: str
    user: StoredUser | None = None


class AuthClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def login(self, username: str, password: str) -> AuthResult:
        try:
            resp = requests.post(
                self.settings.auth_login_url,
                json={"username": username, "password": password},
                timeout=20,
            )
            payload = resp.json()
        except Exception as exc:
            return AuthResult(False, f"登录请求失败: {exc}")
        if resp.status_code >= 400 or not bool(payload.get("success")):
            return AuthResult(False, str(payload.get("message") or "登录失败"))
        data = payload.get("data") or {}
        user = StoredUser.from_payload(data)
        if not user.token:
            return AuthResult(False, "登录成功但没有拿到 token")
        if not user.can_use_autosale(self.settings.require_permission_code, self.settings.allow_admin_roles):
            return AuthResult(False, "当前账号无 Autosale 客户端使用权限")
        return AuthResult(True, str(payload.get("message") or "登录成功"), user)

    def me(self, token: str) -> AuthResult:
        if not token:
            return AuthResult(False, "token 为空")
        try:
            resp = requests.get(
                self.settings.auth_me_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            payload = resp.json()
        except Exception as exc:
            return AuthResult(False, f"登录状态校验失败: {exc}")
        if resp.status_code >= 400 or not bool(payload.get("success")):
            return AuthResult(False, str(payload.get("message") or "登录状态失效"))
        data: dict[str, Any] = payload.get("data") or {}
        if "token" not in data:
            data["token"] = token
        user = StoredUser.from_payload(data)
        if not user.can_use_autosale(self.settings.require_permission_code, self.settings.allow_admin_roles):
            return AuthResult(False, "当前账号无 Autosale 客户端使用权限")
        return AuthResult(True, "登录状态有效", user)


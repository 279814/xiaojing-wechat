from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .settings import state_path


@dataclass
class StoredUser:
    token: str = ""
    user_id: int = 0
    username: str = ""
    display_name: str = ""
    roles: list[str] | None = None
    permissions: list[str] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "StoredUser":
        return cls(
            token=str(payload.get("token") or ""),
            user_id=int(payload.get("userId") or payload.get("user_id") or 0),
            username=str(payload.get("username") or ""),
            display_name=str(payload.get("displayName") or payload.get("display_name") or ""),
            roles=[str(x) for x in payload.get("roles") or []],
            permissions=[str(x) for x in payload.get("permissions") or []],
        )

    def can_use_autosale(self, permission_code: str, admin_roles: tuple[str, ...]) -> bool:
        role_set = set(self.roles or [])
        perm_set = set(self.permissions or [])
        return bool(role_set.intersection(admin_roles) or permission_code in perm_set)

    @property
    def label(self) -> str:
        return self.display_name or self.username or "unknown"


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_path()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"processed_messages": [], "user": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"processed_messages": [], "user": {}}
        if not isinstance(data, dict):
            return {"processed_messages": [], "user": {}}
        data.setdefault("processed_messages", [])
        data.setdefault("user", {})
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_user(self, user: StoredUser) -> None:
        self.data["user"] = asdict(user)
        self.save()

    def load_user(self) -> StoredUser:
        payload = self.data.get("user")
        if not isinstance(payload, dict):
            return StoredUser()
        return StoredUser.from_payload(payload)

    def clear_user(self) -> None:
        self.data["user"] = {}
        self.save()

    def is_processed(self, fingerprint: str) -> bool:
        return fingerprint in set(self.data.get("processed_messages") or [])

    def mark_processed(self, fingerprint: str) -> None:
        if not fingerprint:
            return
        items = [str(x) for x in self.data.get("processed_messages") or []]
        if fingerprint not in items:
            items.append(fingerprint)
        self.data["processed_messages"] = items[-500:]
        self.save()


def message_fingerprint(username: str, timestamp: int | float | str, message: str) -> str:
    raw = f"{username}|{timestamp}|{message}"
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return digest

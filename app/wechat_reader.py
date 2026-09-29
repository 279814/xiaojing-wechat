from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .settings import AppSettings
from .wechat_cli_bundle import ensure_wechat_cli_import_path


@dataclass
class WechatContact:
    username: str
    nick_name: str
    remark: str

    @property
    def display_name(self) -> str:
        return self.remark or self.nick_name or self.username


@dataclass
class WechatMessage:
    chat: str
    username: str
    is_group: bool
    last_message: str
    timestamp: int
    time_text: str = ""
    unread: int = 0


class WechatCliError(RuntimeError):
    pass


class WechatReader:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._customers_by_username: dict[str, WechatContact] = {}

    def _run(self, args: list[str]) -> Any:
        full_args = list(args)
        if self.settings.wechat_cli_config:
            full_args = ["--config", self.settings.wechat_cli_config, *full_args]
        ensure_wechat_cli_import_path()
        try:
            from click.testing import CliRunner
            from wechat_cli.main import cli

            result = CliRunner().invoke(cli, full_args)
            if result.exit_code != 0:
                raise WechatCliError(result.output.strip() or f"wechat-cli exited {result.exit_code}")
            return json.loads(result.output or "null")
        except ImportError as exc:
            if shutil.which("wechat-cli") is None:
                raise WechatCliError(f"内置 wechat-cli 不可用: {exc}") from exc
            cmd = ["wechat-cli", *full_args]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                raise WechatCliError((proc.stderr or proc.stdout or "").strip() or f"wechat-cli exited {proc.returncode}")
            return json.loads(proc.stdout or "null")

    def refresh_customer_contacts(self) -> list[WechatContact]:
        raw = self._run(["contacts", "--limit", "100000"])
        if not isinstance(raw, list):
            raise WechatCliError("contacts 输出不是列表")
        customers: list[WechatContact] = []
        keyword = self.settings.customer_remark_keyword
        for item in raw:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip()
            remark = str(item.get("remark") or "").strip()
            nick_name = str(item.get("nick_name") or "").strip()
            if not username or "@chatroom" in username or username.startswith("gh_"):
                continue
            contact = WechatContact(username=username, nick_name=nick_name, remark=remark)
            # WeChat 4 has no stored display-name column: the chat list and chat title
            # show contact.remark when set, otherwise contact.nick_name.
            if keyword and keyword not in contact.display_name:
                continue
            customers.append(contact)
        self._customers_by_username = {item.username: item for item in customers}
        return customers

    def customer_for_username(self, username: str) -> WechatContact | None:
        return self._customers_by_username.get(username)

    def fetch_customer_unread(self) -> list[WechatMessage]:
        if not self._customers_by_username:
            self.refresh_customer_contacts()
        raw = self._run(["unread", "--limit", "100"])
        if not isinstance(raw, list):
            raise WechatCliError("unread 输出不是列表")
        messages: list[WechatMessage] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip()
            if username not in self._customers_by_username:
                continue
            is_group = bool(item.get("is_group"))
            if is_group:
                continue
            messages.append(
                WechatMessage(
                    chat=str(item.get("chat") or self._customers_by_username[username].display_name),
                    username=username,
                    is_group=is_group,
                    last_message=str(item.get("last_message") or ""),
                    timestamp=int(item.get("timestamp") or 0),
                    time_text=str(item.get("time") or ""),
                    unread=int(item.get("unread") or 0),
                )
            )
        return messages

    def fetch_new_customer_messages(self) -> list[WechatMessage]:
        if not self._customers_by_username:
            self.refresh_customer_contacts()
        raw = self._run(["new-messages"])
        items = raw.get("messages") if isinstance(raw, dict) else []
        if not isinstance(items, list):
            return []
        messages: list[WechatMessage] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip()
            if username not in self._customers_by_username or bool(item.get("is_group")):
                continue
            messages.append(
                WechatMessage(
                    chat=str(item.get("chat") or self._customers_by_username[username].display_name),
                    username=username,
                    is_group=False,
                    last_message=str(item.get("last_message") or ""),
                    timestamp=int(item.get("timestamp") or 0),
                    time_text=str(item.get("time") or ""),
                    unread=int(item.get("unread") or 0),
                )
            )
        return messages

    def fetch_recent_inbound_messages(self, username: str, *, limit: int = 10) -> list[WechatMessage]:
        """Return recent inbound (customer) text lines for one chat, oldest first.

        ``wechat-cli history`` only prints "[YYYY-MM-DD HH:MM] sender: text"
        strings, so the rows are read directly: a backfilled line must carry the
        exact second ``unread`` reports or its fingerprint never matches the
        lines already seen or answered.
        """
        if not username or "@chatroom" in username:
            return []
        contact = self._customers_by_username.get(username)
        count = max(1, int(limit))
        messages = [
            WechatMessage(
                chat=contact.display_name if contact else username,
                username=username,
                is_group=False,
                last_message=text,
                timestamp=create_time,
                time_text=datetime.fromtimestamp(create_time).strftime("%m-%d %H:%M"),
                unread=0,
            )
            for create_time, text in self._recent_inbound_rows(username, count * 3)
        ]
        messages.sort(key=lambda msg: (int(msg.timestamp or 0), msg.last_message))
        return messages[-count:]

    def _recent_inbound_rows(self, username: str, scan_limit: int) -> list[tuple[int, str]]:
        ensure_wechat_cli_import_path()
        from wechat_cli.core.context import AppContext
        from wechat_cli.core.messages import (
            _load_name2id_maps,
            _query_messages,
            decompress_content,
            resolve_chat_context,
        )

        app = AppContext(self.settings.wechat_cli_config or None)
        chat = resolve_chat_context(username, app.msg_db_keys, app.cache, app.decrypted_dir)
        rows: list[tuple[int, str]] = []
        for table in (chat or {}).get("message_tables") or []:
            with closing(sqlite3.connect(table["db_path"])) as conn:
                sender_by_id = _load_name2id_maps(conn)
                for _local_id, local_type, create_time, sender_id, content, content_type in _query_messages(
                    conn, table["table_name"], limit=scan_limit
                ):
                    if sender_by_id.get(sender_id) != username or (int(local_type or 0) & 0xFFFFFFFF) != 1:
                        continue
                    text = str(decompress_content(content, content_type) or "").strip()
                    if text:
                        rows.append((int(create_time or 0), text))
        return rows

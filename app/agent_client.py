from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .settings import AppSettings, is_production_agent_host
from .state_store import StoredUser, message_fingerprint
from .wechat_reader import WechatContact, WechatMessage

# Backend stops its own agent wait after ~180s and answers "processing"; later
# re-posts of the same message read the finished run, so keep re-posting well
# past that instead of reporting a failure.
BACKEND_PROCESSING_WAIT_SECONDS = 420


def effective_agent_user_id(user: StoredUser, configured: str = "") -> str:
    """The salesperson the agent should answer as.

    The login account is that identity. A separately typed agent id is not used,
    so a stale value such as ``001`` cannot be sent for another salesperson.
    """
    username = str(getattr(user, "username", "") or "").strip()
    if username:
        return username
    return str(configured or "").strip() or "demo_sales"


@dataclass
class ReplyResult:
    ok: bool
    message: str
    reply: str = ""
    status: str = ""
    message_id: str = ""
    can_auto_send: bool = False
    raw: dict[str, Any] | None = None


@dataclass
class SendClaimResult:
    ok: bool
    message: str
    status: str = ""
    message_id: str = ""
    raw: dict[str, Any] | None = None


def extract_can_auto_send(payload: dict[str, Any] | None) -> bool:
    """Read can_auto_send from Java (canAutoSend) or agent (can_auto_send) payloads."""
    if not isinstance(payload, dict):
        return False
    if "canAutoSend" in payload:
        return bool(payload.get("canAutoSend"))
    if "can_auto_send" in payload:
        return bool(payload.get("can_auto_send"))
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    if "can_auto_send" in raw:
        return bool(raw.get("can_auto_send"))
    if "canAutoSend" in raw:
        return bool(raw.get("canAutoSend"))
    return False


def extract_client_reply(payload: dict[str, Any] | None) -> str:
    """Prefer the customer-visible draft even when auto-send is blocked."""
    if not isinstance(payload, dict):
        return ""
    for key in ("reply", "content"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    for key in ("reply", "agent_reply", "content"):
        text = str(raw.get(key) or "").strip()
        if text:
            return text
    return ""


def should_auto_send_reply(*, settings: AppSettings, can_auto_send: bool, reply: str) -> bool:
    """Match packaged client: local auto-send checkbox controls paste/send.

    ``can_auto_send`` is the backend quality-gate flag and is logged by callers;
    it does not block the local paste/send action when the UI switch is on.
    """
    _ = can_auto_send  # retained for call-site logging / API compatibility
    return bool(settings.auto_send_enabled and str(reply or "").strip())


def agent_fallback_allowed(settings: AppSettings) -> bool:
    """Never silently fall back to the known production agent host."""
    url = str(settings.agent_chat_url or "").strip()
    if not url:
        return False
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if is_production_agent_host(host) and not bool(settings.allow_production_agent_fallback):
        return False
    return True


class AgentClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def generate_reply(
        self,
        user: StoredUser,
        contact: WechatContact | None,
        msg: WechatMessage,
        requested_mode: str,
        *,
        batch_messages: list[WechatMessage] | None = None,
    ) -> ReplyResult:
        messages = list(batch_messages or [msg])
        if not messages:
            return ReplyResult(False, "没有可发送的顾客消息")
        primary = messages[-1]
        message_id = message_fingerprint(primary.username, primary.timestamp, primary.last_message)
        if len(messages) > 1:
            message_id = "batch_" + message_fingerprint(
                primary.username,
                "|".join(f"{item.timestamp}:{item.last_message}" for item in messages),
                str(len(messages)),
            )
        unread_rows = [
            {
                "messageId": message_fingerprint(item.username, item.timestamp, item.last_message),
                "message_id": message_fingerprint(item.username, item.timestamp, item.last_message),
                "index": index,
                "sender": "customer",
                "time": item.time_text or "",
                "timestamp": int(item.timestamp or 0),
                "content": item.last_message,
            }
            for index, item in enumerate(messages, start=1)
        ]
        payload = {
            "messageId": message_id,
            "message_id": message_id,
            "customerWechatId": primary.username,
            "customerName": contact.display_name if contact else primary.chat,
            "customerRemark": contact.remark if contact else "",
            "message": primary.last_message if len(messages) == 1 else "",
            "requestedMode": requested_mode,
            "agentUserId": effective_agent_user_id(user, self.settings.sales_agent_user_id),
            "autoSend": self.settings.auto_send_enabled,
            "auto_send": self.settings.auto_send_enabled,
        }
        if len(messages) > 1:
            payload["unreadMessages"] = unread_rows
            payload["messageBatch"] = {
                "type": "wechat_unread_batch",
                "batchId": message_id,
                "messageCount": len(unread_rows),
                "messages": unread_rows,
            }
        else:
            payload["message"] = primary.last_message
        result = self._try_backend_chat(user, payload)
        if result.ok:
            return result
        if result.status == "processing":
            return result
        if agent_fallback_allowed(self.settings):
            return self._try_agent_fallback(
                user,
                contact,
                primary,
                requested_mode,
                message_id,
                result.message,
                batch_messages=messages if len(messages) > 1 else None,
            )
        return result

    def _try_backend_chat(self, user: StoredUser, payload: dict[str, Any]) -> ReplyResult:
        deadline = time.monotonic() + BACKEND_PROCESSING_WAIT_SECONDS
        last = ReplyResult(False, "后端正在处理这条消息")
        while time.monotonic() < deadline:
            last = self._post_backend_chat_once(user, payload)
            if last.ok or last.status != "processing":
                return last
            time.sleep(2.0)
        return last

    def _post_backend_chat_once(self, user: StoredUser, payload: dict[str, Any]) -> ReplyResult:
        try:
            resp = requests.post(
                self.settings.backend_chat_url,
                json=payload,
                headers={"Authorization": f"Bearer {user.token}"},
                timeout=200,
            )
            data = resp.json()
        except Exception as exc:
            return ReplyResult(False, f"后端 autosale 接口请求失败: {exc}")
        if resp.status_code == 404:
            return ReplyResult(False, "后端尚未提供 /api/autosale/chat 接口")
        if resp.status_code >= 400 or not bool(data.get("success")):
            return ReplyResult(False, str(data.get("message") or "后端生成回复失败"), raw=data)
        detail = data.get("data") or {}
        status = str(detail.get("status") or data.get("status") or "").strip()
        message_id = str(detail.get("messageId") or detail.get("message_id") or payload.get("message_id") or "")
        if status == "processing":
            return ReplyResult(False, "后端正在处理这条消息，请稍后重试", status=status, message_id=message_id, raw=data)
        if status == "sending":
            return ReplyResult(False, "这条消息已经有客户端正在自动发送", status=status, message_id=message_id, raw=data)
        if status == "failed":
            # Real failure only: empty draft / agent error. needs_review is not failed.
            reply_even_if_failed = extract_client_reply(detail)
            if reply_even_if_failed:
                can_auto = extract_can_auto_send(detail)
                return ReplyResult(
                    True,
                    str(detail.get("error") or detail.get("errorMessage") or "生成成功（未自动发送）"),
                    reply=reply_even_if_failed,
                    status=status,
                    message_id=message_id,
                    can_auto_send=can_auto,
                    raw=data,
                )
            return ReplyResult(
                False,
                str(detail.get("error") or detail.get("errorMessage") or "后端处理失败"),
                status=status,
                message_id=message_id,
                raw=data,
            )
        reply = extract_client_reply(detail)
        can_auto = extract_can_auto_send(detail)
        if not reply.strip():
            return ReplyResult(False, "后端返回为空", status=status, message_id=message_id, raw=data)
        if status == "needs_review":
            return ReplyResult(
                True,
                "已生成建议（质量门未放行自动发送）",
                reply=reply.strip(),
                status=status,
                message_id=message_id,
                can_auto_send=False,
                raw=data,
            )
        return ReplyResult(
            True,
            "生成成功",
            reply=reply.strip(),
            status=status or "completed",
            message_id=message_id,
            can_auto_send=can_auto,
            raw=data,
        )

    def _try_agent_fallback(
        self,
        user: StoredUser,
        contact: WechatContact | None,
        msg: WechatMessage,
        requested_mode: str,
        message_id: str,
        previous_error: str,
        *,
        batch_messages: list[WechatMessage] | None = None,
    ) -> ReplyResult:
        customer_name = contact.display_name if contact else msg.chat
        payload: dict[str, Any] = {
            "message_id": message_id,
            "user_id": f"wechat_{msg.username}",
            "message": msg.last_message,
            "requested_mode": requested_mode,
            "auto_send": self.settings.auto_send_enabled,
            "strategy_profile": {
                "requested_mode": requested_mode,
                "agent_user_id": effective_agent_user_id(user, self.settings.sales_agent_user_id),
                "customer_name": customer_name,
            },
        }
        if batch_messages and len(batch_messages) > 1:
            unread_rows = [
                {
                    "message_id": message_fingerprint(item.username, item.timestamp, item.last_message),
                    "index": index,
                    "sender": "customer",
                    "time": item.time_text or "",
                    "timestamp": int(item.timestamp or 0),
                    "content": item.last_message,
                }
                for index, item in enumerate(batch_messages, start=1)
            ]
            payload["message"] = ""
            payload["unread_messages"] = unread_rows
            payload["message_batch"] = {
                "type": "wechat_unread_batch",
                "batchId": message_id,
                "messageCount": len(unread_rows),
                "messages": unread_rows,
            }
        deadline = time.monotonic() + 150
        last_status = ""
        last_data: dict[str, Any] | None = None
        last_error = previous_error
        while time.monotonic() < deadline:
            result = self._post_agent_chat(payload, previous_error)
            last_status = result.status
            last_data = result.raw
            if result.ok:
                return result
            if result.status == "processing":
                time.sleep(2.0)
                continue
            if result.status in {"sending", "sent"}:
                return result
            last_error = result.message
            break
        if last_status == "processing":
            return ReplyResult(
                False,
                "Agent 仍在处理这条消息，请稍后再次点击生成回复",
                status=last_status,
                message_id=message_id,
                raw=last_data,
            )
        return ReplyResult(False, last_error, status=last_status, message_id=message_id, raw=last_data)

    def _post_agent_chat(self, payload: dict[str, Any], previous_error: str) -> ReplyResult:
        try:
            resp = requests.post(self.settings.agent_chat_url, json=payload, timeout=200)
            data = resp.json()
        except Exception as exc:
            return ReplyResult(False, f"{previous_error}; Agent 兜底请求失败: {exc}", message_id=str(payload.get("message_id") or ""))
        status = str(data.get("status") or "").strip()
        message_id = str(data.get("message_id") or payload.get("message_id") or "")
        if status == "processing":
            return ReplyResult(False, str(data.get("message") or "Agent 正在处理这条消息"), status=status, message_id=message_id, raw=data)
        if status == "sending":
            return ReplyResult(False, str(data.get("message") or "这条消息已经有客户端正在自动发送"), status=status, message_id=message_id, raw=data)
        if resp.status_code >= 400 or not bool(data.get("ok")):
            return ReplyResult(False, str(data.get("error") or data.get("message") or previous_error), status=status or "failed", message_id=message_id, raw=data)
        reply = str(data.get("reply") or "")
        can_auto = extract_can_auto_send(data)
        if not reply.strip():
            return ReplyResult(False, "Agent 返回为空", status=status, message_id=message_id, raw=data)
        already = "，复用了已处理结果" if bool(data.get("already_processed")) else ""
        return ReplyResult(
            True,
            f"生成成功{already}",
            reply=reply.strip(),
            status=status or "completed",
            message_id=message_id,
            can_auto_send=can_auto,
            raw=data,
        )

    def claim_message_send(self, message_id: str, *, username: str = "") -> SendClaimResult:
        if not self.settings.agent_chat_url or not message_id:
            return SendClaimResult(True, "无需远端发送锁", message_id=message_id)
        host = urlparse(self.settings.agent_chat_url).hostname or ""
        if is_production_agent_host(host) and not self.settings.allow_production_agent_fallback:
            return SendClaimResult(True, "已跳过生产 Agent 发送锁", message_id=message_id)
        payload = {
            "message_id": message_id,
            "source": "windows-autosale",
            "updated_by": username,
            "ttl_seconds": 180,
        }
        try:
            resp = requests.post(self._agent_send_claim_url(), json=payload, timeout=10)
            data = resp.json()
        except Exception as exc:
            return SendClaimResult(False, f"自动发送锁获取失败: {exc}", message_id=message_id)
        status = str(data.get("status") or "").strip()
        claimed = bool(data.get("send_claimed"))
        if resp.status_code >= 400 or not bool(data.get("ok")):
            return SendClaimResult(False, str(data.get("error") or data.get("message") or "自动发送锁获取失败"), status=status, message_id=message_id, raw=data)
        if claimed:
            return SendClaimResult(True, "已获得自动发送权", status=status, message_id=str(data.get("message_id") or message_id), raw=data)
        if status == "sent":
            return SendClaimResult(False, "这条消息已经自动发送过，不再重复发送", status=status, message_id=str(data.get("message_id") or message_id), raw=data)
        if status == "sending":
            return SendClaimResult(False, "这条消息已经有客户端正在自动发送，不再重复发送", status=status, message_id=str(data.get("message_id") or message_id), raw=data)
        return SendClaimResult(False, str(data.get("message") or "这条消息暂时不能自动发送"), status=status, message_id=str(data.get("message_id") or message_id), raw=data)

    def mark_message_sent(self, message_id: str, *, ok: bool, error_message: str = "", username: str = "") -> None:
        if not self.settings.agent_chat_url or not message_id:
            return
        host = urlparse(self.settings.agent_chat_url).hostname or ""
        if is_production_agent_host(host) and not self.settings.allow_production_agent_fallback:
            return
        url = self._agent_send_status_url()
        payload = {
            "message_id": message_id,
            "status": "sent" if ok else "failed",
            "sent": ok,
            "error": error_message,
            "source": "windows-autosale",
            "updated_by": username,
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception:
            return

    def _agent_send_claim_url(self) -> str:
        url = self.settings.agent_chat_url.strip().rstrip("/")
        suffix = "/api/chat"
        if url.endswith(suffix):
            return url[: -len(suffix)] + "/api/chat/send-claim"
        return url + "/send-claim"

    def _agent_send_status_url(self) -> str:
        url = self.settings.agent_chat_url.strip().rstrip("/")
        suffix = "/api/chat"
        if url.endswith(suffix):
            return url[: -len(suffix)] + "/api/chat/send-status"
        return url + "/send-status"

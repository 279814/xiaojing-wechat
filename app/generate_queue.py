"""Helpers for offloading Agent generate/send work without freezing the Qt UI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Collection, Literal

from PySide6.QtCore import QThread, Signal

from .agent_client import AgentClient, ReplyResult
from .state_store import StoredUser, message_fingerprint
from .wechat_reader import WechatContact, WechatMessage
from .wechat_sender import WechatSender, WechatSenderError

GenerateAction = Literal["start", "queue"]
SendAction = Literal["start", "queue"]

# Backend rejects batches above 20 lines.
MAX_BATCH_MESSAGES = 20
# After this many dropped replies in a row the next reply is sent even if the
# customer typed again, so a customer who keeps typing still gets an answer.
MAX_CONSECUTIVE_SUPERSEDES = 3


def decide_generate_action(username: str, in_flight: Collection[str]) -> GenerateAction:
    """Serialize generate HTTP work per customer; never start a second in-flight call."""
    key = str(username or "").strip()
    if key and key in in_flight:
        return "queue"
    return "start"


def decide_send_action(send_busy: bool) -> SendAction:
    """Serialize WeChat paste/send globally; pywinauto must not overlap."""
    return "queue" if send_busy else "start"


@dataclass
class GenerateReplyRequest:
    username: str
    user: StoredUser
    contact: WechatContact | None
    message: WechatMessage
    batch_messages: list[WechatMessage] = field(default_factory=list)
    requested_mode: str = "discuss"
    created_at: float = field(default_factory=time.time)


@dataclass
class GenerateReplyJobResult:
    request: GenerateReplyRequest
    result: ReplyResult


@dataclass
class SendReplyRequest:
    message: WechatMessage
    contact: WechatContact | None
    text: str
    agent_message_id: str
    auto_send_claimed: bool
    auto_send_enabled: bool
    employee_username: str
    update_current_claim: bool
    pre_search_query: str = ""
    # Set on the auto-send path only; a manual send is never dropped as stale.
    source_batch: list[WechatMessage] = field(default_factory=list)
    generated_for_at: float | None = None


@dataclass
class SendReplyJobResult:
    request: SendReplyRequest
    ok: bool
    message: str
    claimed: bool = False
    error: str = ""


class GenerateReplyWorker(QThread):
    """Run AgentClient.generate_reply (long HTTP poll) off the Qt GUI thread."""

    finished_job = Signal(object)

    def __init__(self, agent: AgentClient, request: GenerateReplyRequest) -> None:
        super().__init__()
        self.agent = agent
        self.request = request

    def run(self) -> None:
        req = self.request
        batch = list(req.batch_messages or [req.message])
        result = self.agent.generate_reply(
            req.user,
            req.contact,
            req.message,
            req.requested_mode,
            batch_messages=batch if len(batch) > 1 else None,
        )
        self.finished_job.emit(GenerateReplyJobResult(request=req, result=result))


class SendReplyWorker(QThread):
    """Run claim + pywinauto paste/send + mark-sent off the Qt GUI thread."""

    finished_job = Signal(object)

    def __init__(
        self,
        agent: AgentClient,
        sender: WechatSender,
        request: SendReplyRequest,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.sender = sender
        self.request = request

    def run(self) -> None:
        req = self.request
        claimed = bool(req.auto_send_claimed)
        agent_message_id = str(req.agent_message_id or "")
        auto_send_now = bool(req.auto_send_enabled)
        com_ready = False
        try:
            import pythoncom

            pythoncom.CoInitialize()
            com_ready = True
        except Exception:
            com_ready = False
        try:
            if agent_message_id and auto_send_now and not claimed:
                claim = self.agent.claim_message_send(
                    agent_message_id,
                    username=req.employee_username,
                )
                if claim.ok:
                    claimed = True
                elif claim.status in {"sent", "sending"}:
                    # Packaged client also skips duplicates; do not paste again.
                    self.finished_job.emit(
                        SendReplyJobResult(
                            request=req,
                            ok=False,
                            message=claim.message,
                            claimed=False,
                            error=claim.message,
                        )
                    )
                    return
                # Network/claim errors must not block the packaged paste-send path.
            query = req.contact.display_name if req.contact else req.message.chat
            try:
                self.sender.send_reply(
                    query,
                    req.text,
                    auto_send=auto_send_now,
                    pre_search_query=req.pre_search_query,
                )
            except WechatSenderError as exc:
                if agent_message_id:
                    self.agent.mark_message_sent(
                        agent_message_id,
                        ok=False,
                        error_message=str(exc),
                        username=req.employee_username,
                    )
                self.finished_job.emit(
                    SendReplyJobResult(
                        request=req,
                        ok=False,
                        message=str(exc),
                        claimed=claimed,
                        error=str(exc),
                    )
                )
                return
            if agent_message_id and auto_send_now:
                self.agent.mark_message_sent(
                    agent_message_id,
                    ok=True,
                    username=req.employee_username,
                )
            done = "已粘贴到微信" + ("并发送" if auto_send_now else "，请确认后发送")
            self.finished_job.emit(
                SendReplyJobResult(
                    request=req,
                    ok=True,
                    message=done,
                    claimed=claimed,
                )
            )
        finally:
            if com_ready:
                try:
                    import pythoncom

                    pythoncom.CoUninitialize()
                except Exception:
                    pass


def mark_in_flight(in_flight: set[str], username: str) -> None:
    key = str(username or "").strip()
    if key:
        in_flight.add(key)


def clear_in_flight(in_flight: set[str], username: str) -> None:
    in_flight.discard(str(username or "").strip())


def store_queued_request(
    queued: dict[str, Any],
    request: GenerateReplyRequest,
) -> None:
    """Keep one pending request per customer while a generate is in flight.

    A newer request replaces the older queued one but keeps its unanswered lines.
    """
    key = str(request.username or "").strip()
    if not key:
        return
    previous = queued.get(key)
    if isinstance(previous, GenerateReplyRequest):
        request.batch_messages = merge_batch_messages(
            request_batch(previous),
            request_batch(request),
        )
    queued[key] = request


def request_batch(request: GenerateReplyRequest) -> list[WechatMessage]:
    return list(request.batch_messages or [request.message])


def merge_batch_messages(*batches: list[WechatMessage]) -> list[WechatMessage]:
    """Union customer lines oldest first, dropping repeats, capped at the backend limit."""
    seen: set[str] = set()
    merged: list[WechatMessage] = []
    for batch in batches:
        for msg in batch or []:
            key = message_fingerprint(msg.username, msg.timestamp, msg.last_message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(msg)
    merged.sort(key=lambda m: int(m.timestamp or 0))
    return merged[-MAX_BATCH_MESSAGES:]


def is_reply_superseded(
    generated_for_at: float | None,
    latest_inbound_at: float | None,
    *,
    has_newer_request: bool,
    consecutive_supersedes: int,
) -> bool:
    """A reply is stale when the customer sent another line after its batch was taken.

    The newer request then answers every unanswered line at once instead of the
    customer receiving an answer to a question they already moved past.
    """
    if consecutive_supersedes >= MAX_CONSECUTIVE_SUPERSEDES:
        return False
    if has_newer_request:
        return True
    if generated_for_at is None or latest_inbound_at is None:
        return False
    return float(latest_inbound_at) > float(generated_for_at)

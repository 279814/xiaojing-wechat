from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class SupportsFingerprint(Protocol):
    def is_processed(self, fingerprint: str) -> bool: ...

    def mark_processed(self, fingerprint: str) -> None: ...


@dataclass
class PendingCustomerBatch:
    username: str
    chat: str
    quiet_deadline: float
    messages: list[Any] = field(default_factory=list)
    seen_fingerprints: set[str] = field(default_factory=set)
    max_unread: int = 0


PENDING_POLL_SECONDS = 1.5
FIRST_LINE_QUIET_SECONDS = 3.0
_OPENER_TEXTS = frozenset(
    {
        "你好",
        "您好",
        "在吗",
        "在么",
        "在不在",
        "在",
        "有人吗",
        "嗨",
        "哈喽",
        "hi",
        "hello",
        "早",
        "早上好",
        "中午好",
        "下午好",
        "晚上好",
    }
)


def clamp_quiet_seconds(value: float) -> float:
    return max(6.0, min(8.0, float(value)))


def clamp_first_line_quiet_seconds(value: float | None, quiet_seconds: float) -> float:
    first = FIRST_LINE_QUIET_SECONDS if value is None else float(value)
    return max(2.0, min(first, clamp_quiet_seconds(quiet_seconds)))


def is_conversation_opener(text: Any) -> bool:
    """Greetings usually precede the real question, so they keep the full window."""
    compact = re.sub(r"[\s，,。.!！~～?？、…]+", "", str(text or "")).lower()
    return not compact or compact in _OPENER_TEXTS


def next_poll_delay(poll_interval_seconds: float, *, has_pending: bool) -> float:
    """Poll quickly while a quiet window is open so a burst is seen before it closes."""
    interval = max(1.0, float(poll_interval_seconds))
    return min(interval, PENDING_POLL_SECONDS) if has_pending else interval


def note_unread_message(
    pending_by_user: dict[str, PendingCustomerBatch],
    *,
    msg: Any,
    fingerprint: str,
    quiet_seconds: float,
    is_processed: Callable[[str], bool],
    now: float | None = None,
    first_line_quiet_seconds: float | None = None,
) -> bool:
    """Track one unread observation. Returns True only when a new line was added.

    Only a new line restarts the quiet window; seeing the same unread line on the
    next poll must not, or a customer who stays unread would never be answered.
    A lone substantive first line closes after the short first-line window; a
    greeting, a backlog (unread > 1) or any further line uses the full window.
    """
    if not str(getattr(msg, "last_message", "") or "").strip():
        return False
    if is_processed(fingerprint):
        return False
    current = time.time() if now is None else float(now)
    quiet = clamp_quiet_seconds(quiet_seconds)
    username = str(getattr(msg, "username", "") or "")
    pending = pending_by_user.get(username)
    if pending is None:
        pending = PendingCustomerBatch(
            username=username,
            chat=str(getattr(msg, "chat", "") or ""),
            quiet_deadline=current + quiet,
        )
        pending_by_user[username] = pending
    else:
        pending.chat = str(getattr(msg, "chat", "") or pending.chat)
    pending.max_unread = max(pending.max_unread, int(getattr(msg, "unread", 0) or 0))
    if fingerprint in pending.seen_fingerprints:
        return False
    pending.seen_fingerprints.add(fingerprint)
    pending.messages.append(msg)
    lone_first_line = (
        len(pending.messages) == 1
        and pending.max_unread <= 1
        and not is_conversation_opener(getattr(msg, "last_message", ""))
    )
    window = clamp_first_line_quiet_seconds(first_line_quiet_seconds, quiet_seconds) if lone_first_line else quiet
    pending.quiet_deadline = current + window
    return True


def materialize_batch(
    pending: PendingCustomerBatch,
    *,
    is_processed: Callable[[str], bool],
    fingerprint_fn: Callable[[Any], str],
    history_loader: Callable[[str, int], list[Any]] | None = None,
) -> list[Any]:
    collected = list(pending.messages)
    need = max(pending.max_unread, len(collected), 1)
    if history_loader is not None and (need > len(collected) or pending.max_unread > 1):
        try:
            history = history_loader(pending.username, max(need + 2, 8))
        except Exception:
            history = []
        if history:
            by_fp = {fingerprint_fn(item): item for item in collected}
            for item in history:
                fp = fingerprint_fn(item)
                if is_processed(fp):
                    continue
                by_fp[fp] = item
            ordered = sorted(
                by_fp.values(),
                key=lambda m: (int(getattr(m, "timestamp", 0) or 0), str(getattr(m, "last_message", "") or "")),
            )
            if ordered:
                return ordered[-max(need, len(collected)) :]
    collected.sort(
        key=lambda m: (int(getattr(m, "timestamp", 0) or 0), str(getattr(m, "last_message", "") or ""))
    )
    return collected


def flush_ready_batches(
    pending_by_user: dict[str, PendingCustomerBatch],
    *,
    state: SupportsFingerprint,
    fingerprint_fn: Callable[[Any], str],
    history_loader: Callable[[str, int], list[Any]] | None = None,
    now: float | None = None,
    observed_until: float | None = None,
) -> list[tuple[list[Any], list[str]]]:
    """Emit batches whose quiet window has closed.

    ``observed_until`` is when the latest unread poll started.  A batch is only
    final once a poll that began after its deadline saw no newer line, so a
    message sent just before the deadline is not split into the next batch.
    """
    current = time.time() if now is None else float(now)
    cutoff = current if observed_until is None else min(current, float(observed_until))
    ready = [item for item in pending_by_user.values() if cutoff >= item.quiet_deadline]
    flushed: list[tuple[list[Any], list[str]]] = []
    for pending in ready:
        pending_by_user.pop(pending.username, None)
        batch = materialize_batch(
            pending,
            is_processed=state.is_processed,
            fingerprint_fn=fingerprint_fn,
            history_loader=history_loader,
        )
        if not batch:
            continue
        fingerprints: list[str] = []
        kept: list[Any] = []
        for msg in batch:
            fp = fingerprint_fn(msg)
            if state.is_processed(fp):
                continue
            state.mark_processed(fp)
            fingerprints.append(fp)
            kept.append(msg)
        if fingerprints:
            flushed.append((kept, fingerprints))
    return flushed

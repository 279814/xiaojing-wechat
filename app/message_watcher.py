from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal

from .settings import AppSettings
from .state_store import StateStore, message_fingerprint
from .unread_debounce import (
    PendingCustomerBatch,
    clamp_first_line_quiet_seconds,
    clamp_quiet_seconds,
    flush_ready_batches,
    next_poll_delay,
    note_unread_message,
)
from .wechat_reader import WechatMessage, WechatReader


class MessageWatcher(QThread):
    """Poll unread customer chats, debounce, then emit one batch per quiet window.

    After unread appears for a customer, wait ``first_line_quiet_seconds`` (~3s)
    for a lone question, or ``debounce_quiet_seconds`` (6–8s) after a greeting or
    once a second line arrives, before requesting the agent. A new unread line
    during that window restarts the full timer, and polling speeds up while a
    window is open. When the quiet window
    ends, emit every distinct inbound line collected in the window (not only the
    last bubble).
    """

    message_found = Signal(object, str)
    batch_found = Signal(object, str)  # list[WechatMessage], batch_fingerprint
    inbound_noted = Signal(str, float)  # username, time the new line was seen
    status_changed = Signal(str)

    def __init__(self, settings: AppSettings, reader: WechatReader, state: StateStore) -> None:
        super().__init__()
        self.settings = settings
        self.reader = reader
        self.state = state
        self._running = True
        self._pending: dict[str, PendingCustomerBatch] = {}
        self._observed_until: float | None = None

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        self.status_changed.emit("正在读取顾客联系人...")
        try:
            customers = self.reader.refresh_customer_contacts()
            self.status_changed.emit(f"已加载 {len(customers)} 个顾客联系人")
        except Exception as exc:
            self.status_changed.emit(f"顾客联系人读取失败: {exc}")

        while self._running:
            try:
                poll_started = time.time()
                unread = self.reader.fetch_customer_unread()
                merge_triggers = 0
                for msg in unread:
                    if self._note_unread(msg):
                        merge_triggers += 1
                self._observed_until = poll_started
                flushed = self._flush_ready_batches()
                quiet = clamp_quiet_seconds(float(self.settings.debounce_quiet_seconds))
                first_quiet = clamp_first_line_quiet_seconds(
                    float(self.settings.first_line_quiet_seconds),
                    float(self.settings.debounce_quiet_seconds),
                )
                self.status_changed.emit(
                    f"每 {max(1.0, float(self.settings.poll_interval_seconds)):.0f} 秒读取未读；"
                    f"本轮未读 {len(unread)} 条，合并触发 {merge_triggers} 次；"
                    f"单句静默 {first_quiet:.0f}s、连发静默 {quiet:.0f}s 后成批；本轮发出 {flushed} 批"
                )
            except Exception as exc:
                self.status_changed.emit(f"微信消息读取失败: {exc}")
            delay = next_poll_delay(float(self.settings.poll_interval_seconds), has_pending=bool(self._pending))
            wait_until = time.time() + delay
            while self._running and time.time() < wait_until:
                time.sleep(0.15)

    def _note_unread(self, msg: WechatMessage) -> bool:
        fp = message_fingerprint(msg.username, msg.timestamp, msg.last_message)
        noted = note_unread_message(
            self._pending,
            msg=msg,
            fingerprint=fp,
            quiet_seconds=float(self.settings.debounce_quiet_seconds),
            is_processed=self.state.is_processed,
            first_line_quiet_seconds=float(self.settings.first_line_quiet_seconds),
        )
        if noted:
            self.inbound_noted.emit(str(msg.username or ""), time.time())
        return noted

    def _flush_ready_batches(self) -> int:
        def _fp(msg: WechatMessage) -> str:
            return message_fingerprint(msg.username, msg.timestamp, msg.last_message)

        def _history(username: str, limit: int) -> list[WechatMessage]:
            return self.reader.fetch_recent_inbound_messages(username, limit=limit)

        flushed_rows = flush_ready_batches(
            self._pending,
            state=self.state,
            fingerprint_fn=_fp,
            history_loader=_history,
            observed_until=self._observed_until,
        )
        for batch, fingerprints in flushed_rows:
            if len(batch) == 1:
                self.message_found.emit(batch[0], fingerprints[0])
            else:
                self.batch_found.emit(batch, "|".join(fingerprints))
        return len(flushed_rows)

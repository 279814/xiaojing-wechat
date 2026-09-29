from __future__ import annotations

import logging
import sys
from collections import deque
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "app"

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .agent_client import AgentClient, should_auto_send_reply
from .auth_client import AuthClient, AuthResult
from .generate_queue import (
    GenerateReplyJobResult,
    GenerateReplyRequest,
    GenerateReplyWorker,
    SendReplyJobResult,
    SendReplyRequest,
    SendReplyWorker,
    clear_in_flight,
    decide_generate_action,
    decide_send_action,
    is_reply_superseded,
    mark_in_flight,
    merge_batch_messages,
    request_batch,
    store_queued_request,
)
from .message_watcher import MessageWatcher
from .settings import AppSettings
from .state_store import StateStore, StoredUser, message_fingerprint
from .wechat_reader import WechatContact, WechatMessage, WechatReader
from .wechat_cli_manager import WechatCliManager, WechatCliStatus
from .send import WechatSender
from .send.overlay import OverlayController

_LOG = logging.getLogger("autosale.client")


class AuthWorker(QThread):
    """Run AuthClient.login / .me off the Qt GUI thread."""

    finished_result = Signal(object)

    def __init__(
        self,
        auth: AuthClient,
        *,
        mode: str,
        username: str = "",
        password: str = "",
        token: str = "",
    ) -> None:
        super().__init__()
        self.auth = auth
        self.mode = mode
        self.username = username
        self.password = password
        self.token = token

    def run(self) -> None:
        if self.mode == "me":
            self.finished_result.emit(self.auth.me(self.token))
            return
        self.finished_result.emit(self.auth.login(self.username, self.password))


class WechatInitWorker(QThread):
    finished_status = Signal(object)

    def __init__(self, manager: WechatCliManager, force: bool = False) -> None:
        super().__init__()
        self.manager = manager
        self.force = force

    def run(self) -> None:
        self.finished_status.emit(self.manager.initialize(force=self.force))


class WechatStatusWorker(QThread):
    """Run WechatCliManager.status (may scan disk) off the Qt GUI thread."""

    finished_status = Signal(object)

    def __init__(self, manager: WechatCliManager) -> None:
        super().__init__()
        self.manager = manager

    def run(self) -> None:
        self.finished_status.emit(self.manager.status())


class CustomerRefreshWorker(QThread):
    """Run WechatReader.refresh_customer_contacts (sqlite / CLI) off the GUI thread."""

    finished_result = Signal(object, str)  # list[WechatContact] | None, error

    def __init__(self, reader: WechatReader) -> None:
        super().__init__()
        self.reader = reader

    def run(self) -> None:
        try:
            customers = self.reader.refresh_customer_contacts()
            self.finished_result.emit(customers, "")
        except Exception as exc:
            self.finished_result.emit(None, str(exc))


class LoginPage(QWidget):
    def __init__(self, settings: AppSettings, state: StateStore, on_login) -> None:
        super().__init__()
        self.settings = settings
        self.state = state
        self.on_login = on_login
        self.auth = AuthClient(settings)
        self._auth_worker: AuthWorker | None = None

        self.backend_input = QLineEdit(settings.backend_api_base)
        self.agent_input = QLineEdit(settings.agent_chat_url)
        self.auto_send_checkbox = QCheckBox("Agent 生成后自动发送到微信")
        self.auto_send_checkbox.setChecked(settings.auto_send_enabled)
        self.username_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.status_label = QLabel("请使用员工平台账号登录。")
        self.status_label.setWordWrap(True)

        self.login_btn = QPushButton("登录")
        self.login_btn.clicked.connect(self.login)

        layout = QVBoxLayout(self)
        title = QLabel("小鲸 Autosale")
        title.setObjectName("title")
        subtitle = QLabel("登录后才能读取顾客消息并调用销售智能体。")
        subtitle.setObjectName("subtitle")
        form = QFormLayout()
        form.addRow("后端 API", self.backend_input)
        form.addRow("Agent 兜底 API", self.agent_input)
        form.addRow("", self.auto_send_checkbox)
        form.addRow("用户名", self.username_input)
        form.addRow("密码", self.password_input)
        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(form)
        layout.addWidget(self.login_btn)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    def login(self) -> None:
        if self._auth_worker and self._auth_worker.isRunning():
            return
        self.settings.backend_api_base = self.backend_input.text().strip().rstrip("/") or self.settings.backend_api_base
        self.settings.agent_chat_url = self.agent_input.text().strip() or self.settings.agent_chat_url
        self.settings.auto_send_enabled = self.auto_send_checkbox.isChecked()
        self.settings.save()
        self.auth = AuthClient(self.settings)
        self.login_btn.setEnabled(False)
        self.status_label.setText("登录中...")
        worker = AuthWorker(
            self.auth,
            mode="login",
            username=self.username_input.text().strip(),
            password=self.password_input.text(),
        )
        self._auth_worker = worker
        worker.finished_result.connect(self._on_login_finished, Qt.QueuedConnection)
        worker.start()

    def _on_login_finished(self, result: object) -> None:
        self.login_btn.setEnabled(True)
        if not isinstance(result, AuthResult):
            self.status_label.setText("登录失败")
            return
        self.status_label.setText(result.message)
        if result.ok and result.user:
            self.state.save_user(result.user)
            self.on_login(result.user)


class MainPage(QWidget):
    def __init__(self, settings: AppSettings, state: StateStore, user: StoredUser, on_logout) -> None:
        super().__init__()
        self.settings = settings
        self.state = state
        self.user = user
        self.on_logout = on_logout
        self.reader = WechatReader(settings)
        self.wechat_cli = WechatCliManager(settings)
        self.agent = AgentClient(settings)
        self.send_overlay = OverlayController()
        self.sender = WechatSender(settings.wechat_search_delay_ms, overlay=self.send_overlay)
        self.watcher: MessageWatcher | None = None
        self.wechat_init_worker: WechatInitWorker | None = None
        self.wechat_status_worker: WechatStatusWorker | None = None
        self._refresh_status_worker: WechatStatusWorker | None = None
        self.customer_refresh_worker: CustomerRefreshWorker | None = None
        self.contacts: dict[str, WechatContact] = {}
        self.current_message: WechatMessage | None = None
        self.current_batch_messages: list[WechatMessage] = []
        self.current_reply = ""
        self.current_agent_message_id = ""
        self.current_auto_send_claimed = False
        self._suppress_auto_generate = False
        self._page_alive = True
        self._generate_in_flight: set[str] = set()
        self._generate_queued: dict[str, GenerateReplyRequest] = {}
        self._generate_workers: dict[str, GenerateReplyWorker] = {}
        self._send_worker: SendReplyWorker | None = None
        self._send_queue: deque[SendReplyRequest] = deque()
        self._latest_inbound_at: dict[str, float] = {}
        self._last_request_at: dict[str, float] = {}
        self._carry_over: dict[str, list[WechatMessage]] = {}
        self._supersede_count: dict[str, int] = {}
        self._stopping_threads: list[QThread] = []

        self.user_label = QLabel(f"已登录: {user.label}")
        self.status_label = QLabel("已暂停")
        self.status_label.setWordWrap(True)
        self.wechat_status_label = QLabel("微信连接状态检查中...")
        self.wechat_status_label.setWordWrap(True)
        self.customer_list = QListWidget()
        self.message_box = QTextEdit()
        self.message_box.setReadOnly(True)
        self.reply_box = QTextEdit()
        self.reply_box.setPlaceholderText("AI 回复会显示在这里，也可以手动修改后发送。")

        self.mode_group = QButtonGroup(self)
        self.discuss_btn = QRadioButton("交流模式")
        self.assist_btn = QRadioButton("辅助模式")
        self.discuss_btn.setChecked(True)
        self.mode_group.addButton(self.discuss_btn)
        self.mode_group.addButton(self.assist_btn)

        self.refresh_btn = QPushButton("刷新顾客")
        self.init_wechat_btn = QPushButton("初始化微信连接")
        self.reinit_wechat_btn = QPushButton("重置连接")
        self.start_btn = QPushButton("启动监听")
        self.stop_btn = QPushButton("暂停")
        self.generate_btn = QPushButton("生成回复")
        self.copy_btn = QPushButton("复制")
        self.send_btn = QPushButton("发送到微信")
        self.logout_btn = QPushButton("退出登录")

        self.refresh_btn.clicked.connect(self.refresh_customers)
        self.init_wechat_btn.clicked.connect(lambda: self.init_wechat_cli(force=False))
        self.reinit_wechat_btn.clicked.connect(lambda: self.init_wechat_cli(force=True))
        self.start_btn.clicked.connect(self.start_watch)
        self.stop_btn.clicked.connect(self.stop_watch)
        self.generate_btn.clicked.connect(self.generate_reply)
        self.copy_btn.clicked.connect(self.copy_reply)
        self.send_btn.clicked.connect(self.send_reply)
        self.logout_btn.clicked.connect(self.logout)
        self.customer_list.currentItemChanged.connect(self.on_customer_selected)

        root = QHBoxLayout(self)
        sidebar = QVBoxLayout()
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.discuss_btn)
        mode_row.addWidget(self.assist_btn)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        sidebar.addWidget(QLabel("小鲸销售助手"))
        sidebar.addWidget(self.user_label)
        sidebar.addLayout(mode_row)
        sidebar.addLayout(btn_row)
        sidebar.addWidget(self.wechat_status_label)
        wechat_row = QHBoxLayout()
        wechat_row.addWidget(self.init_wechat_btn)
        wechat_row.addWidget(self.reinit_wechat_btn)
        sidebar.addLayout(wechat_row)
        sidebar.addWidget(self.refresh_btn)
        sidebar.addWidget(self.customer_list, 1)
        sidebar.addWidget(self.status_label)
        sidebar.addWidget(self.logout_btn)

        detail = QVBoxLayout()
        detail.addWidget(QLabel("顾客消息"))
        detail.addWidget(self.message_box, 1)
        detail.addWidget(QLabel("AI 建议回复"))
        detail.addWidget(self.reply_box, 2)
        action_row = QHBoxLayout()
        action_row.addWidget(self.generate_btn)
        action_row.addWidget(self.copy_btn)
        action_row.addWidget(self.send_btn)
        detail.addLayout(action_row)

        left = QWidget()
        left.setLayout(sidebar)
        left.setFixedWidth(300)
        right = QWidget()
        right.setLayout(detail)
        root.addWidget(left)
        root.addWidget(right, 1)
        self.check_wechat_status()

    def requested_mode(self) -> str:
        return "assist" if self.assist_btn.isChecked() else "discuss"

    def refresh_customers(self, *, prompt_if_uninitialized: bool = True) -> None:
        """Kick off contact refresh; never block the GUI on wechat-cli / sqlite."""
        if self.customer_refresh_worker and self.customer_refresh_worker.isRunning():
            self.status_label.setText("正在刷新顾客，请稍候...")
            return
        if self._refresh_status_worker and self._refresh_status_worker.isRunning():
            self.status_label.setText("正在检查微信连接...")
            return
        self.status_label.setText("正在检查微信连接...")
        self.refresh_btn.setEnabled(False)
        worker = WechatStatusWorker(self.wechat_cli)
        self._refresh_status_worker = worker
        worker.finished_status.connect(
            lambda status, prompt=prompt_if_uninitialized: self._on_refresh_status_ready(status, prompt),
            Qt.QueuedConnection,
        )
        worker.start()

    def _on_refresh_status_ready(self, status: object, prompt_if_uninitialized: bool) -> None:
        if not self._page_alive or not isinstance(status, WechatCliStatus):
            self.refresh_btn.setEnabled(True)
            return
        self.wechat_status_label.setText(status.message)
        self.init_wechat_btn.setEnabled(status.available)
        self.reinit_wechat_btn.setEnabled(status.available)
        if not status.initialized:
            self.refresh_btn.setEnabled(True)
            if prompt_if_uninitialized:
                QMessageBox.information(self, "微信连接未初始化", status.message)
            return
        self._start_customer_refresh()

    def _start_customer_refresh(self) -> None:
        if self.customer_refresh_worker and self.customer_refresh_worker.isRunning():
            self.status_label.setText("正在刷新顾客，请稍候...")
            return
        self.status_label.setText("正在刷新顾客...")
        self.refresh_btn.setEnabled(False)
        worker = CustomerRefreshWorker(self.reader)
        self.customer_refresh_worker = worker
        worker.finished_result.connect(self.on_customer_refresh_finished, Qt.QueuedConnection)
        worker.start()

    def on_customer_refresh_finished(self, customers: object, error: str) -> None:
        if not self._page_alive:
            return
        self.refresh_btn.setEnabled(True)
        if error:
            self.status_label.setText(f"顾客读取失败: {error}")
            return
        rows = [item for item in list(customers or []) if isinstance(item, WechatContact)]
        self.contacts = {item.username: item for item in rows}
        self.customer_list.clear()
        for item in rows:
            row = QListWidgetItem(item.display_name)
            row.setData(Qt.UserRole, item.username)
            self.customer_list.addItem(row)
        self.status_label.setText(
            f"已加载 {len(rows)} 个备注含“{self.settings.customer_remark_keyword}”的顾客"
        )

    def check_wechat_status(self) -> None:
        if self.wechat_status_worker and self.wechat_status_worker.isRunning():
            return
        self.wechat_status_label.setText("微信连接状态检查中...")
        worker = WechatStatusWorker(self.wechat_cli)
        self.wechat_status_worker = worker
        worker.finished_status.connect(self.on_wechat_status_finished, Qt.QueuedConnection)
        worker.start()

    def on_wechat_status_finished(self, status: object) -> None:
        if not self._page_alive or not isinstance(status, WechatCliStatus):
            return
        self.wechat_status_label.setText(status.message)
        self.init_wechat_btn.setEnabled(status.available)
        self.reinit_wechat_btn.setEnabled(status.available)
        if status.initialized:
            self._start_customer_refresh()

    def init_wechat_cli(self, force: bool = False) -> None:
        if self.wechat_init_worker and self.wechat_init_worker.isRunning():
            return
        self.wechat_status_label.setText("正在初始化微信连接，请保持微信已登录...")
        self.init_wechat_btn.setEnabled(False)
        self.reinit_wechat_btn.setEnabled(False)
        self.wechat_init_worker = WechatInitWorker(self.wechat_cli, force=force)
        self.wechat_init_worker.finished_status.connect(self.on_wechat_init_finished, Qt.QueuedConnection)
        self.wechat_init_worker.start()

    def on_wechat_init_finished(self, status) -> None:
        if not self._page_alive:
            return
        self.wechat_status_label.setText(status.message)
        self.init_wechat_btn.setEnabled(status.available)
        self.reinit_wechat_btn.setEnabled(status.available)
        if status.initialized:
            self._start_customer_refresh()
        else:
            QMessageBox.warning(self, "微信连接失败", status.message)

    def start_watch(self) -> None:
        if self.watcher and self.watcher.isRunning():
            return
        self.watcher = MessageWatcher(self.settings, self.reader, self.state)
        self.watcher.inbound_noted.connect(self.on_inbound_noted)
        self.watcher.message_found.connect(self.on_message_found)
        self.watcher.batch_found.connect(self.on_batch_found)
        self.watcher.status_changed.connect(self.status_label.setText)
        self.watcher.start()
        self.status_label.setText("监听已启动")

    def stop_watch(self) -> None:
        if self.watcher:
            watcher = self.watcher
            self.watcher = None
            watcher.stop()
            self._retain_thread_until_finished(watcher)
        self.status_label.setText("已暂停")

    def _retain_thread_until_finished(self, thread: QThread) -> None:
        """Keep a QThread alive without blocking the GUI on wait()."""
        if thread in self._stopping_threads:
            return
        self._stopping_threads.append(thread)

        def _cleanup(t: QThread = thread) -> None:
            if t in self._stopping_threads:
                self._stopping_threads.remove(t)

        thread.finished.connect(_cleanup)

    def on_inbound_noted(self, username: str, noted_at: float) -> None:
        key = str(username or "").strip()
        if key:
            self._latest_inbound_at[key] = max(float(noted_at), self._latest_inbound_at.get(key, 0.0))

    def _reply_is_stale(self, username: str, generated_for_at: float | None, *, has_newer_request: bool) -> bool:
        watching = self.watcher is not None and self.watcher.isRunning()
        return is_reply_superseded(
            generated_for_at,
            self._latest_inbound_at.get(username) if watching else None,
            has_newer_request=has_newer_request,
            consecutive_supersedes=self._supersede_count.get(username, 0),
        )

    def _carry_forward(self, username: str, batch: list[WechatMessage]) -> None:
        """Keep a dropped reply's customer lines so the next request answers them too."""
        queued = self._generate_queued.get(username)
        if queued is not None:
            queued.batch_messages = merge_batch_messages(batch, request_batch(queued))
        else:
            self._carry_over[username] = merge_batch_messages(self._carry_over.get(username, []), batch)
        self._supersede_count[username] = self._supersede_count.get(username, 0) + 1

    def on_batch_found(self, messages: object, _batch_fingerprint: str) -> None:
        batch = [item for item in list(messages or []) if isinstance(item, WechatMessage)]
        if not batch:
            return
        primary = batch[-1]
        self._suppress_auto_generate = True
        try:
            self.on_message_found(primary, message_fingerprint(primary.username, primary.timestamp, primary.last_message))
        finally:
            self._suppress_auto_generate = False
        lines = "\n".join(f"{index}. {item.last_message}" for index, item in enumerate(batch, start=1))
        contact = self.contacts.get(primary.username)
        self.message_box.setPlainText(
            f"顾客: {(contact.display_name if contact else primary.chat)}\n"
            f"微信ID: {primary.username}\n"
            f"本轮合并 {len(batch)} 条未读:\n\n"
            f"{lines}"
        )
        self.current_batch_messages = batch
        if self.settings.auto_send_enabled:
            self.generate_reply()

    def on_message_found(self, msg: WechatMessage, _fingerprint: str) -> None:
        self.current_batch_messages = [msg]
        contact = self.contacts.get(msg.username)
        display = contact.display_name if contact else msg.chat
        matches = self.customer_list.findItems(display, Qt.MatchExactly)
        if not matches:
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, msg.username)
            self.customer_list.addItem(item)
            matches = [item]
        matches[0].setText(f"● {display}")
        matches[0].setData(Qt.UserRole + 1, msg)
        self.customer_list.setCurrentItem(matches[0])
        if self.customer_list.currentItem() is matches[0]:
            self.on_customer_selected(matches[0], None)
        self.status_label.setText(f"收到顾客消息: {display}")
        if self.settings.auto_send_enabled and not self._suppress_auto_generate:
            self.generate_reply()

    def on_customer_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if not current:
            return
        msg = current.data(Qt.UserRole + 1)
        username = str(current.data(Qt.UserRole) or "")
        contact = self.contacts.get(username)
        if isinstance(msg, WechatMessage):
            self.current_message = msg
            self.current_reply = ""
            self.current_agent_message_id = ""
            self.current_auto_send_claimed = False
            self.reply_box.clear()
            self.message_box.setPlainText(
                f"顾客: {(contact.display_name if contact else msg.chat)}\n"
                f"微信ID: {msg.username}\n"
                f"时间: {msg.time_text or msg.timestamp}\n\n"
                f"{msg.last_message}"
            )
            self._sync_generate_button()
            return
        if contact:
            self.current_message = None
            self.current_reply = ""
            self.current_agent_message_id = ""
            self.current_auto_send_claimed = False
            self.reply_box.clear()
            self.message_box.setPlainText(f"顾客: {contact.display_name}\n微信ID: {contact.username}\n\n暂无未读消息。")
            self._sync_generate_button()

    def generate_reply(self) -> None:
        if not self.current_message:
            QMessageBox.information(self, "提示", "请先选择一条顾客消息。")
            return
        contact = self.contacts.get(self.current_message.username)
        username = str(self.current_message.username or "")
        batch = merge_batch_messages(
            self._carry_over.pop(username, []),
            list(self.current_batch_messages or [self.current_message]),
        )
        request = GenerateReplyRequest(
            username=username,
            user=self.user,
            contact=contact,
            message=batch[-1] if batch else self.current_message,
            batch_messages=batch,
            requested_mode=self.requested_mode(),
        )
        self._enqueue_generate(request)

    def _enqueue_generate(self, request: GenerateReplyRequest) -> None:
        username = str(request.username or "").strip()
        if not username:
            return
        self._last_request_at[username] = max(request.created_at, self._last_request_at.get(username, 0.0))
        action = decide_generate_action(username, self._generate_in_flight)
        if action == "queue":
            store_queued_request(self._generate_queued, request)
            self.status_label.setText("正在生成回复...（同顾客请求已排队）")
            self._sync_generate_button()
            return
        self._start_generate_worker(request)

    def _start_generate_worker(self, request: GenerateReplyRequest) -> None:
        username = str(request.username or "").strip()
        if not username:
            return
        mark_in_flight(self._generate_in_flight, username)
        self.status_label.setText("正在生成回复...")
        self._sync_generate_button()
        worker = GenerateReplyWorker(self.agent, request)
        self._generate_workers[username] = worker
        worker.finished_job.connect(self.on_generate_finished, Qt.QueuedConnection)
        worker.finished.connect(
            lambda u=username, w=worker: self._on_generate_thread_finished(u, w),
            Qt.QueuedConnection,
        )
        worker.start()

    def _on_generate_thread_finished(self, username: str, worker: GenerateReplyWorker) -> None:
        current = self._generate_workers.get(username)
        if current is worker:
            self._generate_workers.pop(username, None)

    def on_generate_finished(self, job: object) -> None:
        if not self._page_alive or not isinstance(job, GenerateReplyJobResult):
            return
        request = job.request
        result = job.result
        username = str(request.username or "").strip()
        clear_in_flight(self._generate_in_flight, username)
        viewing = bool(
            self.current_message
            and str(self.current_message.username or "") == username
        )
        if viewing:
            self.status_label.setText(result.message)
        else:
            label = request.contact.display_name if request.contact else request.message.chat
            self.status_label.setText(f"{label}: {result.message}")
        if result.ok:
            if viewing:
                self.current_reply = result.reply
                self.current_agent_message_id = result.message_id
                self.current_auto_send_claimed = False
                self.reply_box.setPlainText(result.reply)
            # Packaged client: local auto-send checkbox controls paste/send.
            # Backend can_auto_send is logged but does not block the send action.
            backend_can_auto_send = bool(result.can_auto_send)
            auto_send = should_auto_send_reply(
                settings=self.settings,
                can_auto_send=backend_can_auto_send,
                reply=result.reply,
            )
            if auto_send and self._reply_is_stale(
                username,
                request.created_at,
                has_newer_request=username in self._generate_queued,
            ):
                self._carry_forward(username, request_batch(request))
                _LOG.info(
                    "reply_superseded reason=newer_customer_message message_id=%s lines=%d",
                    result.message_id,
                    len(request_batch(request)),
                )
                self.status_label.setText("顾客又发了新消息，旧回复未发送，将合并后重新生成")
            elif auto_send:
                self._supersede_count.pop(username, None)
                _LOG.info(
                    "reply_auto_send_allowed control=local_checkbox "
                    "backend_can_auto_send=%s message_id=%s",
                    backend_can_auto_send,
                    result.message_id,
                )
                self._enqueue_send(
                    SendReplyRequest(
                        message=request.message,
                        contact=request.contact,
                        text=result.reply,
                        agent_message_id=result.message_id,
                        auto_send_claimed=False,
                        auto_send_enabled=bool(self.settings.auto_send_enabled),
                        employee_username=self.user.username,
                        update_current_claim=viewing,
                        source_batch=request_batch(request),
                        generated_for_at=request.created_at,
                    )
                )
            elif result.reply.strip() and not self.settings.auto_send_enabled:
                _LOG.info(
                    "reply_auto_send_blocked reason=local_auto_send_off "
                    "backend_can_auto_send=%s message_id=%s",
                    backend_can_auto_send,
                    result.message_id,
                )
                self.status_label.setText(
                    result.message or "已生成回复，本地未开启自动发送"
                )
        elif viewing:
            QMessageBox.warning(self, "生成失败", result.message)
        self._sync_generate_button()
        queued = self._generate_queued.pop(username, None)
        if queued is not None:
            self._start_generate_worker(queued)

    def _sync_generate_button(self) -> None:
        current_name = str(self.current_message.username or "") if self.current_message else ""
        busy = bool(current_name and current_name in self._generate_in_flight)
        self.generate_btn.setEnabled(not busy)
        send_busy = self._send_worker is not None and self._send_worker.isRunning()
        self.send_btn.setEnabled(not send_busy)

    def copy_reply(self) -> None:
        text = self.reply_box.toPlainText().strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.status_label.setText("已复制回复")

    def send_reply(self) -> None:
        if not self.current_message:
            QMessageBox.information(self, "提示", "请先选择一条顾客消息。")
            return
        text = self.reply_box.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "提示", "回复内容为空。")
            return
        contact = self.contacts.get(self.current_message.username)
        self._enqueue_send(
            SendReplyRequest(
                message=self.current_message,
                contact=contact,
                text=text,
                agent_message_id=self.current_agent_message_id,
                auto_send_claimed=self.current_auto_send_claimed,
                auto_send_enabled=bool(self.settings.auto_send_enabled),
                employee_username=self.user.username,
                update_current_claim=True,
            )
        )

    def _enqueue_send(self, request: SendReplyRequest) -> None:
        busy = self._send_worker is not None and self._send_worker.isRunning()
        if decide_send_action(busy) == "queue":
            self._send_queue.append(request)
            self.status_label.setText("正在发送到微信...（已排队）")
            self._sync_generate_button()
            return
        self._start_send_worker(request)

    def _start_next_queued_send(self) -> None:
        while self._send_queue:
            request = self._send_queue.popleft()
            username = str(request.message.username or "").strip()
            # Only drop while the newer line is still waiting in the watcher: a
            # request that already started cannot absorb these lines.
            newest_request_at = max(
                float(request.generated_for_at or 0.0),
                self._last_request_at.get(username, 0.0),
            )
            if (
                request.source_batch
                and username not in self._generate_in_flight
                and self._reply_is_stale(username, newest_request_at, has_newer_request=False)
            ):
                self._carry_forward(username, list(request.source_batch))
                _LOG.info("queued_reply_superseded reason=newer_customer_message message_id=%s", request.agent_message_id)
                continue
            self._start_send_worker(request)
            return

    def _start_send_worker(self, request: SendReplyRequest) -> None:
        self.status_label.setText("正在发送到微信...")
        self._sync_generate_button()
        worker = SendReplyWorker(self.agent, self.sender, request)
        self._send_worker = worker
        worker.finished_job.connect(self.on_send_finished, Qt.QueuedConnection)
        worker.finished.connect(
            lambda w=worker: self._on_send_thread_finished(w),
            Qt.QueuedConnection,
        )
        worker.start()

    def _on_send_thread_finished(self, worker: SendReplyWorker) -> None:
        if self._send_worker is worker:
            self._send_worker = None
        self._sync_generate_button()

    def on_send_finished(self, job: object) -> None:
        if not self._page_alive or not isinstance(job, SendReplyJobResult):
            return
        req = job.request
        if req.update_current_claim and job.claimed:
            self.current_auto_send_claimed = True
        self.status_label.setText(job.message)
        if not job.ok:
            QMessageBox.warning(self, "发送失败", job.error or job.message)
        self._sync_generate_button()
        self._start_next_queued_send()

    def logout(self) -> None:
        self._page_alive = False
        self.stop_watch()
        for worker in list(self._generate_workers.values()):
            self._retain_thread_until_finished(worker)
        self._generate_workers.clear()
        self._generate_in_flight.clear()
        self._generate_queued.clear()
        if self._send_worker is not None:
            self._retain_thread_until_finished(self._send_worker)
            self._send_worker = None
        self._send_queue.clear()
        self._carry_over.clear()
        self._latest_inbound_at.clear()
        self._last_request_at.clear()
        self._supersede_count.clear()
        for worker in (
            self.wechat_init_worker,
            self.wechat_status_worker,
            self._refresh_status_worker,
            self.customer_refresh_worker,
        ):
            if worker is not None and worker.isRunning():
                self._retain_thread_until_finished(worker)
        self.wechat_init_worker = None
        self.wechat_status_worker = None
        self._refresh_status_worker = None
        self.customer_refresh_worker = None
        self.state.clear_user()
        self.on_logout()


class AutosaleApp(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings.load()
        self.state = StateStore()
        self.stack = QStackedWidget()
        self._restore_worker: AuthWorker | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(self.stack)
        self.setWindowTitle("小鲸 Autosale")
        self.resize(980, 680)
        self.show_login()
        self.try_restore_login()

    def show_login(self) -> None:
        self.clear_stack()
        self.stack.addWidget(LoginPage(self.settings, self.state, self.show_main))

    def show_main(self, user: StoredUser) -> None:
        self.clear_stack()
        self.stack.addWidget(MainPage(self.settings, self.state, user, self.show_login))

    def clear_stack(self) -> None:
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()

    def try_restore_login(self) -> None:
        user = self.state.load_user()
        if not user.token:
            return
        if self._restore_worker and self._restore_worker.isRunning():
            return
        worker = AuthWorker(AuthClient(self.settings), mode="me", token=user.token)
        self._restore_worker = worker
        worker.finished_result.connect(self._on_restore_login_finished, Qt.QueuedConnection)
        worker.start()

    def _on_restore_login_finished(self, result: object) -> None:
        if not isinstance(result, AuthResult):
            self.state.clear_user()
            return
        if result.ok and result.user:
            self.state.save_user(result.user)
            self.show_main(result.user)
        else:
            self.state.clear_user()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(
        """
        QWidget { font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; font-size: 13px; }
        #title { font-size: 28px; font-weight: 700; }
        #subtitle { color: #64748b; margin-bottom: 12px; }
        QPushButton { padding: 8px 12px; }
        QListWidget { border: 1px solid #d7dde8; border-radius: 6px; }
        QTextEdit, QLineEdit { border: 1px solid #d7dde8; border-radius: 6px; padding: 6px; }
        """
    )
    win = AutosaleApp()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

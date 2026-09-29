"""Visual takeover shown while the sender drives WeChat.

A click-through, non-activating, always-on-top window draws a magenta frame
around the WeChat window and a fake cursor that glides to each target before
the real click. It never takes focus, so WeChat stays the foreground window.

``OverlayController`` must be created on the Qt GUI thread; its methods may be
called from the send worker thread (they only emit queued signals).
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRect, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .layout import Point, Rect, native_to_logical

ACCENT = QColor(255, 45, 149)
BRAND = "小鲸正在操作微信"


class TakeoverOverlay(QWidget):
    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self._frame = QRectF()
        self._cursor = QPointF()
        self._cursor_visible = True
        self._pulse = 0.0
        self._status = ""
        self._move_anim = QVariantAnimation(self)
        self._move_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._move_anim.valueChanged.connect(self._on_cursor_step)
        self._pulse_anim = QVariantAnimation(self)
        self._pulse_anim.setStartValue(0.0)
        self._pulse_anim.setEndValue(1.0)
        self._pulse_anim.setDuration(420)
        self._pulse_anim.valueChanged.connect(self._on_pulse_step)
        self._pulse_anim.finished.connect(self._on_pulse_done)

    def begin(self, frame_global: QRectF, screen_geometry: QRect) -> None:
        self.setGeometry(screen_geometry)
        self._frame = frame_global.translated(-screen_geometry.x(), -screen_geometry.y())
        self._cursor = QPointF(QCursor.pos() - screen_geometry.topLeft())
        self._cursor_visible = True
        self._pulse = 0.0
        self.show()
        self.raise_()
        self.update()

    def move_cursor(self, target_global: QPointF, duration_ms: int) -> None:
        target = target_global - QPointF(self.geometry().topLeft())
        self._move_anim.stop()
        self._move_anim.setStartValue(QPointF(self._cursor))
        self._move_anim.setEndValue(target)
        self._move_anim.setDuration(max(1, duration_ms))
        self._move_anim.start()

    def pulse(self) -> None:
        self._pulse_anim.stop()
        self._pulse_anim.start()

    def set_cursor_visible(self, visible: bool) -> None:
        self._cursor_visible = visible
        self.update()

    def set_status(self, text: str) -> None:
        self._status = text
        self.update()

    def end(self) -> None:
        self._move_anim.stop()
        self._pulse_anim.stop()
        self.hide()

    def _on_cursor_step(self, value) -> None:
        self._cursor = QPointF(value)
        self.update()

    def _on_pulse_step(self, value) -> None:
        self._pulse = float(value)
        self.update()

    def _on_pulse_done(self) -> None:
        self._pulse = 0.0
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_frame(painter)
        self._paint_status(painter)
        if self._cursor_visible:
            self._paint_cursor(painter)
        painter.end()

    def _paint_frame(self, painter: QPainter) -> None:
        if self._frame.isEmpty():
            return
        for width, alpha in ((14, 40), (8, 90)):
            glow = QColor(ACCENT)
            glow.setAlpha(alpha)
            painter.setPen(QPen(glow, width))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(self._frame.adjusted(2, 2, -2, -2), 10, 10)
        painter.setPen(QPen(ACCENT, 4))
        painter.drawRoundedRect(self._frame.adjusted(2, 2, -2, -2), 10, 10)

    def _paint_status(self, painter: QPainter) -> None:
        if self._frame.isEmpty():
            return
        text = f"{BRAND} · {self._status}" if self._status else BRAND
        font = QFont(self.font())
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        pill_w = metrics.horizontalAdvance(text) + 28
        pill_h = metrics.height() + 12
        # Above the frame when there is room, otherwise over the bottom of the
        # contact column, away from the chat title, input box and 发送 button.
        if self._frame.top() - pill_h - 8 >= 0:
            pill = QRectF(self._frame.center().x() - pill_w / 2, self._frame.top() - pill_h - 8, pill_w, pill_h)
        else:
            pill = QRectF(self._frame.left() + 12, self._frame.bottom() - pill_h - 12, pill_w, pill_h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(ACCENT)
        painter.drawRoundedRect(pill, pill_h / 2, pill_h / 2)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(pill, Qt.AlignCenter, text)

    def _paint_cursor(self, painter: QPainter) -> None:
        tip = self._cursor
        if self._pulse > 0:
            ring = QColor(ACCENT)
            ring.setAlphaF(max(0.0, 1.0 - self._pulse))
            painter.setPen(QPen(ring, 3))
            painter.setBrush(Qt.NoBrush)
            radius = 6 + 22 * self._pulse
            painter.drawEllipse(tip, radius, radius)
        shape = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10, 19), (7, 12.5), (12.5, 12.5)]
        polygon = QPolygonF([QPointF(tip.x() + x * 1.4, tip.y() + y * 1.4) for x, y in shape])
        path = QPainterPath()
        path.addPolygon(polygon)
        path.closeSubpath()
        painter.setPen(QPen(QColor(30, 30, 30), 1.5))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(ACCENT)
        painter.drawEllipse(tip + QPointF(2.5, 6), 2.2, 2.2)


class OverlayController(QObject):
    """Thread-safe front for TakeoverOverlay; implements sequence.Overlay."""

    _begin = Signal(object, bool)
    _move = Signal(object, int)
    _pulse = Signal()
    _cursor_visible = Signal(bool)
    _status = Signal(str)
    _end = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._widget: TakeoverOverlay | None = None
        self._physical = True
        self._begin.connect(self._on_begin, Qt.QueuedConnection)
        self._move.connect(self._on_move, Qt.QueuedConnection)
        self._pulse.connect(self._on_pulse, Qt.QueuedConnection)
        self._cursor_visible.connect(self._on_cursor_visible, Qt.QueuedConnection)
        self._status.connect(self._on_status, Qt.QueuedConnection)
        self._end.connect(self._on_end, Qt.QueuedConnection)

    def begin(self, rect: Rect, physical: bool) -> None:
        self._begin.emit(rect, physical)

    def move_cursor(self, point: Point, duration_ms: int) -> None:
        self._move.emit(point, int(duration_ms))

    def pulse(self) -> None:
        self._pulse.emit()

    def set_cursor_visible(self, visible: bool) -> None:
        self._cursor_visible.emit(bool(visible))

    def set_status(self, text: str) -> None:
        self._status.emit(str(text))

    def end(self) -> None:
        self._end.emit()

    def _overlay(self) -> TakeoverOverlay:
        if self._widget is None:
            self._widget = TakeoverOverlay()
        return self._widget

    def _on_begin(self, rect: Rect, physical: bool) -> None:
        self._physical = physical
        center = Point(rect.left + rect.width // 2, rect.top + rect.height // 2)
        screen_geometry, _ = self._screen_for(center)
        top_left = self._to_logical(Point(rect.left, rect.top))
        bottom_right = self._to_logical(Point(rect.right, rect.bottom))
        self._overlay().begin(QRectF(top_left, bottom_right), screen_geometry)

    def _on_move(self, point: Point, duration_ms: int) -> None:
        self._overlay().move_cursor(self._to_logical(point), duration_ms)

    def _on_pulse(self) -> None:
        self._overlay().pulse()

    def _on_cursor_visible(self, visible: bool) -> None:
        self._overlay().set_cursor_visible(visible)

    def _on_status(self, text: str) -> None:
        self._overlay().set_status(text)

    def _on_end(self) -> None:
        if self._widget is not None:
            self._widget.end()

    def _screen_for(self, point: Point) -> tuple[QRect, float]:
        screens = QGuiApplication.screens()
        for screen in screens:
            geometry = screen.geometry()
            dpr = screen.devicePixelRatio() if self._physical else 1.0
            native = QRect(geometry.x(), geometry.y(), round(geometry.width() * dpr), round(geometry.height() * dpr))
            if native.contains(point.x, point.y):
                return geometry, dpr
        primary = QGuiApplication.primaryScreen()
        return primary.geometry(), (primary.devicePixelRatio() if self._physical else 1.0)

    def _to_logical(self, point: Point) -> QPointF:
        geometry, dpr = self._screen_for(point)
        return QPointF(
            native_to_logical(point.x, geometry.x(), dpr),
            native_to_logical(point.y, geometry.y(), dpr),
        )

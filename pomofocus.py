#!/usr/bin/env python3
"""
Tomato — wheel timer (PySide6).
Scroll wheel to set time (1-59 min). Click = start/pause. Hold 1s = stop.
Textured wheel with pointer. Start point at bottom, clockwise.
"""

import sys
import os
import math

from PySide6.QtCore import Qt, QTimer, QElapsedTimer, QPointF, QRectF
from PySide6.QtGui import (
    QPainter, QPen, QColor, QFont, QFontMetrics,
    QPixmap, QIcon, QRadialGradient, QPainterPath,
)
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtMultimedia import QSoundEffect, QMediaPlayer, QAudioOutput
from PySide6.QtCore import QUrl


# --- Palette ---
PAL_RED_LIGHT = QColor("#c4605a")
PAL_RED_DARK  = QColor("#b5443c")
PAL_SAGE      = QColor("#9aab8a")
PAL_GREEN     = QColor("#7aa55e")
PAL_GREEN_DK  = QColor("#618a5a")

BG            = QColor("#1a1a1a")
WORK_COLOR    = PAL_RED_DARK
REST_COLOR    = PAL_GREEN
TEXT_COLOR    = QColor("#f0ece4")
TRACK_COLOR   = QColor("#2e2e2e")
HINT_COLOR    = PAL_SAGE

STEP_SEC = 15
MIN_TIME = 15       # 15 sec
MAX_TIME = 59 * 60  # 59 min
LONG_PRESS_MS = 1000
SCROLL_SFX_INTERVAL_MS = 45
WORK_TICK_INTERVAL_MS = 1000

_HERE = os.path.dirname(os.path.abspath(__file__))
TICK_WAV = os.path.join(_HERE, "clock_ticking.wav")
RING_WAV = os.path.join(_HERE, "clock_ring.wav")
SCROLL_WAV = os.path.join(_HERE, "scroll_tick.wav")


# --- Icon ---

def _make_tomato_icon(size=128):
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    cx, cy = size // 2, int(size * 0.55)
    rx, ry = int(size * 0.42), int(size * 0.38)
    grad = QRadialGradient(cx - rx * 0.3, cy - ry * 0.3, rx * 1.6)
    grad.setColorAt(0.0, PAL_RED_LIGHT)
    grad.setColorAt(0.6, PAL_RED_DARK)
    grad.setColorAt(1.0, QColor("#8a2a25"))
    p.setBrush(grad)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(cx - rx, cy - ry, rx * 2, ry * 2)
    stem_x = cx
    stem_top = cy - ry - int(size * 0.08)
    p.setPen(QPen(PAL_GREEN_DK, size * 0.04, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(stem_x, cy - ry + 4, stem_x, stem_top)
    leaf = QPainterPath()
    leaf.moveTo(stem_x, stem_top + 4)
    leaf.cubicTo(stem_x + size * 0.18, stem_top - size * 0.06,
                 stem_x + size * 0.22, stem_top + size * 0.08,
                 stem_x + size * 0.06, stem_top + size * 0.10)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(PAL_GREEN)
    p.drawPath(leaf)
    leaf2 = QPainterPath()
    leaf2.moveTo(stem_x, stem_top + 4)
    leaf2.cubicTo(stem_x - size * 0.12, stem_top - size * 0.04,
                  stem_x - size * 0.14, stem_top + size * 0.06,
                  stem_x - size * 0.03, stem_top + size * 0.08)
    p.setBrush(PAL_GREEN_DK)
    p.drawPath(leaf2)
    p.end()
    return QIcon(pix)


# --- Main widget ---

class WheelTimer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("tomato")
        self.setFixedSize(320, 320)
        self.setMouseTracking(True)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag_pos = None
        self._dragging = False

        self.is_work = True
        self.total_sec = 25 * 60
        self.remaining_sec = self.total_sec
        self.running = False
        self.paused = False

        # Long-press: fires after 1s even without release
        self._press_timer = QElapsedTimer()
        self._long_press_check = QTimer(self)
        self._long_press_check.setSingleShot(True)
        self._long_press_check.setInterval(LONG_PRESS_MS)
        self._long_press_check.timeout.connect(self._on_long_press)
        self._long_press_fired = False

        # Scroll accumulator for smooth trackpad/wheel
        self._scroll_accum = 0

        # Countdown
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        # Cache wheel texture
        self._wheel_cache = None

        # --- Audio ---
        # Scroll feedback (short one-shot)
        self._scroll_snd = QSoundEffect(self)
        self._scroll_snd.setSource(QUrl.fromLocalFile(SCROLL_WAV))
        self._scroll_snd.setVolume(0.075)
        self._last_scroll_sfx = QElapsedTimer()

        # Continuous ticking during WORK (metronome-style timer)
        self._work_tick_snd = QSoundEffect(self)
        self._work_tick_snd.setSource(QUrl.fromLocalFile(SCROLL_WAV))
        self._work_tick_snd.setVolume(0.09)
        self._work_tick_timer = QTimer(self)
        self._work_tick_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._work_tick_timer.setInterval(WORK_TICK_INTERVAL_MS)
        self._work_tick_timer.timeout.connect(self._on_work_tick)

        # Ring/bell (looped until user clicks)
        self._ring_output = QAudioOutput(self)
        self._ring_output.setVolume(0.12)
        self._ring_player = QMediaPlayer(self)
        self._ring_player.setAudioOutput(self._ring_output)
        self._ring_player.setSource(QUrl.fromLocalFile(RING_WAV))

        self.setWindowIcon(_make_tomato_icon())

    def _play_scroll_feedback(self):
        """Play immediate scroll feedback with a soft max rate."""
        if self._last_scroll_sfx.isValid() and self._last_scroll_sfx.elapsed() < SCROLL_SFX_INTERVAL_MS:
            return
        if self._scroll_snd.isPlaying():
            self._scroll_snd.stop()
        self._scroll_snd.play()
        if self._last_scroll_sfx.isValid():
            self._last_scroll_sfx.restart()
        else:
            self._last_scroll_sfx.start()

    def _on_work_tick(self):
        if not self.running or not self.is_work:
            self._work_tick_timer.stop()
            return
        if self._work_tick_snd.isPlaying():
            self._work_tick_snd.stop()
        self._work_tick_snd.play()

    # --- Paint ---

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Transparent background — draw dark circle behind wheel
        cx, cy = self.width() // 2, self.height() // 2
        radius = 140
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(BG)
        p.drawEllipse(QPointF(cx, cy), radius + 2, radius + 2)
        color = WORK_COLOR if self.is_work else REST_COLOR

        # Draw cached wheel
        if self._wheel_cache is None:
            self._wheel_cache = QPixmap(self.size())
            self._wheel_cache.fill(Qt.GlobalColor.transparent)
            wp = QPainter(self._wheel_cache)
            wp.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._draw_wheel(wp, cx, cy, radius)
            wp.end()
        p.drawPixmap(0, 0, self._wheel_cache)

        self._draw_pointer(p, cx, cy, radius, color)
        self._draw_progress(p, cx, cy, radius - 32, color)

        # Time text
        mins = self.remaining_sec // 60
        secs = self.remaining_sec % 60
        time_str = f"{mins:02d}:{secs:02d}"
        font = QFont("Menlo", 38, QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(TEXT_COLOR)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(time_str)
        th = fm.height()
        p.drawText(cx - tw // 2, cy + th // 4, time_str)

        # Label
        if self.paused:
            label = "PAUSED"
        else:
            label = "WORK" if self.is_work else "REST"
        small_font = QFont("Helvetica Neue", 11)
        p.setFont(small_font)
        p.setPen(HINT_COLOR)
        sfm = QFontMetrics(small_font)
        lw = sfm.horizontalAdvance(label)
        p.drawText(cx - lw // 2, cy + th // 4 + 28, label)

        # Hint
        if not self.running and not self.paused:
            hint = "scroll · click · hold"
        elif self.paused:
            hint = "click · hold"
        else:
            hint = "click · hold"
        hint_font = QFont("Helvetica Neue", 9)
        p.setFont(hint_font)
        p.setPen(QColor("#555555"))
        hfm = QFontMetrics(hint_font)
        hw = hfm.horizontalAdvance(hint)
        p.drawText(cx - hw // 2, cy + th // 4 + 46, hint)

        p.end()

    def _draw_wheel(self, p, cx, cy, radius):
        """Textured knurled ring with thin ridges."""
        ring_w = 22
        outer_r = radius
        inner_r = radius - ring_w

        # Base ring
        ring_rect = QRectF(cx - outer_r, cy - outer_r, outer_r * 2, outer_r * 2)
        grad = QRadialGradient(cx, cy, outer_r)
        grad.setColorAt(0.0, QColor("#484848"))
        grad.setColorAt(0.75, QColor("#3a3a3a"))
        grad.setColorAt(0.9, QColor("#2a2a2a"))
        grad.setColorAt(1.0, QColor("#222222"))
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(ring_rect)

        # Inner cutout
        inner_rect = QRectF(cx - inner_r, cy - inner_r, inner_r * 2, inner_r * 2)
        p.setBrush(BG)
        p.drawEllipse(inner_rect)

        # Thin ridges
        num_ridges = 72
        for i in range(num_ridges):
            angle = (2 * math.pi * i) / num_ridges
            x1 = cx + math.cos(angle) * (inner_r + 3)
            y1 = cy + math.sin(angle) * (inner_r + 3)
            x2 = cx + math.cos(angle) * (outer_r - 3)
            y2 = cy + math.sin(angle) * (outer_r - 3)

            if i % 2 == 0:
                c = QColor(255, 255, 255, 18)
            else:
                c = QColor(0, 0, 0, 30)
            p.setPen(QPen(c, 1.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # Outer bevel highlight
        p.setPen(QPen(QColor(255, 255, 255, 15), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ring_rect.adjusted(1, 1, -1, -1))

        # Inner shadow
        p.setPen(QPen(QColor(0, 0, 0, 40), 1.0))
        p.drawEllipse(inner_rect.adjusted(-1, -1, 1, 1))

    def _draw_pointer(self, p, cx, cy, radius, color):
        """Pointer dot on the wheel. Start at bottom (6 o'clock), clockwise."""
        # frac: 0 = bottom, 1 = full turn back to bottom
        frac = (self.total_sec - MIN_TIME) / (MAX_TIME - MIN_TIME)
        # Start at bottom (pi/2), go clockwise
        angle = math.pi / 2 + frac * 2 * math.pi

        ring_w = 22
        ptr_r = radius - ring_w / 2
        px = cx + math.cos(angle) * ptr_r
        py = cy + math.sin(angle) * ptr_r

        # Glow
        p.setPen(Qt.PenStyle.NoPen)
        glow = QRadialGradient(px, py, 11)
        glow.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), 160))
        glow.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        p.setBrush(glow)
        p.drawEllipse(QPointF(px, py), 11, 11)
        # Core
        p.setBrush(color)
        p.drawEllipse(QPointF(px, py), 5, 5)
        # Bright center
        p.setBrush(QColor(255, 255, 255, 180))
        p.drawEllipse(QPointF(px, py), 2, 2)

    def _draw_progress(self, p, cx, cy, radius, color):
        """Countdown arc inside the wheel."""
        arc_w = 7
        rect_arc = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        # Track
        p.setPen(QPen(TRACK_COLOR, arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect_arc, 0, 360 * 16)

        # Progress (starts at bottom, clockwise = negative in Qt coords)
        if self.total_sec > 0:
            frac = self.remaining_sec / self.total_sec
        else:
            frac = 0
        span_angle = int(frac * 360 * 16)
        # Qt: angles in 1/16 degree, 0 = 3 o'clock, positive = counter-clockwise
        # Bottom = -90*16, clockwise = negative span
        start_angle = -90 * 16  # bottom
        p.setPen(QPen(color, arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect_arc, start_angle, -span_angle)

    # --- Input ---

    def wheelEvent(self, event):
        if self.running:
            return
        # Accumulate delta — 30 units threshold
        self._scroll_accum += event.angleDelta().y()
        threshold = 30
        steps = int(self._scroll_accum / threshold)
        if steps != 0:
            self._scroll_accum -= steps * threshold
            old_val = self.total_sec
            self.total_sec = max(MIN_TIME, min(MAX_TIME, self.total_sec + steps * STEP_SEC))
            if self.total_sec != old_val:
                self.remaining_sec = self.total_sec
                self._play_scroll_feedback()
                self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()
            self._press_timer.start()
            self._long_press_fired = False
            self._long_press_check.start()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            diff = event.globalPosition().toPoint() - self._drag_pos
            if diff.manhattanLength() > 5:
                if not self._dragging:
                    self._dragging = True
                    # Cancel click/hold if dragged
                    self._long_press_check.stop()
                    self._long_press_fired = True
                    # Use native system drag for proper multi-monitor support
                    self.windowHandle().startSystemMove()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_pos = None
        self._dragging = False
        self._long_press_check.stop()

        if self._long_press_fired:
            # Already handled by timer
            return

        # Short click
        self._scroll_snd.play()
        if self.running:
            self._pause()
        elif self.paused:
            self._resume()
        else:
            self._start()

    def _on_long_press(self):
        """Fires after 1s hold — no need to release."""
        self._long_press_fired = True
        self._scroll_snd.play()
        self._full_stop()

    # --- Actions ---

    def _start(self):
        if self.remaining_sec <= 0:
            return
        self.running = True
        self.paused = False
        self._ring_player.stop()
        self._timer.start()
        if self.is_work:
            self._on_work_tick()
            self._work_tick_timer.start()
        self.update()

    def _pause(self):
        self.running = False
        self.paused = True
        self._timer.stop()
        self._work_tick_timer.stop()
        self._work_tick_snd.stop()
        self.update()

    def _resume(self):
        self.running = True
        self.paused = False
        self._timer.start()
        if self.is_work:
            self._on_work_tick()
            self._work_tick_timer.start()
        self.update()

    def _full_stop(self):
        self._timer.stop()
        self.running = False
        self.paused = False
        self._work_tick_timer.stop()
        self._work_tick_snd.stop()
        self._ring_player.stop()
        self.is_work = True
        self.total_sec = 25 * 60
        self.remaining_sec = self.total_sec
        self.update()

    # --- Timer ---

    def _tick(self):
        if self.remaining_sec > 0:
            self.remaining_sec -= 1
            self.update()

        if self.remaining_sec <= 0:
            self._timer.stop()
            self.running = False
            self.paused = False
            self._work_tick_timer.stop()
            self._work_tick_snd.stop()

            if self.is_work:
                self._ring_player.setPosition(0)
                self._ring_player.play()
                self.is_work = False
                self.total_sec = 5 * 60
                self.remaining_sec = self.total_sec
            else:
                self._ring_player.setPosition(0)
                self._ring_player.play()
                self.is_work = True
                self.total_sec = 25 * 60
                self.remaining_sec = self.total_sec

            self.update()


# --- Entry ---

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(_make_tomato_icon())
    w = WheelTimer()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

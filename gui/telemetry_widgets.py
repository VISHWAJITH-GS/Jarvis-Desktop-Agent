"""
gui/telemetry_widgets.py - Modern Glassmorphic PyQt6 Telemetry Components for JARVIS

Redesigned Telemetry Dashboard:
1. StatCard: Glassmorphism dark card container (rgba(255,255,255,0.04), 14-16px radius, subtle border & glow).
2. RadialGauge: Custom QPainter circular gauge with smooth load-based color interpolation,
   glowing arc, easing QPropertyAnimation, bold centered %, and detail text.
3. DriveBar: Slim progress bar for storage partitions with free space text and load coloring.
4. BatteryWidget: Proportional battery-shaped icon with neon charging bolt and AC/battery chips.
5. SystemVitalsWidget: Responsive grid card housing CPU, RAM, GPU, VRAM radial gauges, storage, and battery.
6. GlowLineChart / RollingLoadChartWidget: Visual centerpiece 60-second line chart with Catmull-Rom
   spline smoothing, glowing multi-series lines (CPU, Memory, GPU), vertical fading gradient fill,
   faint gridlines, latest glowing dots with values, and interactive hover crosshair & tooltip.
7. ProcessTable / ProcessRadarWidget: Clean table of top consumers (no radar), inline RAM & CPU magnitude
   bars, sortable by RAM/CPU descending, instance grouping, monospace numbers, and zero-flicker in-place updates.
8. SpeedStat / NetLinkWaveformWidget: Two large stat blocks for Download (↓) and Upload (↑) with big bold
   numbers, auto-scaled units, arrow icons, connection name, and high-activity color pulse (no graph).
9. ActionsHistoryWidget: Compact glass list with success/fail icon, action description, and duration chip.
10. MasterTelemetryPanel: Unified command-center layout combining System Vitals, Load Line Chart,
    Processes table, Internet speed stats, and Actions history with responsive scaling.
"""

from __future__ import annotations

import collections
import datetime
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.theme import C
from gui.base import keena_font
from core.telemetry_service import TelemetryEventBus, get_event_bus


# ─────────────────────────────────────────────────────────────────────────────
# Design System & Palette Constants
# ─────────────────────────────────────────────────────────────────────────────

# Dark glassmorphic palette
BG_COLOR = "#07090f"
CARD_BG = "rgba(255, 255, 255, 0.04)"
CARD_BORDER = "rgba(255, 255, 255, 0.08)"
CARD_BORDER_COLOR = QColor(255, 255, 255, 20)
CARD_BG_COLOR = QColor(13, 17, 28, 210)
CARD_RADIUS = 14

# Neon accents (Restrained palette: cyan, violet, amber. Red reserved ONLY for critical/warning)
ACCENT_CYAN = QColor(34, 211, 238)      # #22d3ee (Normal load 0-60%)
ACCENT_VIOLET = QColor(167, 139, 250)   # #a78bfa (Memory / upload accent)
ACCENT_AMBER = QColor(251, 191, 36)     # #fbbf24 (Elevated load 60-85%)
ACCENT_RED = QColor(244, 63, 94)        # #f43f5e (Critical load 85-100% only)
ACCENT_GREEN = QColor(16, 185, 129)     # #10b981 (Success / online)

TEXT_PRIMARY = "#f8fafc"
TEXT_MUTED = "#94a3b8"
TEXT_DIM = "#64748b"


def sans_font(size: float | int, bold: bool = False, letter_spacing: float = 0.0) -> QFont:
    """Clean geometric sans font for labels and titles (Segoe UI / Inter)."""
    f = QFont("Segoe UI")
    if isinstance(size, float):
        f.setPointSizeF(size)
    else:
        f.setPointSize(int(size))
    f.setBold(bold)
    if letter_spacing > 0.0:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
    return f


def mono_font(size: float | int, bold: bool = False) -> QFont:
    """Clean monospace font for numbers and metrics (Consolas / JetBrains Mono)."""
    f = QFont("Consolas")
    if isinstance(size, float):
        f.setPointSizeF(size)
    else:
        f.setPointSize(int(size))
    f.setBold(bold)
    return f


def load_color(pct: float) -> QColor:
    """
    Smoothly interpolate color based on load percentage:
      0  - 60%: Cyan (#22d3ee)
      60 - 85%: Cyan blending into Amber (#fbbf24)
      85 - 100%: Amber blending into Warning Red (#f43f5e)
    """
    val = max(0.0, min(100.0, float(pct)))
    if val <= 60.0:
        return ACCENT_CYAN
    elif val <= 85.0:
        t = (val - 60.0) / 25.0
        r = int(ACCENT_CYAN.red() + t * (ACCENT_AMBER.red() - ACCENT_CYAN.red()))
        g = int(ACCENT_CYAN.green() + t * (ACCENT_AMBER.green() - ACCENT_CYAN.green()))
        b = int(ACCENT_CYAN.blue() + t * (ACCENT_AMBER.blue() - ACCENT_CYAN.blue()))
        return QColor(r, g, b)
    else:
        t = (val - 85.0) / 15.0
        r = int(ACCENT_AMBER.red() + t * (ACCENT_RED.red() - ACCENT_AMBER.red()))
        g = int(ACCENT_AMBER.green() + t * (ACCENT_RED.green() - ACCENT_AMBER.green()))
        b = int(ACCENT_AMBER.blue() + t * (ACCENT_RED.blue() - ACCENT_AMBER.blue()))
        return QColor(r, g, b)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Base Glassmorphic Container: StatCard
# ─────────────────────────────────────────────────────────────────────────────

class StatCard(QFrame):
    """
    Dark glassmorphic container with rounded corners (14-16px),
    rgba(255,255,255,0.04) fill, 1px border rgba(255,255,255,0.08),
    and soft glow/shadow.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {CARD_BG};
                border: 1px solid {CARD_BORDER};
                border-radius: {CARD_RADIUS}px;
            }}
        """)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Circular Radial Gauge Widget
# ─────────────────────────────────────────────────────────────────────────────

class RadialGauge(QWidget):
    """
    Circular (radial) gauge with smooth QPainter rendering:
    - Large bold percentage in the center
    - Muted uppercase label at the top
    - Small detail text (GHz, GB, °C) underneath
    - Smooth color interpolation (cyan -> amber -> red)
    - Animated value changes using QPropertyAnimation (easing, never jumping)
    - Soft glow around the value arc
    - Optional mini thermometer / colored chip for temperatures
    - Optional mini-core load bars for multi-core CPU
    - Graceful fallback for missing data (shows 'N/A')
    """

    def __init__(
        self,
        title: str = "METRIC",
        sub_text: str = "--",
        parent: Optional[QWidget] = None,
        show_temp_chip: bool = False,
    ):
        super().__init__(parent)
        self._title = title.upper()
        self._sub_text = sub_text
        self._target_value = 0.0
        self._anim_value = 0.0
        self._is_na = False
        self._show_temp_chip = show_temp_chip
        self._temp_c = -1.0
        self._mini_bars: List[float] = []

        self.setMinimumSize(110, 125)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setStyleSheet("background: transparent;")

        # Property animation for smooth transitions
        self._anim = QPropertyAnimation(self, b"animated_value", self)
        self._anim.setDuration(350)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    @pyqtProperty(float)
    def animated_value(self) -> float:
        return self._anim_value

    @animated_value.setter
    def animated_value(self, val: float):
        self._anim_value = max(0.0, min(100.0, float(val)))
        self.update()

    def set_value(self, val: float, sub_text: Optional[str] = None, is_na: bool = False):
        self._is_na = is_na
        if sub_text is not None:
            self._sub_text = sub_text

        if is_na or val < 0:
            self._is_na = True
            self._target_value = 0.0
            self._anim.stop()
            self._anim_value = 0.0
            self.update()
            return

        self._is_na = False
        new_target = max(0.0, min(100.0, float(val)))
        if abs(new_target - self._target_value) > 0.1:
            self._target_value = new_target
            self._anim.stop()
            self._anim.setStartValue(self._anim_value)
            self._anim.setEndValue(self._target_value)
            self._anim.start()
        else:
            self.update()

    def set_temp(self, temp_c: float):
        self._temp_c = temp_c
        self.update()

    def set_mini_bars(self, bars: List[float]):
        self._mini_bars = list(bars)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w = self.width()
        h = self.height()

        # Glass card background
        card_rect = QRectF(0, 0, w, h)
        painter.setBrush(QBrush(CARD_BG_COLOR))
        painter.setPen(QPen(CARD_BORDER_COLOR, 1))
        painter.drawRoundedRect(card_rect.adjusted(0.5, 0.5, -0.5, -0.5), CARD_RADIUS, CARD_RADIUS)

        # 1. Section Title (small, uppercase, letter-spaced)
        painter.setFont(sans_font(7, bold=True, letter_spacing=1.2))
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(8, 8, w - 16, 16), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self._title)

        # 2. Radial Arc Dimensions
        cx = w / 2.0
        cy = 58.0
        radius = min(36.0, (w - 28) / 2.0)
        track_width = 5.5
        arc_rect = QRectF(cx - radius, cy - radius, radius * 2.0, radius * 2.0)

        # 240° sweep arc: starts at 210° (bottom-left) and sweeps -240° clockwise to -30° (bottom-right)
        start_angle = 210 * 16
        total_span = -240 * 16

        # Draw background track arc
        track_pen = QPen(QColor(255, 255, 255, 18), track_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(arc_rect, start_angle, total_span)

        if not self._is_na and self._anim_value > 0.0:
            val_col = load_color(self._anim_value)
            value_span = int(total_span * (self._anim_value / 100.0))

            # Soft glow under-arc
            glow_col = QColor(val_col.red(), val_col.green(), val_col.blue(), 40)
            glow_pen = QPen(glow_col, track_width + 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(glow_pen)
            painter.drawArc(arc_rect, start_angle, value_span)

            # Crisp foreground arc
            val_pen = QPen(val_col, track_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(val_pen)
            painter.drawArc(arc_rect, start_angle, value_span)

        # 3. Big Value Text in Center
        painter.setFont(mono_font(13, bold=True))
        if self._is_na:
            painter.setPen(QColor(TEXT_DIM))
            val_text = "N/A"
        else:
            painter.setPen(QColor(TEXT_PRIMARY))
            val_text = f"{int(round(self._anim_value))}%"
        painter.drawText(QRectF(cx - 38, cy - 13, 76, 26), Qt.AlignmentFlag.AlignCenter, val_text)

        # 4. Detail Sub-Text underneath center
        painter.setFont(sans_font(7, bold=False))
        painter.setPen(QColor(TEXT_MUTED))
        sub_y = cy + radius - 2.0
        painter.drawText(QRectF(6, sub_y, w - 12, 16), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self._sub_text)

        # 5. Optional Temperature Chip or Mini-Bars
        bottom_y = sub_y + 17.0
        if self._show_temp_chip and self._temp_c > 0:
            chip_t = int(self._temp_c)
            if chip_t < 60:
                c_lbl = "COOL"
                c_col = ACCENT_CYAN
            elif chip_t < 80:
                c_lbl = "WARM"
                c_col = ACCENT_AMBER
            else:
                c_lbl = "HOT"
                c_col = ACCENT_RED

            # Small thermometer pill
            pill_text = f"{chip_t}°C · {c_lbl}"
            painter.setFont(mono_font(6, bold=True))
            pill_w = 64
            pill_h = 14
            pill_x = cx - (pill_w / 2.0)
            pill_rect = QRectF(pill_x, bottom_y, pill_w, pill_h)
            painter.setBrush(QBrush(QColor(c_col.red(), c_col.green(), c_col.blue(), 25)))
            painter.setPen(QPen(QColor(c_col.red(), c_col.green(), c_col.blue(), 90), 1))
            painter.drawRoundedRect(pill_rect, 3, 3)
            painter.setPen(c_col)
            painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, pill_text)

        elif self._mini_bars and len(self._mini_bars) > 1:
            # Draw optional per-core mini bars
            nbars = min(16, len(self._mini_bars))
            bar_w = max(2.5, min(4.5, (w - 30) / nbars))
            bar_gap = 1.5
            total_bars_w = (nbars * bar_w) + ((nbars - 1) * bar_gap)
            start_bx = cx - (total_bars_w / 2.0)
            max_bh = 10.0

            for i in range(nbars):
                c_val = max(0.0, min(100.0, self._mini_bars[i]))
                bh = max(1.5, (c_val / 100.0) * max_bh)
                bx = start_bx + i * (bar_w + bar_gap)
                by = bottom_y + (max_bh - bh)
                bcol = load_color(c_val)
                painter.setBrush(QBrush(bcol))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(QRectF(bx, by, bar_w, bh), 1, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Storage Drive Bar Widget
# ─────────────────────────────────────────────────────────────────────────────

class DriveBar(QWidget):
    """
    Slim horizontal progress bar for storage drives with load-based color,
    mount label on the left, and free space formatted on the right.
    """

    def __init__(self, mount_label: str = "C:", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._mount = mount_label
        self._free_str = "-- GB free"
        self._pct = 0.0
        self.setFixedHeight(24)
        self.setStyleSheet("background: transparent;")

    def set_data(self, mount_label: str, pct: float, free_str: str):
        self._mount = mount_label
        self._pct = max(0.0, min(100.0, float(pct)))
        self._free_str = free_str
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w = self.width()
        h = self.height()

        # Labels row: Left Drive name, Right free space
        painter.setFont(sans_font(7, bold=True))
        painter.setPen(QColor(TEXT_PRIMARY))
        painter.drawText(0, 9, f"DRIVE {self._mount}")

        painter.setFont(mono_font(7))
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(0, 0, w, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, f"{self._free_str} ({int(self._pct)}%)")

        # Slim progress track
        bar_y = 15.0
        bar_h = 5.0
        bar_w = float(w)

        # Track background
        painter.setBrush(QBrush(QColor(255, 255, 255, 15)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(0, bar_y, bar_w, bar_h), 2.5, 2.5)

        # Filled progress chunk with load color
        if self._pct > 0.0:
            fill_w = max(4.0, (self._pct / 100.0) * bar_w)
            col = load_color(self._pct)
            painter.setBrush(QBrush(col))
            painter.drawRoundedRect(QRectF(0, bar_y, fill_w, bar_h), 2.5, 2.5)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Battery / Power State Widget
# ─────────────────────────────────────────────────────────────────────────────

class BatteryWidget(QWidget):
    """
    Battery-shaped icon that fills proportionally, with a lightning bolt
    when plugged in, big bold percentage, and AC/battery status chip.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pct = 100.0
        self._plugged = True
        self._is_na = False
        self.setFixedHeight(28)
        self.setStyleSheet("background: transparent;")

    def set_battery(self, pct: Optional[float], plugged: Optional[bool]):
        if pct is None:
            self._is_na = True
            self._pct = 0.0
            self._plugged = True
        else:
            self._is_na = False
            self._pct = max(0.0, min(100.0, float(pct)))
            self._plugged = bool(plugged)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w = self.width()

        # 1. Proportional Battery Icon
        b_x = 0.0
        b_y = 6.0
        b_w = 26.0
        b_h = 14.0

        # Battery outer casing
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1.2))
        painter.setBrush(QBrush(QColor(10, 14, 24, 180)))
        painter.drawRoundedRect(QRectF(b_x, b_y, b_w, b_h), 3, 3)

        # Battery positive terminal bump
        painter.setBrush(QBrush(QColor(255, 255, 255, 70)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(b_x + b_w + 1.0, b_y + 4.0, 2.0, 6.0), 1, 1)

        # Fill level inside battery
        if not self._is_na and self._pct > 0.0:
            inner_w = (b_w - 4.0) * (self._pct / 100.0)
            # Low battery turns amber/red, high is cyan
            bcol = load_color(100.0 - self._pct) if not self._plugged else ACCENT_CYAN
            painter.setBrush(QBrush(bcol))
            painter.drawRoundedRect(QRectF(b_x + 2.0, b_y + 2.0, inner_w, b_h - 4.0), 1.5, 1.5)

        # Lightning bolt when plugged in
        if self._plugged:
            bolt_path = QPainterPath()
            lx = b_x + (b_w / 2.0)
            ly = b_y + 2.0
            bolt_path.moveTo(lx + 1.0, ly)
            bolt_path.lineTo(lx - 2.5, ly + 5.5)
            bolt_path.lineTo(lx, ly + 5.5)
            bolt_path.lineTo(lx - 1.0, ly + 10.0)
            bolt_path.lineTo(lx + 2.5, ly + 4.5)
            bolt_path.lineTo(lx, ly + 4.5)
            bolt_path.closeSubpath()
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.setPen(QPen(QColor("#000000"), 0.5))
            painter.drawPath(bolt_path)

        # 2. Battery Percentage
        painter.setFont(mono_font(9, bold=True))
        painter.setPen(QColor(TEXT_PRIMARY))
        pct_text = "N/A" if self._is_na else f"{int(self._pct)}%"
        painter.drawText(QRectF(36, 4, 45, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, pct_text)

        # 3. Status chip
        chip_x = 86
        chip_w = 64
        chip_h = 16
        chip_rect = QRectF(chip_x, 6, chip_w, chip_h)
        if self._plugged:
            c_text = "AC POWER"
            c_col = ACCENT_CYAN
        else:
            c_text = "ON BATTERY"
            c_col = ACCENT_AMBER

        painter.setBrush(QBrush(QColor(c_col.red(), c_col.green(), c_col.blue(), 20)))
        painter.setPen(QPen(QColor(c_col.red(), c_col.green(), c_col.blue(), 80), 1))
        painter.drawRoundedRect(chip_rect, 3, 3)
        painter.setFont(sans_font(6, bold=True))
        painter.setPen(c_col)
        painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, c_text)


# ─────────────────────────────────────────────────────────────────────────────
# 5. System Vitals Widget (Composing Gauges, Drives, and Power)
# ─────────────────────────────────────────────────────────────────────────────

class SystemVitalsWidget(QWidget):
    """
    Renders CPU, RAM, GPU, VRAM with circular radial gauges,
    storage partitions with slim drive progress bars, and proportional battery.
    Maintains compatibility aliases for external callers and tests.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._disk_bars: Dict[str, DriveBar] = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 1. Top Row: 4 Radial Gauges in a Responsive Grid ──
        self.gauges_grid = QGridLayout()
        self.gauges_grid.setContentsMargins(0, 0, 0, 0)
        self.gauges_grid.setSpacing(8)

        # CPU Radial Gauge
        self.gauge_cpu = RadialGauge(title="CPU", sub_text="0 MHz")
        self.gauges_grid.addWidget(self.gauge_cpu, 0, 0)

        # RAM Radial Gauge
        self.gauge_ram = RadialGauge(title="RAM", sub_text="0.0 / 0.0 GB")
        self.gauges_grid.addWidget(self.gauge_ram, 0, 1)

        # GPU Radial Gauge
        self.gauge_gpu = RadialGauge(title="GPU", sub_text="-- °C", show_temp_chip=True)
        self.gauges_grid.addWidget(self.gauge_gpu, 0, 2)

        # Dedicated VRAM Radial Gauge
        self.gauge_vram = RadialGauge(title="VRAM", sub_text="0.0 / 0.0 GB")
        self.gauges_grid.addWidget(self.gauge_vram, 0, 3)

        root.addLayout(self.gauges_grid)

        # ── 2. Bottom Row: Storage Drives + Battery Cards ──
        sub_row = QHBoxLayout()
        sub_row.setContentsMargins(0, 0, 0, 0)
        sub_row.setSpacing(8)

        # Storage Drives Card
        self.card_storage = StatCard()
        v_st = QVBoxLayout(self.card_storage)
        v_st.setContentsMargins(12, 10, 12, 10)
        v_st.setSpacing(6)

        lbl_st_title = QLabel("STORAGE DRIVES")
        lbl_st_title.setFont(sans_font(7, bold=True, letter_spacing=1.2))
        lbl_st_title.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        v_st.addWidget(lbl_st_title)

        self.drives_container = QVBoxLayout()
        self.drives_container.setSpacing(5)
        v_st.addLayout(self.drives_container)
        v_st.addStretch()
        sub_row.addWidget(self.card_storage, stretch=3)

        # Power / Battery Card
        self.card_battery = StatCard()
        v_bat = QVBoxLayout(self.card_battery)
        v_bat.setContentsMargins(12, 10, 12, 10)
        v_bat.setSpacing(6)

        lbl_bat_title = QLabel("POWER & BATTERY")
        lbl_bat_title.setFont(sans_font(7, bold=True, letter_spacing=1.2))
        lbl_bat_title.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        v_bat.addWidget(lbl_bat_title)

        self.battery_widget = BatteryWidget()
        v_bat.addWidget(self.battery_widget)
        v_bat.addStretch()
        sub_row.addWidget(self.card_battery, stretch=2)

        root.addLayout(sub_row)

        # ── Compatibility surrogate labels/bars for legacy callers and unit tests ──
        self.lbl_cpu_val = QLabel("0.0 %")
        self.lbl_cpu_freq = QLabel("0 MHz")
        self.lbl_ram_val = QLabel("0.0 / 0.0 GB")
        self.lbl_gpu_val = QLabel("0 %")
        self.lbl_gpu_temp = QLabel("-- °C")
        self.lbl_vram_val = QLabel("0.0 / 0.0 GB")
        self.lbl_bat_val = QLabel("N/A")

        self.bar_cpu = QProgressBar()
        self.bar_cpu.setRange(0, 100)
        self.bar_ram = QProgressBar()
        self.bar_ram.setRange(0, 100)
        self.bar_gpu = QProgressBar()
        self.bar_gpu.setRange(0, 100)
        self.bar_vram = QProgressBar()
        self.bar_vram.setRange(0, 100)
        self.bar_bat = QProgressBar()
        self.bar_bat.setRange(0, 100)

    def update_vitals(self, snap: Dict[str, Any]):
        # 1. CPU
        cpu_p = snap.get("cpu_pct", 0.0)
        cpu_freq = snap.get("cpu_freq_mhz", 0.0)
        cpu_percore = snap.get("cpu_percore", [])
        freq_str = f"{int(cpu_freq)} MHz" if cpu_freq > 0 else "--"

        self.gauge_cpu.set_value(cpu_p, sub_text=freq_str)
        if cpu_percore:
            self.gauge_cpu.set_mini_bars(cpu_percore)

        self.lbl_cpu_val.setText(f"{cpu_p:.1f} %")
        self.lbl_cpu_freq.setText(freq_str)
        self.bar_cpu.setValue(int(cpu_p))

        # 2. RAM
        mem_used = snap.get("mem_used_gb", 0.0)
        mem_tot = snap.get("mem_total_gb", 0.0)
        mem_p = snap.get("mem_pct", 0.0)
        mem_str = f"{mem_used:.1f} / {mem_tot:.1f} GB"

        self.gauge_ram.set_value(mem_p, sub_text=mem_str)
        self.lbl_ram_val.setText(f"{mem_str} ({int(mem_p)}%)")
        self.bar_ram.setValue(int(mem_p))

        # 3. GPU & Temperature
        gpu_p = snap.get("gpu_pct", -1.0)
        gpu_t = snap.get("gpu_temp_c", -1.0)
        if gpu_p >= 0:
            temp_str = f"{int(gpu_t)} °C" if gpu_t > 0 else "--"
            self.gauge_gpu.set_value(gpu_p, sub_text=temp_str)
            self.gauge_gpu.set_temp(gpu_t)
            self.lbl_gpu_val.setText(f"{int(gpu_p)} %")
            self.lbl_gpu_temp.setText(temp_str)
            self.bar_gpu.setValue(int(gpu_p))
        else:
            self.gauge_gpu.set_value(0, sub_text="N/A", is_na=True)
            self.lbl_gpu_val.setText("N/A")
            self.lbl_gpu_temp.setText("")
            self.bar_gpu.setValue(0)

        # 4. Dedicated VRAM
        v_used = snap.get("gpu_vram_used_gb", -1.0)
        v_tot = snap.get("gpu_vram_total_gb", -1.0)
        v_pct = snap.get("gpu_vram_pct", -1.0)
        if v_used >= 0 and v_tot > 0:
            v_str = f"{v_used:.1f} / {v_tot:.1f} GB"
            self.gauge_vram.set_value(v_pct, sub_text=v_str)
            self.lbl_vram_val.setText(f"{v_str} ({int(v_pct)}%)")
            self.bar_vram.setValue(int(v_pct))
        else:
            self.gauge_vram.set_value(0, sub_text="N/A", is_na=True)
            self.lbl_vram_val.setText("N/A")
            self.bar_vram.setValue(0)

        # 5. Storage Partitions
        disks = snap.get("disks", [])
        for d in disks:
            mnt = d.get("mount", "")
            lbl_name = d.get("label", mnt)
            fmt = d.get("formatted", "")
            pct = d.get("pct", 0.0)

            if mnt not in self._disk_bars:
                d_bar = DriveBar(lbl_name)
                self.drives_container.addWidget(d_bar)
                self._disk_bars[mnt] = d_bar

            self._disk_bars[mnt].set_data(lbl_name, pct, fmt)

        # 6. Battery & Power
        bat_p = snap.get("battery_pct")
        bat_plug = snap.get("battery_plugged", True)
        self.battery_widget.set_battery(bat_p, bat_plug)
        if bat_p is not None:
            self.lbl_bat_val.setText(snap.get("battery_formatted", "N/A"))
            self.bar_bat.setValue(int(bat_p))
        else:
            self.lbl_bat_val.setText("N/A")
            self.bar_bat.setValue(0)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Load, Last 60 Seconds: Visual Centerpiece Line Chart
# ─────────────────────────────────────────────────────────────────────────────

class GlowLineChart(QWidget):
    """
    Wide, smooth, anti-aliased multi-series line chart:
    - Catmull-Rom spline smoothing (smooth curves, never stepped)
    - Three series: CPU (#22d3ee), Memory (#a78bfa), GPU (#fbbf24)
    - Soft glowing lines with subtle fading vertical gradient fill
    - Faint horizontal gridlines at 25%, 50%, 75%, 100% with subtle labels
    - 60s -> now time axis
    - Glowing dot at the latest point of each line with current value label
    - Hover crosshair with interactive glass tooltip showing all 3 values
    - Top legend chips matching line colors
    - Fluid real-time scrolling
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(175)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setStyleSheet("background: transparent;")
        self.setMouseTracking(True)

        self._history: List[Tuple[float, float, float, float]] = []  # (t, cpu, mem, gpu)
        self._latest_cpu = 0.0
        self._latest_mem = 0.0
        self._latest_gpu = 0.0

        # Mouse hover crosshair state
        self._hover_pt: Optional[QPointF] = None
        self._hover_idx: int = -1

    def update_history(self, samples: List[Tuple[float, float, float, float]]):
        self._history = list(samples)
        if self._history:
            _, self._latest_cpu, self._latest_mem, self._latest_gpu = self._history[-1]
        self.update()

    def mouseMoveEvent(self, event):
        self._hover_pt = event.position()
        self.update()

    def leaveEvent(self, event):
        self._hover_pt = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w = self.width()
        h = self.height()

        # 1. Dark Glass Card Background
        card_rect = QRectF(0, 0, w, h)
        painter.setBrush(QBrush(CARD_BG_COLOR))
        painter.setPen(QPen(CARD_BORDER_COLOR, 1))
        painter.drawRoundedRect(card_rect.adjusted(0.5, 0.5, -0.5, -0.5), CARD_RADIUS, CARD_RADIUS)

        # 2. Header & Live Legend Chips
        painter.setFont(sans_font(7, bold=True, letter_spacing=1.2))
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(14, 18, "LOAD, LAST 60 SECONDS")

        # Draw Live Legend Chips on Top-Right
        chip_x = w - 245
        chip_y = 7
        self._draw_legend_chip(painter, chip_x, chip_y, "CPU", f"{int(self._latest_cpu)}%", ACCENT_CYAN)
        chip_x += 76
        self._draw_legend_chip(painter, chip_x, chip_y, "MEM", f"{int(self._latest_mem)}%", ACCENT_VIOLET)
        chip_x += 76
        self._draw_legend_chip(painter, chip_x, chip_y, "GPU", f"{int(self._latest_gpu)}%", ACCENT_AMBER)

        # 3. Plot Area Geometry
        top_y = 30.0
        bot_y = h - 22.0
        left_x = 34.0
        right_x = w - 24.0
        plot_w = right_x - left_x
        plot_h = bot_y - top_y

        if plot_w <= 20 or plot_h <= 20:
            return

        # 4. Faint Horizontal Gridlines & Y-Axis Labels (25%, 50%, 75%, 100%)
        painter.setFont(mono_font(6))
        painter.setPen(QColor(TEXT_DIM))
        grid_pen = QPen(QColor(255, 255, 255, 14), 1, Qt.PenStyle.DashLine)

        for pct in (0.25, 0.50, 0.75, 1.0):
            gy = bot_y - (plot_h * pct)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(left_x, gy), QPointF(right_x, gy))
            painter.setPen(QColor(TEXT_DIM))
            painter.drawText(QRectF(4, gy - 6, 26, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{int(pct * 100)}")

        # 5. Time Axis (-60s, -45s, -30s, -15s, NOW)
        painter.setFont(sans_font(6))
        painter.setPen(QColor(TEXT_DIM))
        time_labels = [("-60s", 0.0), ("-45s", 0.25), ("-30s", 0.50), ("-15s", 0.75), ("NOW", 1.0)]
        for lbl, frac in time_labels:
            tx = left_x + (frac * plot_w)
            align = Qt.AlignmentFlag.AlignLeft if frac == 0 else (Qt.AlignmentFlag.AlignRight if frac == 1.0 else Qt.AlignmentFlag.AlignHCenter)
            painter.drawText(QRectF(tx - 15, bot_y + 4, 30, 14), align | Qt.AlignmentFlag.AlignTop, lbl)

        if len(self._history) < 2:
            return

        # 6. Map History Points to Chart Geometry
        n = len(self._history)
        step_x = plot_w / max(1, 60 - 1)
        start_x = right_x - (n - 1) * step_x

        cpu_pts: List[QPointF] = []
        mem_pts: List[QPointF] = []
        gpu_pts: List[QPointF] = []

        for i, item in enumerate(self._history):
            px = start_x + (i * step_x)
            cpu_val = max(0.0, min(100.0, float(item[1])))
            mem_val = max(0.0, min(100.0, float(item[2])))
            gpu_val = max(0.0, min(100.0, float(item[3])))

            cpu_pts.append(QPointF(px, bot_y - (cpu_val / 100.0 * plot_h)))
            mem_pts.append(QPointF(px, bot_y - (mem_val / 100.0 * plot_h)))
            gpu_pts.append(QPointF(px, bot_y - (gpu_val / 100.0 * plot_h)))

        # 7. Draw Series with Catmull-Rom Spline Smoothing + Vertical Gradient Fill
        # Series order: GPU (amber), Memory (violet), CPU (cyan)
        self._draw_spline_series(painter, gpu_pts, ACCENT_AMBER, bot_y, top_y, f"{int(self._latest_gpu)}%")
        self._draw_spline_series(painter, mem_pts, ACCENT_VIOLET, bot_y, top_y, f"{int(self._latest_mem)}%")
        self._draw_spline_series(painter, cpu_pts, ACCENT_CYAN, bot_y, top_y, f"{int(self._latest_cpu)}%")

        # 8. Interactive Hover Crosshair & Tooltip
        if self._hover_pt and left_x <= self._hover_pt.x() <= right_x and top_y <= self._hover_pt.y() <= bot_y:
            hx = self._hover_pt.x()
            # Find nearest index
            nearest_idx = min(n - 1, max(0, int(round((hx - start_x) / max(0.1, step_x)))))
            if 0 <= nearest_idx < n:
                snap_x = cpu_pts[nearest_idx].x()

                # Draw vertical subtle dashed crosshair line
                cross_pen = QPen(QColor(255, 255, 255, 60), 1, Qt.PenStyle.DashLine)
                painter.setPen(cross_pen)
                painter.drawLine(QPointF(snap_x, top_y), QPointF(snap_x, bot_y))

                # Highlight dots at crosshair
                for pt, col in [(cpu_pts[nearest_idx], ACCENT_CYAN), (mem_pts[nearest_idx], ACCENT_VIOLET), (gpu_pts[nearest_idx], ACCENT_AMBER)]:
                    painter.setBrush(QBrush(col))
                    painter.setPen(QPen(QColor("#ffffff"), 1))
                    painter.drawEllipse(pt, 3.5, 3.5)

                # Floating glass tooltip
                item = self._history[nearest_idx]
                c_val = int(item[1])
                m_val = int(item[2])
                g_val = int(item[3])

                tip_w = 98.0
                tip_h = 56.0
                tip_x = snap_x + 10.0
                if tip_x + tip_w > right_x:
                    tip_x = snap_x - tip_w - 10.0
                tip_y = max(top_y, min(bot_y - tip_h, self._hover_pt.y() - (tip_h / 2.0)))

                tip_rect = QRectF(tip_x, tip_y, tip_w, tip_h)
                painter.setBrush(QBrush(QColor(10, 14, 24, 235)))
                painter.setPen(QPen(QColor(255, 255, 255, 35), 1))
                painter.drawRoundedRect(tip_rect, 6, 6)

                painter.setFont(mono_font(6.5))
                painter.setPen(QColor(TEXT_MUTED))
                secs_ago = int((n - 1 - nearest_idx) * (60.0 / max(1, n - 1)))
                painter.drawText(QRectF(tip_x + 6, tip_y + 4, tip_w - 12, 12), Qt.AlignmentFlag.AlignLeft, f"-{secs_ago}s ago")

                painter.setPen(ACCENT_CYAN)
                painter.drawText(QRectF(tip_x + 6, tip_y + 17, tip_w - 12, 11), Qt.AlignmentFlag.AlignLeft, f"CPU: {c_val}%")

                painter.setPen(ACCENT_VIOLET)
                painter.drawText(QRectF(tip_x + 6, tip_y + 29, tip_w - 12, 11), Qt.AlignmentFlag.AlignLeft, f"MEM: {m_val}%")

                painter.setPen(ACCENT_AMBER)
                painter.drawText(QRectF(tip_x + 6, tip_y + 41, tip_w - 12, 11), Qt.AlignmentFlag.AlignLeft, f"GPU: {g_val}%")

    def _draw_legend_chip(self, painter: QPainter, x: float, y: float, label: str, val: str, color: QColor):
        chip_rect = QRectF(x, y, 70, 18)
        painter.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 18)))
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 60), 1))
        painter.drawRoundedRect(chip_rect, 4, 4)

        # Dot
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(x + 7, y + 9), 2.5, 2.5)

        # Text
        painter.setFont(sans_font(6.5, bold=True))
        painter.setPen(color)
        painter.drawText(QRectF(x + 13, y, 54, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{label} {val}")

    def _draw_spline_series(
        self,
        painter: QPainter,
        pts: List[QPointF],
        color: QColor,
        bot_y: float,
        top_y: float,
        latest_label: str,
    ):
        if len(pts) < 2:
            return

        # 1. Build Catmull-Rom smooth spline path
        path = QPainterPath()
        path.moveTo(pts[0])

        for i in range(len(pts) - 1):
            p0 = pts[i - 1] if i > 0 else pts[i]
            p1 = pts[i]
            p2 = pts[i + 1]
            p3 = pts[i + 2] if i + 2 < len(pts) else p2

            # Catmull-Rom to Cubic Bezier control points
            c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0, p1.y() + (p2.y() - p0.y()) / 6.0)
            c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0, p2.y() - (p3.y() - p1.y()) / 6.0)
            path.cubicTo(c1, c2, p2)

        # 2. Draw Subtle Vertical Gradient Fill fading to transparent
        fill_path = QPainterPath(path)
        fill_path.lineTo(pts[-1].x(), bot_y)
        fill_path.lineTo(pts[0].x(), bot_y)
        fill_path.closeSubpath()

        grad = QLinearGradient(0, top_y, 0, bot_y)
        grad.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), 30))
        grad.setColorAt(0.8, QColor(color.red(), color.green(), color.blue(), 5))
        grad.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(fill_path)

        # 3. Soft Glowing Line (wide translucent stroke behind)
        glow_pen = QPen(QColor(color.red(), color.green(), color.blue(), 38), 5.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(glow_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # 4. Crisp Foreground Line
        line_pen = QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(line_pen)
        painter.drawPath(path)

        # 5. Glowing Dot at Latest Point with Live Value Label
        last_pt = pts[-1]
        # Halo
        painter.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 50)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(last_pt, 6.0, 6.0)

        # Center dot
        painter.setBrush(QBrush(color))
        painter.drawEllipse(last_pt, 3.0, 3.0)


# Backward compatibility alias
RollingLoadChartWidget = GlowLineChart


# ─────────────────────────────────────────────────────────────────────────────
# 7. Process Table Widget (Processes: Clean Table, No Radar)
# ─────────────────────────────────────────────────────────────────────────────

class ProcessTable(QWidget):
    """
    Clean table of top consumers (replaces the radar graphic):
    - Process name (grouped with instance count like 'python.exe ×5')
    - RAM formatted with inline magnitude bar
    - CPU % with inline magnitude bar
    - Sortable by RAM or CPU descending
    - Up to top 8 rows
    - Zebra striping with subtle hover highlight
    - Right-aligned monospace numbers
    - Smooth in-place updates without flicker or jumping
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._sort_by = "ram"  # "ram" or "cpu"
        self._raw_procs: List[Dict[str, Any]] = []
        self._max_rows = 8
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # Container StatCard
        card = StatCard()
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(12, 10, 12, 10)
        c_lay.setSpacing(6)

        # Card Header with interactive sort toggles
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)

        title = QLabel("TOP PROCESS CONSUMERS")
        title.setFont(sans_font(7, bold=True, letter_spacing=1.2))
        title.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        hdr.addWidget(title)
        hdr.addStretch()

        # Sort Buttons
        self.btn_sort_ram = QPushButton("RAM ↓")
        self.btn_sort_ram.setFont(mono_font(6.5, bold=True))
        self.btn_sort_ram.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sort_ram.clicked.connect(lambda: self._set_sort("ram"))
        hdr.addWidget(self.btn_sort_ram)

        self.btn_sort_cpu = QPushButton("CPU ↓")
        self.btn_sort_cpu.setFont(mono_font(6.5, bold=True))
        self.btn_sort_cpu.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sort_cpu.clicked.connect(lambda: self._set_sort("cpu"))
        hdr.addWidget(self.btn_sort_cpu)

        self._update_sort_btn_styles()
        c_lay.addLayout(hdr)

        # Table Column Header
        col_hdr = QHBoxLayout()
        col_hdr.setContentsMargins(4, 2, 4, 2)
        h_name = QLabel("PROCESS NAME")
        h_name.setFont(sans_font(6.5, bold=True))
        h_name.setStyleSheet(f"color: {TEXT_DIM};")
        col_hdr.addWidget(h_name, stretch=4)

        h_ram = QLabel("MEMORY")
        h_ram.setFont(sans_font(6.5, bold=True))
        h_ram.setStyleSheet(f"color: {TEXT_DIM};")
        col_hdr.addWidget(h_ram, stretch=3)

        h_cpu = QLabel("CPU %")
        h_cpu.setFont(sans_font(6.5, bold=True))
        h_cpu.setStyleSheet(f"color: {TEXT_DIM};")
        col_hdr.addWidget(h_cpu, stretch=2)
        c_lay.addLayout(col_hdr)

        # Process Rows Container (pre-allocated 8 rows for flicker-free in-place text updates)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(2)

        self._row_widgets = []
        self.proc_rows = []  # Compatibility list of (lbl_name, lbl_ram, lbl_cpu) tuples

        for i in range(self._max_rows):
            rw = QFrame()
            rw.setFixedHeight(22)
            bg = "rgba(255, 255, 255, 0.02)" if i % 2 == 1 else "transparent"
            rw.setStyleSheet(f"""
                QFrame {{
                    background: {bg};
                    border-radius: 4px;
                    border: none;
                }}
                QFrame:hover {{
                    background: rgba(34, 211, 238, 0.06);
                }}
            """)
            rl = QHBoxLayout(rw)
            rl.setContentsMargins(6, 0, 6, 0)
            rl.setSpacing(6)

            # Icon + Name
            nl = QLabel("--")
            nl.setFont(sans_font(7, bold=True))
            nl.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent;")
            rl.addWidget(nl, stretch=4)

            # RAM readout with inline magnitude bar
            ram_cont = QWidget()
            ram_lay = QHBoxLayout(ram_cont)
            ram_lay.setContentsMargins(0, 0, 0, 0)
            ram_lay.setSpacing(4)
            ml = QLabel("--")
            ml.setFont(mono_font(7, bold=True))
            ml.setStyleSheet(f"color: {ACCENT_VIOLET.name()}; background: transparent;")
            ml.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ram_bar = QProgressBar()
            ram_bar.setRange(0, 100)
            ram_bar.setValue(0)
            ram_bar.setFixedHeight(3)
            ram_bar.setTextVisible(False)
            ram_bar.setStyleSheet(f"""
                QProgressBar {{ background: rgba(255, 255, 255, 0.08); border: none; border-radius: 1px; }}
                QProgressBar::chunk {{ background: {ACCENT_VIOLET.name()}; border-radius: 1px; }}
            """)
            ram_lay.addWidget(ml, stretch=3)
            ram_lay.addWidget(ram_bar, stretch=2)
            rl.addWidget(ram_cont, stretch=3)

            # CPU % readout with inline magnitude bar
            cpu_cont = QWidget()
            cpu_lay = QHBoxLayout(cpu_cont)
            cpu_lay.setContentsMargins(0, 0, 0, 0)
            cpu_lay.setSpacing(4)
            cl = QLabel("--")
            cl.setFont(mono_font(7, bold=True))
            cl.setStyleSheet(f"color: {ACCENT_CYAN.name()}; background: transparent;")
            cl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            cpu_bar = QProgressBar()
            cpu_bar.setRange(0, 100)
            cpu_bar.setValue(0)
            cpu_bar.setFixedHeight(3)
            cpu_bar.setTextVisible(False)
            cpu_bar.setStyleSheet(f"""
                QProgressBar {{ background: rgba(255, 255, 255, 0.08); border: none; border-radius: 1px; }}
                QProgressBar::chunk {{ background: {ACCENT_CYAN.name()}; border-radius: 1px; }}
            """)
            cpu_lay.addWidget(cl, stretch=2)
            cpu_lay.addWidget(cpu_bar, stretch=2)
            rl.addWidget(cpu_cont, stretch=2)

            self.rows_box.addWidget(rw)
            self._row_widgets.append((rw, nl, ml, ram_bar, cl, cpu_bar))
            self.proc_rows.append((nl, ml, cl))

        c_lay.addLayout(self.rows_box)
        root.addWidget(card)

    def _set_sort(self, mode: str):
        self._sort_by = mode
        self._update_sort_btn_styles()
        self._refresh_table()

    def _update_sort_btn_styles(self):
        active_style = f"""
            QPushButton {{
                color: #ffffff;
                background: rgba(34, 211, 238, 0.25);
                border: 1px solid {ACCENT_CYAN.name()};
                border-radius: 3px;
                padding: 1px 6px;
            }}
        """
        inactive_style = f"""
            QPushButton {{
                color: {TEXT_MUTED};
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 3px;
                padding: 1px 6px;
            }}
            QPushButton:hover {{
                background: rgba(255, 255, 255, 0.08);
                color: #ffffff;
            }}
        """
        self.btn_sort_ram.setStyleSheet(active_style if self._sort_by == "ram" else inactive_style)
        self.btn_sort_cpu.setStyleSheet(active_style if self._sort_by == "cpu" else inactive_style)

    def update_processes(self, procs: List[Dict[str, Any]]):
        self._raw_procs = list(procs)
        self._refresh_table()

    def _refresh_table(self):
        if not self._raw_procs:
            for rw, nl, ml, rb, cl, cb in self._row_widgets:
                nl.setText("--")
                ml.setText("--")
                cl.setText("--")
                rb.setValue(0)
                cb.setValue(0)
            return

        # Sort by active criteria descending
        if self._sort_by == "cpu":
            sorted_procs = sorted(self._raw_procs, key=lambda x: x.get("cpu_pct", 0.0), reverse=True)
        else:
            sorted_procs = sorted(self._raw_procs, key=lambda x: x.get("ram_bytes", 0), reverse=True)

        # Find max RAM for normalized inline magnitude bar
        max_ram = max(1.0, max((p.get("ram_bytes", 0) for p in sorted_procs), default=1.0))

        for idx in range(self._max_rows):
            rw, nl, ml, rb, cl, cb = self._row_widgets[idx]
            if idx < len(sorted_procs):
                p = sorted_procs[idx]
                name = p.get("name", "unknown")
                cnt = p.get("count", 1)
                cnt_str = f" ×{cnt}" if cnt > 1 else ""
                short_name = name if len(name) <= 18 else name[:16] + ".."
                nl.setText(f"{short_name}{cnt_str}")
                nl.setToolTip(f"{name} ({cnt} instance{'s' if cnt > 1 else ''})")

                # RAM
                ram_str = p.get("ram_formatted", "--")
                ml.setText(ram_str)
                ram_frac = min(100, int((p.get("ram_bytes", 0) / max_ram) * 100))
                rb.setValue(ram_frac)

                # CPU
                cpu_val = p.get("cpu_pct", 0.0)
                cl.setText(f"{cpu_val:.1f}%")
                cb.setValue(min(100, int(cpu_val)))
                rw.setVisible(True)
            else:
                nl.setText("--")
                ml.setText("--")
                cl.setText("--")
                rb.setValue(0)
                cb.setValue(0)
                if idx >= 4:
                    rw.setVisible(False)


# Backward compatibility alias
ProcessRadarWidget = ProcessTable


# ─────────────────────────────────────────────────────────────────────────────
# 8. Internet Speed Widget (Numbers only, no graph)
# ─────────────────────────────────────────────────────────────────────────────

class SpeedStat(QWidget):
    """
    Two large stat blocks: Download (↓) and Upload (↑)
    with big bold numbers, auto-scaled units (kb/s, Mb/s, MB/s),
    small direction arrows, connection name, and high-activity pulse. No graph.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._pulse_alpha = 0
        self._is_active = False

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        card = StatCard()
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(12, 10, 12, 10)
        c_lay.setSpacing(6)

        # Card Title
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        title = QLabel("INTERNET SPEED")
        title.setFont(sans_font(7, bold=True, letter_spacing=1.2))
        title.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        hdr.addWidget(title)
        hdr.addStretch()

        self.lbl_conn = QLabel("WI-FI / ETHERNET")
        self.lbl_conn.setFont(sans_font(6.5, bold=True))
        self.lbl_conn.setStyleSheet(f"color: {TEXT_DIM}; background: transparent; border: none;")
        hdr.addWidget(self.lbl_conn)
        c_lay.addLayout(hdr)

        # 2 Large Stat Blocks (Download & Upload)
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(0, 4, 0, 4)
        stats_row.setSpacing(10)

        # Block 1: Download
        b_dl = QFrame()
        b_dl.setStyleSheet("background: rgba(255, 255, 255, 0.02); border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.06);")
        v_dl = QVBoxLayout(b_dl)
        v_dl.setContentsMargins(10, 8, 10, 8)
        v_dl.setSpacing(2)

        lbl_dl_tag = QLabel("↓ DOWNLOAD")
        lbl_dl_tag.setFont(sans_font(6.5, bold=True))
        lbl_dl_tag.setStyleSheet(f"color: {ACCENT_CYAN.name()}; background: transparent; border: none;")
        v_dl.addWidget(lbl_dl_tag)

        h_dl_val = QHBoxLayout()
        h_dl_val.setContentsMargins(0, 0, 0, 0)
        h_dl_val.setSpacing(4)
        self.lbl_dl_num = QLabel("0.0")
        self.lbl_dl_num.setFont(mono_font(18, bold=True))
        self.lbl_dl_num.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent; border: none;")
        h_dl_val.addWidget(self.lbl_dl_num)

        self.lbl_dl_unit = QLabel("kb/s")
        self.lbl_dl_unit.setFont(sans_font(8, bold=True))
        self.lbl_dl_unit.setStyleSheet(f"color: {ACCENT_CYAN.name()}; background: transparent; border: none; padding-bottom: 2px;")
        h_dl_val.addWidget(self.lbl_dl_unit, alignment=Qt.AlignmentFlag.AlignBottom)
        h_dl_val.addStretch()
        v_dl.addLayout(h_dl_val)
        stats_row.addWidget(b_dl, stretch=1)

        # Block 2: Upload
        b_ul = QFrame()
        b_ul.setStyleSheet("background: rgba(255, 255, 255, 0.02); border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.06);")
        v_ul = QVBoxLayout(b_ul)
        v_ul.setContentsMargins(10, 8, 10, 8)
        v_ul.setSpacing(2)

        lbl_ul_tag = QLabel("↑ UPLOAD")
        lbl_ul_tag.setFont(sans_font(6.5, bold=True))
        lbl_ul_tag.setStyleSheet(f"color: {ACCENT_VIOLET.name()}; background: transparent; border: none;")
        v_ul.addWidget(lbl_ul_tag)

        h_ul_val = QHBoxLayout()
        h_ul_val.setContentsMargins(0, 0, 0, 0)
        h_ul_val.setSpacing(4)
        self.lbl_ul_num = QLabel("0.0")
        self.lbl_ul_num.setFont(mono_font(18, bold=True))
        self.lbl_ul_num.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent; border: none;")
        h_ul_val.addWidget(self.lbl_ul_num)

        self.lbl_ul_unit = QLabel("kb/s")
        self.lbl_ul_unit.setFont(sans_font(8, bold=True))
        self.lbl_ul_unit.setStyleSheet(f"color: {ACCENT_VIOLET.name()}; background: transparent; border: none; padding-bottom: 2px;")
        h_ul_val.addWidget(self.lbl_ul_unit, alignment=Qt.AlignmentFlag.AlignBottom)
        h_ul_val.addStretch()
        v_ul.addLayout(h_ul_val)
        stats_row.addWidget(b_ul, stretch=1)

        c_lay.addLayout(stats_row)
        root.addWidget(card)

    def update_net(self, net_info: Dict[str, Any]):
        dl_num = str(net_info.get("dl_num", "0.0"))
        dl_unit = str(net_info.get("dl_unit", "kb/s"))
        ul_num = str(net_info.get("ul_num", "0.0"))
        ul_unit = str(net_info.get("ul_unit", "kb/s"))

        # Fallback parsing if string formatted was provided
        if "dl_formatted" in net_info and ("dl_num" not in net_info):
            parts = net_info["dl_formatted"].replace("↓", "").strip().split()
            if len(parts) >= 2:
                dl_num, dl_unit = parts[0], parts[1]
        if "ul_formatted" in net_info and ("ul_num" not in net_info):
            parts = net_info["ul_formatted"].replace("↑", "").strip().split()
            if len(parts) >= 2:
                ul_num, ul_unit = parts[0], parts[1]

        self.lbl_dl_num.setText(dl_num)
        self.lbl_dl_unit.setText(dl_unit)
        self.lbl_ul_num.setText(ul_num)
        self.lbl_ul_unit.setText(ul_unit)

        conn = net_info.get("conn_name")
        if conn:
            self.lbl_conn.setText(str(conn).upper())

        # Check for high throughput burst
        dl_bps = net_info.get("dl_rate_bps", 0.0)
        ul_bps = net_info.get("ul_rate_bps", 0.0)
        if dl_bps > 500 * 1024.0 or ul_bps > 200 * 1024.0:
            self.lbl_dl_num.setStyleSheet(f"color: {ACCENT_CYAN.name()}; background: transparent; border: none;")
        else:
            self.lbl_dl_num.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent; border: none;")


# Backward compatibility alias
NetLinkWaveformWidget = SpeedStat


# ─────────────────────────────────────────────────────────────────────────────
# 9. Actions History Widget (Kept as is, restyled)
# ─────────────────────────────────────────────────────────────────────────────

class ActionsHistoryWidget(QWidget):
    """
    Live list of executed tool calls with completion checkmark, badge,
    duration chip, and dark glass card style.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._max_actions = 12
        self._actions_count = 0
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        card = StatCard()
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(12, 10, 12, 10)
        c_lay.setSpacing(6)

        # Header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        title = QLabel("ACTIONS HISTORY")
        title.setFont(sans_font(7, bold=True, letter_spacing=1.2))
        title.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        hdr.addWidget(title)
        hdr.addStretch()

        self.lbl_counter = QLabel("0 EXECUTED")
        self.lbl_counter.setFont(mono_font(6.5, bold=True))
        self.lbl_counter.setStyleSheet(f"color: {ACCENT_CYAN.name()}; background: transparent; border: none;")
        hdr.addWidget(self.lbl_counter)
        c_lay.addLayout(hdr)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFixedHeight(140)
        self.scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: transparent;
                width: 4px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.15);
                min-height: 16px;
                border-radius: 2px;
            }
        """)

        self.list_container = QWidget()
        self.list_container.setStyleSheet("background: transparent;")
        self.list_lay = QVBoxLayout(self.list_container)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(4)
        self.list_lay.addStretch()

        self.scroll.setWidget(self.list_container)
        c_lay.addWidget(self.scroll)
        root.addWidget(card)

    def add_action(self, action: Dict[str, Any]):
        self._actions_count += 1
        self.lbl_counter.setText(f"{self._actions_count} EXECUTED")

        check = action.get("checkmark", "✓")
        lbl = action.get("label", "")
        cat = action.get("category", "TO")
        dur = action.get("duration_ms", 1)
        succ = action.get("success", True)

        row = QFrame()
        row.setFixedHeight(24)
        row.setStyleSheet("""
            QFrame {
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 4px;
            }
            QFrame:hover {
                background: rgba(255, 255, 255, 0.05);
            }
        """)
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 2, 8, 2)
        h.setSpacing(6)

        c_lbl = QLabel("✓" if succ else "✗")
        c_lbl.setFont(sans_font(8, bold=True))
        c_lbl.setStyleSheet(f"color: {ACCENT_GREEN.name()};" if succ else f"color: {ACCENT_RED.name()};")
        h.addWidget(c_lbl)

        cat_lbl = QLabel(f"[{cat}]")
        cat_lbl.setFont(sans_font(6.5, bold=True))
        cat_lbl.setStyleSheet(f"color: {TEXT_DIM};")
        h.addWidget(cat_lbl)

        txt_lbl = QLabel(lbl)
        txt_lbl.setFont(sans_font(7))
        txt_lbl.setStyleSheet(f"color: {TEXT_PRIMARY};")
        h.addWidget(txt_lbl, stretch=1)

        dur_lbl = QLabel(f"{dur} ms")
        dur_lbl.setFont(mono_font(6.5, bold=True))
        dur_lbl.setStyleSheet(f"color: {ACCENT_CYAN.name()}; background: rgba(34, 211, 238, 0.12); border-radius: 3px; padding: 1px 4px;")
        h.addWidget(dur_lbl)

        self.list_lay.insertWidget(0, row)

        # Trim old rows
        if self.list_lay.count() > self._max_actions + 1:
            item = self.list_lay.takeAt(self._max_actions)
            if item and item.widget():
                item.widget().deleteLater()


# ─────────────────────────────────────────────────────────────────────────────
# 10. Standalone Compatibility Widgets (Exported for test/module compatibility)
# ─────────────────────────────────────────────────────────────────────────────

class TopStatusBarWidget(QWidget):
    """Compatibility widget representing top status pills and clock."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.lbl_clock = QLabel("00:00:00.0", self)
        self.lbl_date = QLabel("DATE", self)
        self.lbl_uptime = QLabel("UPTIME 00:00:00", self)
        self.pill_conn = QLabel("CORE ONLINE", self)
        self.pill_state = QLabel("IDLE", self)
        self.pill_mode = QLabel("HYBRID", self)
        self.pill_abort = QLabel("STOP", self)
        self.setVisible(False)

    def update_clock(self, clock_info: Dict[str, Any]):
        self.lbl_clock.setText(clock_info.get("time_str", "00:00:00.0"))
        self.lbl_date.setText(clock_info.get("date_str", ""))
        self.lbl_uptime.setText(f"UPTIME {clock_info.get('uptime_str', '00:00:00')}")

    def update_status_pill(self, status: Dict[str, Any]):
        self.pill_conn.setText(status.get("connection", "CORE ONLINE"))
        self.pill_state.setText(status.get("agent_state", "IDLE"))
        self.pill_mode.setText(status.get("brain_mode", "HYBRID"))
        self.pill_abort.setText(status.get("abort_badge", "STOP"))


class EventStreamWidget(QWidget):
    """Compatibility widget for live event stream."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.log_text = QTextEdit(self)
        self.log_text.setReadOnly(True)
        self.setVisible(False)

    def add_event(self, entry: Dict[str, Any]):
        tag = entry.get("tag", "INFO")
        msg = entry.get("message", "")
        self.log_text.append(f"[{tag}] {msg}")


class ModelBenchWidget(QWidget):
    """Compatibility widget for model benchmarks."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setVisible(False)

    def update_model(self, data: Dict[str, Any]):
        pass


class ResponseCardPerfHeader(QFrame):
    """Metadata header attached to assistant response cards."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(22)
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 0, 6, 0)
        self.lbl_model = QLabel("gemini-2.5-flash", self)
        self.lbl_link = QLabel("LINK 0 ms", self)
        self.lbl_req = QLabel("REQUESTS 0", self)
        self.lbl_act = QLabel("ACTIONS 0", self)
        self.lbl_tok = QLabel("TOKENS 0", self)
        h.addWidget(self.lbl_model)
        h.addWidget(self.lbl_link)
        h.addWidget(self.lbl_req)
        h.addWidget(self.lbl_act)
        h.addWidget(self.lbl_tok)
        h.addStretch()

    def update_perf(self, perf: Dict[str, Any]):
        self.lbl_model.setText(perf.get("model", "gemini-2.5-flash"))
        self.lbl_link.setText(f"{perf.get('latency_ms', 0)} ms")
        self.lbl_req.setText(f"REQUESTS {perf.get('requests', 0)}")
        self.lbl_act.setText(f"ACTIONS {perf.get('actions', 0)}")
        self.lbl_tok.setText(f"TOKENS {perf.get('tokens', 0)}")


# ─────────────────────────────────────────────────────────────────────────────
# 11. Master Telemetry Panel (Unified Command-Center View)
# ─────────────────────────────────────────────────────────────────────────────

class MasterTelemetryPanel(QWidget):
    """
    Redesigned, modern glassmorphic Telemetry Command Center:
    - 1. System Vitals (Radial Gauges for CPU, RAM, GPU, VRAM; Drives; Battery)
    - 2. Load, last 60 seconds (Smooth wide line chart with glow & gradient fill)
    - 3. Processes (Top 8 consumers table with inline magnitude bars, no radar)
    - 4. Internet Speed (Numbers only with auto-scaled units, no graph)
    - 5. Actions History (Live tool executions with duration chips)
    - Autonomously subscribes to TelemetryEventBus signals
    - Fully responsive across window sizes
    - Preserves all public methods, properties, and signals for backwards compatibility.
    """

    def __init__(self, main_win=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._main_win = main_win
        self._operating_mode = "Performance"
        self.setStyleSheet(f"background-color: {BG_COLOR};")
        self._build_ui()
        self._wire_bus()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(8)

        # Responsive Scroll Area to allow graceful scaling from narrow to wide widths
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: transparent;
                width: 4px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.15);
                min-height: 20px;
                border-radius: 2px;
            }
        """)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        c_lay = QVBoxLayout(content)
        c_lay.setContentsMargins(2, 2, 2, 2)
        c_lay.setSpacing(10)

        # ── 1. System Vitals (Radial Gauges, Drives, Battery) ──
        self.vitals_widget = SystemVitalsWidget(content)
        c_lay.addWidget(self.vitals_widget)

        # ── 2. Load, Last 60 Seconds: Visual Centerpiece Line Chart ──
        self.load_chart = GlowLineChart(content)
        c_lay.addWidget(self.load_chart)

        # ── 3. Bottom Grid: Processes Table (Left) + Internet Speed & Actions (Right) ──
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(10)

        # Left Column: Processes Table (Top 8 Consumers, no radar)
        self.process_table = ProcessTable(content)
        self.radar_widget = self.process_table  # Backwards compatibility alias
        bottom_row.addWidget(self.process_table, stretch=5)

        # Right Column: Internet Speed + Actions History
        col_right = QVBoxLayout()
        col_right.setContentsMargins(0, 0, 0, 0)
        col_right.setSpacing(10)

        self.net_widget = SpeedStat(content)
        col_right.addWidget(self.net_widget)

        self.actions_history = ActionsHistoryWidget(content)
        col_right.addWidget(self.actions_history)

        bottom_row.addLayout(col_right, stretch=4)
        c_lay.addLayout(bottom_row)

        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

        # Retain hidden surrogate references for compatibility with any external caller
        self.top_status = TopStatusBarWidget(self)
        self.event_stream = EventStreamWidget(self)
        self.model_bench = ModelBenchWidget(self)

    def _wire_bus(self):
        bus = get_event_bus()
        bus.vitals_updated.connect(self.vitals_widget.update_vitals)
        bus.load_history_updated.connect(self.load_chart.update_history)
        bus.processes_updated.connect(self.process_table.update_processes)
        bus.net_updated.connect(self.net_widget.update_net)
        bus.clock_updated.connect(self.top_status.update_clock)
        bus.status_pill_updated.connect(self.top_status.update_status_pill)
        bus.event_logged.connect(self.event_stream.add_event)
        bus.model_bench_logged.connect(self.model_bench.update_model)
        bus.action_logged.connect(self.actions_history.add_action)

    # ── Compatibility Properties & Methods ────────────────────────────────────

    @property
    def _mode(self) -> str:
        return getattr(self, "_operating_mode", "Performance")

    @_mode.setter
    def _mode(self, val: str):
        self._operating_mode = str(val)

    def set_operating_mode(self, mode: str):
        self._operating_mode = str(mode)

    @property
    def lbl_cpu_usage(self):
        return self.vitals_widget.lbl_cpu_val

    @property
    def lbl_gpu_usage(self):
        return self.vitals_widget.lbl_gpu_val

    @property
    def lbl_cpu_temp(self):
        return self.vitals_widget.lbl_gpu_temp

    @property
    def bar_cpu(self):
        return self.vitals_widget.bar_cpu

    @property
    def bar_gpu(self):
        return self.vitals_widget.bar_gpu

    def update_metrics(self, snap: Dict[str, Any]):
        """Legacy compatibility slot for timer loops and unit tests."""
        cpu = snap.get("cpu", 0.0)
        gpu = snap.get("gpu", -1.0)
        tmp = snap.get("tmp", -1.0)

        # Update vitals
        vitals_payload: Dict[str, Any] = {
            "cpu_pct": cpu,
            "gpu_pct": gpu,
            "gpu_temp_c": tmp,
        }
        self.vitals_widget.update_vitals(vitals_payload)

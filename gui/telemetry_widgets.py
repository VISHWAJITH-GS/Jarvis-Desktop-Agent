"""
gui/telemetry_widgets.py - Hardware-Accelerated PyQt6 Telemetry Visual Components for JARVIS

Includes:
1. SystemVitalsWidget: Labeled rows, numeric readouts, slim horizontal progress bars with semantic color thresholds.
2. RollingLoadChartWidget: QPainter multi-series line chart (CPU, MEM, GPU) over 60s with gridlines & legend.
3. ProcessRadarWidget: Concentric radar sweep & target blips paired with Top 5 process memory/CPU table.
4. NetLinkWaveformWidget: Real-time dual sparkline showing download and upload throughput (kb/s / MB/s).
5. EventStreamWidget: Monospace auto-scrolling live event stream log feed.
6. ModelBenchWidget: Compact benchmark & latency diagnostic cards for active models.
7. ActionsHistoryWidget: Live list of executed tool calls with completion checkmark, badge, and duration.
8. TopStatusBarWidget: Top status bar with live state pills (connection, state, brain mode, abort badge, clock/uptime).
9. ResponseCardPerfHeader: Metadata performance header for assistant response cards.
10. MasterTelemetryPanel: Comprehensive command-center layout combining all telemetry widgets.
"""

from __future__ import annotations

import collections
import datetime
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
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
# 1. System Vitals Widget
# ─────────────────────────────────────────────────────────────────────────────

class SystemVitalsWidget(QWidget):
    """
    Renders CPU, RAM, GPU/VRAM, Storage partitions, and Battery vitals
    with numeric readouts and slim semantic progress bars.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._disk_rows: Dict[str, Tuple[QLabel, QProgressBar]] = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        h_icon = QLabel("◈")
        h_icon.setFont(keena_font(9, bold=True))
        h_icon.setStyleSheet(f"color: {C.PRI};")
        hdr.addWidget(h_icon)
        h_title = QLabel("SYSTEM VITALS")
        h_title.setFont(keena_font(9, bold=True, spacing=1.2))
        h_title.setStyleSheet("color: #ffffff;")
        hdr.addWidget(h_title)
        hdr.addStretch()
        root.addLayout(hdr)

        self.cards_container = QVBoxLayout()
        self.cards_container.setSpacing(6)
        root.addLayout(self.cards_container)

        # 1. CPU Card
        c_cpu, self.lbl_cpu_val, self.bar_cpu, sub_cpu = self._make_vital_card("CPU", "0.0 %")
        self.lbl_cpu_freq = QLabel("0 MHz")
        self.lbl_cpu_freq.setFont(keena_font(7))
        self.lbl_cpu_freq.setStyleSheet(f"color: {C.TEXT_DIM};")
        sub_cpu.addWidget(self.lbl_cpu_freq)
        self.cards_container.addWidget(c_cpu)

        # 2. RAM Card
        c_ram, self.lbl_ram_val, self.bar_ram, sub_ram = self._make_vital_card("MEMORY (RAM)", "0.0 / 0.0 GB")
        self.cards_container.addWidget(c_ram)

        # 3. GPU Card
        c_gpu, self.lbl_gpu_val, self.bar_gpu, sub_gpu = self._make_vital_card("GPU UTIL", "0 %")
        self.lbl_gpu_temp = QLabel("-- °C")
        self.lbl_gpu_temp.setFont(keena_font(7))
        self.lbl_gpu_temp.setStyleSheet(f"color: {C.TEXT_DIM};")
        sub_gpu.addWidget(self.lbl_gpu_temp)
        self.cards_container.addWidget(c_gpu)

        # 4. VRAM Card
        c_vram, self.lbl_vram_val, self.bar_vram, sub_vram = self._make_vital_card("DEDICATED VRAM", "0.0 / 0.0 GB")
        self.cards_container.addWidget(c_vram)

        # 5. Storage Card (Container for dynamic drives)
        self.c_storage = QFrame()
        self.c_storage.setStyleSheet(f"""
            QFrame {{
                background: rgba(14, 5, 12, 0.85);
                border: 1px solid rgba(255, 30, 66, 0.25);
                border-radius: 4px;
            }}
        """)
        self.v_storage = QVBoxLayout(self.c_storage)
        self.v_storage.setContentsMargins(8, 6, 8, 6)
        self.v_storage.setSpacing(4)
        s_hdr = QLabel("STORAGE DRIVES")
        s_hdr.setFont(keena_font(7, bold=True, spacing=1.0))
        s_hdr.setStyleSheet(f"color: {C.PRI_DIM};")
        self.v_storage.addWidget(s_hdr)
        self.cards_container.addWidget(self.c_storage)

        # 6. Battery Card
        self.c_bat, self.lbl_bat_val, self.bar_bat, _ = self._make_vital_card("BATTERY / POWER", "N/A")
        self.cards_container.addWidget(self.c_bat)

        root.addStretch()

    def _make_vital_card(self, title: str, init_val: str):
        c = QFrame()
        c.setStyleSheet(f"""
            QFrame {{
                background: rgba(14, 5, 12, 0.85);
                border: 1px solid rgba(255, 30, 66, 0.25);
                border-radius: 4px;
            }}
        """)
        v = QVBoxLayout(c)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(3)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        tl = QLabel(title)
        tl.setFont(keena_font(7, bold=True, spacing=1.0))
        tl.setStyleSheet(f"color: {C.PRI_DIM};")
        hdr.addWidget(tl)
        hdr.addStretch()

        vl = QLabel(init_val)
        vl.setFont(keena_font(8, bold=True))
        vl.setStyleSheet("color: #ffffff;")
        hdr.addWidget(vl)
        v.addLayout(hdr)

        bar = QProgressBar()
        bar.setFixedHeight(4)
        bar.setTextVisible(False)
        bar.setRange(0, 100)
        bar.setValue(0)
        self._apply_bar_style(bar, 0)
        v.addWidget(bar)

        sub_lay = QHBoxLayout()
        sub_lay.setContentsMargins(0, 0, 0, 0)
        v.addLayout(sub_lay)

        return c, vl, bar, sub_lay

    def _apply_bar_style(self, bar: QProgressBar, pct: float):
        if pct > 90:
            col = "#ff1744"  # Critical red
        elif pct >= 75:
            col = "#ffb300"  # Amber
        else:
            col = "#00e5ff"  # Cyan neon
        bar.setStyleSheet(f"""
            QProgressBar {{
                background: rgba(255, 255, 255, 0.08);
                border: none;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background: {col};
                border-radius: 2px;
            }}
        """)

    def update_vitals(self, snap: Dict[str, Any]):
        # CPU
        cpu_p = snap.get("cpu_pct", 0.0)
        self.lbl_cpu_val.setText(f"{cpu_p:.1f} %")
        self.bar_cpu.setValue(int(cpu_p))
        self._apply_bar_style(self.bar_cpu, cpu_p)
        cf = snap.get("cpu_freq_mhz", 0.0)
        if cf > 0:
            self.lbl_cpu_freq.setText(f"{int(cf)} MHz")

        # RAM
        mem_fmt = snap.get("mem_formatted", "0.0 / 0.0 GB")
        mem_p = snap.get("mem_pct", 0.0)
        self.lbl_ram_val.setText(f"{mem_fmt} ({int(mem_p)}%)")
        self.bar_ram.setValue(int(mem_p))
        self._apply_bar_style(self.bar_ram, mem_p)

        # GPU
        gpu_p = snap.get("gpu_pct", -1.0)
        gpu_t = snap.get("gpu_temp_c", -1.0)
        if gpu_p >= 0:
            self.lbl_gpu_val.setText(f"{int(gpu_p)} %")
            self.bar_gpu.setValue(int(gpu_p))
            self._apply_bar_style(self.bar_gpu, gpu_p)
            self.lbl_gpu_temp.setText(f"{int(gpu_t)} °C" if gpu_t > 0 else "")
        else:
            self.lbl_gpu_val.setText("N/A")
            self.bar_gpu.setValue(0)
            self._apply_bar_style(self.bar_gpu, 0)
            self.lbl_gpu_temp.setText("")

        # VRAM
        v_used = snap.get("gpu_vram_used_gb", -1.0)
        v_tot = snap.get("gpu_vram_total_gb", -1.0)
        v_pct = snap.get("gpu_vram_pct", -1.0)
        if v_used >= 0 and v_tot > 0:
            self.lbl_vram_val.setText(f"{v_used:.1f} / {v_tot:.1f} GB ({int(v_pct)}%)")
            self.bar_vram.setValue(int(v_pct))
            self._apply_bar_style(self.bar_vram, v_pct)
        else:
            self.lbl_vram_val.setText("N/A")
            self.bar_vram.setValue(0)
            self._apply_bar_style(self.bar_vram, 0)

        # Storage
        disks = snap.get("disks", [])
        for d in disks:
            mnt = d.get("mount", "")
            lbl_name = d.get("label", mnt)
            fmt = d.get("formatted", "")
            pct = d.get("pct", 0.0)

            if mnt not in self._disk_rows:
                row_w = QWidget()
                row_l = QVBoxLayout(row_w)
                row_l.setContentsMargins(0, 2, 0, 2)
                row_l.setSpacing(2)

                r_top = QHBoxLayout()
                r_top.setContentsMargins(0, 0, 0, 0)
                d_lbl = QLabel(f"DISK {lbl_name}")
                d_lbl.setFont(keena_font(7, bold=True))
                d_lbl.setStyleSheet("color: #ffffff;")
                r_top.addWidget(d_lbl)
                r_top.addStretch()

                v_lbl = QLabel(fmt)
                v_lbl.setFont(keena_font(7))
                v_lbl.setStyleSheet(f"color: {C.TEXT_DIM};")
                r_top.addWidget(v_lbl)
                row_l.addLayout(r_top)

                pb = QProgressBar()
                pb.setFixedHeight(3)
                pb.setTextVisible(False)
                pb.setRange(0, 100)
                row_l.addWidget(pb)

                self.v_storage.addWidget(row_w)
                self._disk_rows[mnt] = (v_lbl, pb)

            v_label, p_bar = self._disk_rows[mnt]
            v_label.setText(f"{fmt} ({int(pct)}%)")
            p_bar.setValue(int(pct))
            self._apply_bar_style(p_bar, pct)

        # Battery
        bat_fmt = snap.get("battery_formatted", "N/A")
        bat_p = snap.get("battery_pct")
        if bat_p is not None:
            self.c_bat.setVisible(True)
            self.lbl_bat_val.setText(bat_fmt)
            self.bar_bat.setValue(int(bat_p))
            self._apply_bar_style(self.bar_bat, 100 - bat_p)
        else:
            self.c_bat.setVisible(False)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Rolling Load Chart Widget (LOAD – 60 S)
# ─────────────────────────────────────────────────────────────────────────────

class RollingLoadChartWidget(QWidget):
    """
    Hardware-accelerated QPainter multi-series line graph.
    Plots CPU, MEM, and GPU load over the last 60 seconds.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(140)
        self.setStyleSheet("background: transparent;")
        self._history: List[Tuple[float, float, float, float]] = []  # (t, cpu, mem, gpu)
        self._latest_cpu = 0.0
        self._latest_mem = 0.0
        self._latest_gpu = 0.0

    def update_history(self, samples: List[Tuple[float, float, float, float]]):
        self._history = list(samples)
        if self._history:
            _, self._latest_cpu, self._latest_mem, self._latest_gpu = self._history[-1]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        # Background card
        card_rect = QRectF(0, 0, w, h)
        painter.fillRect(card_rect, QColor(14, 5, 12, 210))
        painter.setPen(QPen(QColor(255, 30, 66, 60), 1))
        painter.drawRoundedRect(card_rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

        # Header with legend
        painter.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        painter.setPen(QColor(C.PRI))
        painter.drawText(10, 16, "LOAD – 60 S")

        # Swatches
        swatch_x = w - 210
        # CPU
        painter.setBrush(QBrush(QColor("#00e5ff")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(swatch_x, 8, 6, 6)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(swatch_x + 10, 15, f"CPU {int(self._latest_cpu)}%")

        # MEM
        swatch_x += 70
        painter.setBrush(QBrush(QColor("#FF1E42")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(swatch_x, 8, 6, 6)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(swatch_x + 10, 15, f"MEM {int(self._latest_mem)}%")

        # GPU
        swatch_x += 70
        painter.setBrush(QBrush(QColor("#ffb300")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(swatch_x, 8, 6, 6)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(swatch_x + 10, 15, f"GPU {int(self._latest_gpu)}%")

        # Plot area
        top_y = 26.0
        bot_y = h - 16.0
        left_x = 10.0
        right_x = w - 10.0
        plot_w = right_x - left_x
        plot_h = bot_y - top_y

        if plot_w <= 10 or plot_h <= 10:
            return

        # Gridlines (25%, 50%, 75%, 100%)
        grid_pen = QPen(QColor(255, 255, 255, 18), 1, Qt.PenStyle.DashLine)
        painter.setPen(grid_pen)
        for pct in (0.25, 0.50, 0.75, 1.0):
            gy = bot_y - (plot_h * pct)
            painter.drawLine(QPointF(left_x, gy), QPointF(right_x, gy))

        if len(self._history) < 2:
            return

        # Map points
        n = len(self._history)
        step_x = plot_w / max(1, 60 - 1)
        start_x = right_x - (n - 1) * step_x

        def _make_path(series_idx: int) -> Tuple[QPainterPath, QPolygonF]:
            path = QPainterPath()
            poly = QPolygonF()
            for i, item in enumerate(self._history):
                val = max(0.0, min(100.0, float(item[series_idx])))
                px = start_x + (i * step_x)
                py = bot_y - (val / 100.0 * plot_h)
                pt = QPointF(px, py)
                if i == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
                poly.append(pt)
            return path, poly

        # Draw CPU series (Cyan)
        path_cpu, poly_cpu = _make_path(1)
        painter.setPen(QPen(QColor("#00e5ff"), 1.8))
        painter.drawPath(path_cpu)

        # Draw MEM series (Neon Crimson)
        path_mem, poly_mem = _make_path(2)
        painter.setPen(QPen(QColor("#FF1E42"), 1.8))
        painter.drawPath(path_mem)

        # Draw GPU series (Amber)
        path_gpu, poly_gpu = _make_path(3)
        painter.setPen(QPen(QColor("#ffb300"), 1.8))
        painter.drawPath(path_gpu)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Process Radar Widget (PROCESS RADAR & TOP 5)
# ─────────────────────────────────────────────────────────────────────────────

class RadarCanvas(QWidget):
    """Concentric animated radar sweep and glowing process blips."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(140, 140)
        self._sweep_angle = 0.0
        self._procs: List[Dict[str, Any]] = []

        # Sweep animation timer (40ms ~ 25fps)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_sweep)
        self._timer.start(40)

    def _tick_sweep(self):
        self._sweep_angle = (self._sweep_angle + 3.0) % 360.0
        self.update()

    def set_processes(self, procs: List[Dict[str, Any]]):
        self._procs = list(procs)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        max_r = min(cx, cy) - 8.0

        # Radar background circle
        painter.setBrush(QBrush(QColor(10, 3, 10, 220)))
        painter.setPen(QPen(QColor(255, 30, 66, 120), 1))
        painter.drawEllipse(QPointF(cx, cy), max_r, max_r)

        # Concentric rings
        ring_pen = QPen(QColor(255, 30, 66, 45), 1, Qt.PenStyle.DashLine)
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for f in (0.33, 0.66, 1.0):
            painter.drawEllipse(QPointF(cx, cy), max_r * f, max_r * f)

        # Crosshairs
        cross_pen = QPen(QColor(255, 30, 66, 50), 1)
        painter.setPen(cross_pen)
        painter.drawLine(QPointF(cx - max_r, cy), QPointF(cx + max_r, cy))
        painter.drawLine(QPointF(cx, cy - max_r), QPointF(cx, cy + max_r))

        # Rotating Sweep Line
        sweep_rad = math.radians(self._sweep_angle)
        sx = cx + max_r * math.cos(sweep_rad)
        sy = cy + max_r * math.sin(sweep_rad)

        sweep_pen = QPen(QColor("#FF1E42"), 1.5)
        painter.setPen(sweep_pen)
        painter.drawLine(QPointF(cx, cy), QPointF(sx, sy))

        # Target blips
        painter.setFont(QFont("Montserrat", 6, QFont.Weight.Bold))
        for p in self._procs:
            ang = p.get("angle_rad", 0.0)
            r_norm = p.get("radius_norm", 0.5)
            bx = cx + (max_r * r_norm) * math.cos(ang)
            by = cy + (max_r * r_norm) * math.sin(ang)

            # Glow
            painter.setBrush(QBrush(QColor(0, 229, 255, 80)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(bx, by), 5, 5)

            # Center Dot
            painter.setBrush(QBrush(QColor("#00e5ff")))
            painter.drawEllipse(QPointF(bx, by), 2.5, 2.5)


class ProcessRadarWidget(QWidget):
    """
    Combines circular concentric radar with Top 5 process memory/CPU table.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        h_icon = QLabel("◎")
        h_icon.setFont(keena_font(9, bold=True))
        h_icon.setStyleSheet(f"color: {C.PRI};")
        hdr.addWidget(h_icon)
        h_title = QLabel("PROCESS RADAR & TOP CONSUMERS")
        h_title.setFont(keena_font(9, bold=True, spacing=1.2))
        h_title.setStyleSheet("color: #ffffff;")
        hdr.addWidget(h_title)
        hdr.addStretch()
        root.addLayout(hdr)

        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: rgba(14, 5, 12, 0.85);
                border: 1px solid rgba(255, 30, 66, 0.25);
                border-radius: 4px;
            }}
        """)
        c_lay = QHBoxLayout(card)
        c_lay.setContentsMargins(8, 8, 8, 8)
        c_lay.setSpacing(10)

        # Radar Canvas
        self.radar = RadarCanvas()
        c_lay.addWidget(self.radar)

        # Top 5 Table
        t_box = QVBoxLayout()
        t_box.setSpacing(3)

        th = QHBoxLayout()
        th.setContentsMargins(0, 0, 0, 0)
        lbl_p = QLabel("PROCESS")
        lbl_p.setFont(keena_font(7, bold=True))
        lbl_p.setStyleSheet(f"color: {C.PRI_DIM};")
        th.addWidget(lbl_p, stretch=3)
        lbl_m = QLabel("RAM")
        lbl_m.setFont(keena_font(7, bold=True))
        lbl_m.setStyleSheet(f"color: {C.PRI_DIM};")
        th.addWidget(lbl_m, stretch=2)
        lbl_c = QLabel("CPU")
        lbl_c.setFont(keena_font(7, bold=True))
        lbl_c.setStyleSheet(f"color: {C.PRI_DIM};")
        th.addWidget(lbl_c, stretch=1)
        t_box.addLayout(th)

        self.proc_rows = []
        for _ in range(5):
            r = QHBoxLayout()
            r.setContentsMargins(0, 0, 0, 0)
            n_lbl = QLabel("--")
            n_lbl.setFont(keena_font(7))
            n_lbl.setStyleSheet("color: #ffffff;")
            m_lbl = QLabel("--")
            m_lbl.setFont(keena_font(7, bold=True))
            m_lbl.setStyleSheet("color: #00e5ff;")
            c_lbl = QLabel("--")
            c_lbl.setFont(keena_font(7))
            c_lbl.setStyleSheet(f"color: {C.TEXT_DIM};")

            r.addWidget(n_lbl, stretch=3)
            r.addWidget(m_lbl, stretch=2)
            r.addWidget(c_lbl, stretch=1)
            t_box.addLayout(r)
            self.proc_rows.append((n_lbl, m_lbl, c_lbl))

        c_lay.addLayout(t_box, stretch=1)
        root.addWidget(card)

    def update_processes(self, procs: List[Dict[str, Any]]):
        self.radar.set_processes(procs)
        for idx in range(5):
            n_lbl, m_lbl, c_lbl = self.proc_rows[idx]
            if idx < len(procs):
                p = procs[idx]
                name = p.get("name", "unknown")
                if len(name) > 16:
                    name = name[:14] + ".."
                cnt = p.get("count", 1)
                cnt_str = f" (x{cnt})" if cnt > 1 else ""
                n_lbl.setText(f"{name}{cnt_str}")
                m_lbl.setText(p.get("ram_formatted", "--"))
                c_lbl.setText(f"{p.get('cpu_pct', 0.0):.1f}%")
            else:
                n_lbl.setText("--")
                m_lbl.setText("--")
                c_lbl.setText("--")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Net Link Waveform Widget (NET LINK)
# ─────────────────────────────────────────────────────────────────────────────

class NetLinkWaveformWidget(QWidget):
    """
    Bi-directional / dual-series real-time sparkline showing
    download (↓ cyan) and upload (↑ crimson) throughput.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setStyleSheet("background: transparent;")
        self._history: List[Tuple[float, float, float]] = []  # (t, dl, ul)
        self._dl_fmt = "↓ 0 kb/s"
        self._ul_fmt = "↑ 0 kb/s"

    def update_net(self, net_info: Dict[str, Any]):
        self._dl_fmt = net_info.get("dl_formatted", "↓ 0 kb/s")
        self._ul_fmt = net_info.get("ul_formatted", "↑ 0 kb/s")
        self._history = list(net_info.get("net_history", []))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        card_rect = QRectF(0, 0, w, h)
        painter.fillRect(card_rect, QColor(14, 5, 12, 210))
        painter.setPen(QPen(QColor(255, 30, 66, 60), 1))
        painter.drawRoundedRect(card_rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

        # Header
        painter.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        painter.setPen(QColor(C.PRI))
        painter.drawText(10, 16, "NET LINK THROUGHPUT")

        # Live values
        painter.setPen(QColor("#00e5ff"))
        painter.drawText(w - 170, 16, self._dl_fmt)
        painter.setPen(QColor("#FF1E42"))
        painter.drawText(w - 85, 16, self._ul_fmt)

        top_y = 26.0
        bot_y = h - 14.0
        left_x = 10.0
        right_x = w - 10.0
        plot_w = right_x - left_x
        plot_h = bot_y - top_y

        if plot_w <= 10 or plot_h <= 10 or len(self._history) < 2:
            return

        # Calculate max rate for dynamic scaling
        max_rate = 1024.0 * 50.0  # minimum 50 KB/s ceiling
        for _, dl, ul in self._history:
            if dl > max_rate:
                max_rate = dl
            if ul > max_rate:
                max_rate = ul

        n = len(self._history)
        step_x = plot_w / max(1, 60 - 1)
        start_x = right_x - (n - 1) * step_x

        path_dl = QPainterPath()
        path_ul = QPainterPath()

        for i, (_, dl, ul) in enumerate(self._history):
            px = start_x + (i * step_x)
            py_dl = bot_y - (dl / max_rate * plot_h)
            py_ul = bot_y - (ul / max_rate * plot_h)

            if i == 0:
                path_dl.moveTo(px, py_dl)
                path_ul.moveTo(px, py_ul)
            else:
                path_dl.lineTo(px, py_dl)
                path_ul.lineTo(px, py_ul)

        # Draw Download (Cyan)
        painter.setPen(QPen(QColor("#00e5ff"), 1.8))
        painter.drawPath(path_dl)

        # Draw Upload (Crimson)
        painter.setPen(QPen(QColor("#FF1E42"), 1.8))
        painter.drawPath(path_ul)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Live Event Stream Widget (EVENT STREAM)
# ─────────────────────────────────────────────────────────────────────────────

class EventStreamWidget(QWidget):
    """
    Real-time monospace structured event log feed matching the HUD theme.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        h_icon = QLabel("⚡")
        h_icon.setFont(keena_font(9, bold=True))
        h_icon.setStyleSheet(f"color: {C.PRI};")
        hdr.addWidget(h_icon)
        h_title = QLabel("LIVE EVENT STREAM")
        h_title.setFont(keena_font(9, bold=True, spacing=1.2))
        h_title.setStyleSheet("color: #ffffff;")
        hdr.addWidget(h_title)
        hdr.addStretch()
        root.addLayout(hdr)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 8))
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(14, 5, 12, 0.90);
                border: 1px solid rgba(255, 30, 66, 0.25);
                border-radius: 4px;
                color: #e0e0e0;
                padding: 6px;
            }}
        """)
        root.addWidget(self.log_text)

    def add_event(self, entry: Dict[str, Any]):
        ts = entry.get("timestamp", "")
        tag = entry.get("tag", "INFO")
        msg = entry.get("message", "")
        lvl = entry.get("level", "info")

        if lvl == "error" or tag == "FAULT":
            tag_col = "#ff1744"
            msg_col = "#ff5252"
        elif tag == "ROUTE":
            tag_col = "#00e5ff"
            msg_col = "#80d8ff"
        elif tag == "TOOL":
            tag_col = "#ffb300"
            msg_col = "#ffe57f"
        elif tag == "STATE":
            tag_col = "#e040fb"
            msg_col = "#ea80fc"
        elif tag == "RECV":
            tag_col = "#00e676"
            msg_col = "#b9f6ca"
        else:
            tag_col = C.PRI
            msg_col = "#ffffff"

        html = f"""
            <div style="margin-bottom: 3px; font-family: Consolas, monospace; font-size: 8pt;">
                <span style="color: {C.TEXT_DIM};">{ts}</span>
                <span style="color: {tag_col}; font-weight: bold;"> [{tag}]</span>
                <span style="color: {msg_col};"> {msg}</span>
            </div>
        """
        self.log_text.append(html)
        # Auto-scroll to end
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())


# ─────────────────────────────────────────────────────────────────────────────
# 6. Model Bench Widget (MODEL BENCH)
# ─────────────────────────────────────────────────────────────────────────────

class ModelBenchWidget(QWidget):
    """
    Diagnostic cards for configured models: tier, latency, and pass ratio.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._cards: Dict[str, Tuple[QLabel, QLabel, QLabel]] = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        h_icon = QLabel("◆")
        h_icon.setFont(keena_font(9, bold=True))
        h_icon.setStyleSheet(f"color: {C.PRI};")
        hdr.addWidget(h_icon)
        h_title = QLabel("MODEL BENCHMARK & LATENCY")
        h_title.setFont(keena_font(9, bold=True, spacing=1.2))
        h_title.setStyleSheet("color: #ffffff;")
        hdr.addWidget(h_title)
        hdr.addStretch()
        root.addLayout(hdr)

        self.container = QVBoxLayout()
        self.container.setSpacing(4)
        root.addLayout(self.container)

        # Pre-populate default models
        defaults = [
            ("gemini-2.5-flash", "FAST", "0.7s", "16/16 pass"),
            ("gemini-3.6-flash", "FAST", "1.3s", "15/16 pass"),
            ("gemini-2.5-pro", "SMART", "4.0s", "14/16 pass"),
        ]
        for name, tier, lat, ratio in defaults:
            self._add_or_update_model(name, tier, lat, ratio)

    def _add_or_update_model(self, name: str, tier: str, lat: str, ratio: str):
        if name in self._cards:
            lbl_tier, lbl_lat, lbl_rat = self._cards[name]
            lbl_tier.setText(f"[ {tier} ]")
            lbl_lat.setText(lat)
            lbl_rat.setText(ratio)
            return

        c = QFrame()
        c.setStyleSheet(f"""
            QFrame {{
                background: rgba(14, 5, 12, 0.85);
                border: 1px solid rgba(255, 30, 66, 0.25);
                border-radius: 4px;
            }}
        """)
        h = QHBoxLayout(c)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        nl = QLabel(name)
        nl.setFont(keena_font(7, bold=True))
        nl.setStyleSheet("color: #ffffff;")
        h.addWidget(nl, stretch=3)

        tl = QLabel(f"[ {tier} ]")
        tl.setFont(keena_font(7, bold=True))
        tl.setStyleSheet("color: #00e5ff;" if tier == "FAST" else "color: #e040fb;")
        h.addWidget(tl, stretch=1)

        ll = QLabel(lat)
        ll.setFont(keena_font(7, bold=True))
        ll.setStyleSheet("color: #ffb300;")
        h.addWidget(ll, stretch=1)

        rl = QLabel(ratio)
        rl.setFont(keena_font(7))
        rl.setStyleSheet(f"color: {C.TEXT_DIM};")
        h.addWidget(rl, stretch=1)

        self.container.addWidget(c)
        self._cards[name] = (tl, ll, rl)

    def update_model(self, data: Dict[str, Any]):
        name = data.get("model", "")
        if name:
            self._add_or_update_model(
                name,
                data.get("tier", "FAST"),
                data.get("avg_latency", "--"),
                data.get("pass_ratio", "--"),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Actions History Widget (ACTIONS)
# ─────────────────────────────────────────────────────────────────────────────

class ActionsHistoryWidget(QWidget):
    """
    Live list of executed tool calls with completion checkmark, badge, and duration.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        h_icon = QLabel("✓")
        h_icon.setFont(keena_font(9, bold=True))
        h_icon.setStyleSheet("color: #00e676;")
        hdr.addWidget(h_icon)
        h_title = QLabel("ACTIONS HISTORY")
        h_title.setFont(keena_font(9, bold=True, spacing=1.2))
        h_title.setStyleSheet("color: #ffffff;")
        hdr.addWidget(h_title)
        hdr.addStretch()
        root.addLayout(hdr)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("background: transparent; border: none;")

        self.list_container = QWidget()
        self.list_container.setStyleSheet("background: transparent;")
        self.list_lay = QVBoxLayout(self.list_container)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(4)
        self.list_lay.addStretch()

        self.scroll.setWidget(self.list_container)
        root.addWidget(self.scroll)

    def add_action(self, action: Dict[str, Any]):
        check = action.get("checkmark", "✓")
        lbl = action.get("label", "")
        cat = action.get("category", "TO")
        dur = action.get("duration_ms", 1)
        succ = action.get("success", True)

        row = QFrame()
        row.setStyleSheet(f"""
            QFrame {{
                background: rgba(14, 5, 12, 0.85);
                border: 1px solid rgba(255, 30, 66, 0.20);
                border-radius: 3px;
            }}
        """)
        h = QHBoxLayout(row)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(6)

        c_lbl = QLabel(check)
        c_lbl.setFont(keena_font(8, bold=True))
        c_lbl.setStyleSheet("color: #00e676;" if succ else "color: #ff1744;")
        h.addWidget(c_lbl)

        cat_lbl = QLabel(f"[{cat}]")
        cat_lbl.setFont(keena_font(7, bold=True))
        cat_lbl.setStyleSheet(f"color: {C.PRI_DIM};")
        h.addWidget(cat_lbl)

        txt_lbl = QLabel(lbl)
        txt_lbl.setFont(keena_font(7))
        txt_lbl.setStyleSheet("color: #ffffff;")
        h.addWidget(txt_lbl, stretch=1)

        dur_lbl = QLabel(f"{dur} ms")
        dur_lbl.setFont(keena_font(7, bold=True))
        dur_lbl.setStyleSheet("color: #00e5ff;")
        h.addWidget(dur_lbl)

        # Insert at the top of the list
        self.list_lay.insertWidget(0, row)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Top Status Bar Widget
# ─────────────────────────────────────────────────────────────────────────────

class TopStatusBarWidget(QWidget):
    """
    Top status bar with live state pills:
    [ CORE ONLINE ] [ IDLE ] [ HYBRID ] [ STOP CTRL+ALT+BACKSPACE ]
    and live wall-clock with tenths, uppercase date, and session uptime.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setStyleSheet(f"""
            QWidget {{
                background: rgba(10, 4, 6, 0.95);
                border-bottom: 1px solid rgba(255, 30, 66, 0.35);
            }}
        """)
        self._build_ui()

    def _build_ui(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)

        def _pill(text: str, color: str, bg: str):
            lbl = QLabel(text)
            lbl.setFont(keena_font(7, bold=True, spacing=1.0))
            lbl.setStyleSheet(f"""
                QLabel {{
                    color: {color};
                    background: {bg};
                    border: 1px solid {color};
                    border-radius: 3px;
                    padding: 2px 7px;
                }}
            """)
            return lbl

        # State pills
        self.pill_conn = _pill("CORE ONLINE", "#00e676", "rgba(0, 230, 118, 0.12)")
        self.pill_state = _pill("IDLE", "#00e5ff", "rgba(0, 229, 255, 0.12)")
        self.pill_mode = _pill("HYBRID", "#e040fb", "rgba(224, 64, 251, 0.12)")
        self.pill_abort = _pill("STOP CTRL+ALT+BACKSPACE", "#ff1744", "rgba(255, 23, 68, 0.18)")

        lay.addWidget(self.pill_conn)
        lay.addWidget(self.pill_state)
        lay.addWidget(self.pill_mode)
        lay.addWidget(self.pill_abort)

        lay.addStretch()

        # Clock & Uptime readouts
        self.lbl_clock = QLabel("00:00:00.0")
        self.lbl_clock.setFont(keena_font(8, bold=True))
        self.lbl_clock.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        lay.addWidget(self.lbl_clock)

        self.lbl_date = QLabel("SAT, 28 NOV, 2026")
        self.lbl_date.setFont(keena_font(7))
        self.lbl_date.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        lay.addWidget(self.lbl_date)

        self.lbl_uptime = QLabel("UPTIME 00:00:00")
        self.lbl_uptime.setFont(keena_font(7, bold=True))
        self.lbl_uptime.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; border: none;")
        lay.addWidget(self.lbl_uptime)

    def update_clock(self, clock_info: Dict[str, Any]):
        self.lbl_clock.setText(clock_info.get("time_str", "00:00:00.0"))
        self.lbl_date.setText(clock_info.get("date_str", ""))
        self.lbl_uptime.setText(f"UPTIME {clock_info.get('uptime_str', '00:00:00')}")

    def update_status_pill(self, status: Dict[str, Any]):
        conn = status.get("connection", "CORE ONLINE")
        self.pill_conn.setText(conn)
        if "ONLINE" in conn:
            self.pill_conn.setStyleSheet("color: #00e676; background: rgba(0, 230, 118, 0.12); border: 1px solid #00e676; border-radius: 3px; padding: 2px 7px;")
        else:
            self.pill_conn.setStyleSheet("color: #ff1744; background: rgba(255, 23, 68, 0.18); border: 1px solid #ff1744; border-radius: 3px; padding: 2px 7px;")

        st = status.get("agent_state", "IDLE")
        self.pill_state.setText(st)
        if st == "THINKING":
            col = "#ffb300"
        elif st in ("ACTING", "SPEAKING"):
            col = "#e040fb"
        elif st == "LISTENING":
            col = "#00e676"
        else:
            col = "#00e5ff"
        self.pill_state.setStyleSheet(f"color: {col}; background: rgba(255, 255, 255, 0.08); border: 1px solid {col}; border-radius: 3px; padding: 2px 7px;")

        mode = status.get("brain_mode", "HYBRID")
        self.pill_mode.setText(mode)

        abort = status.get("abort_badge", "STOP CTRL+ALT+BACKSPACE")
        self.pill_abort.setText(abort)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Response Card Performance Header
# ─────────────────────────────────────────────────────────────────────────────

class ResponseCardPerfHeader(QFrame):
    """
    Metadata header attached to assistant response cards:
    Model name, LINK latency, REQUESTS, ACTIONS, TOKENS.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setStyleSheet(f"""
            QFrame {{
                background: rgba(10, 4, 6, 0.70);
                border-bottom: 1px solid rgba(255, 30, 66, 0.20);
                border-radius: 3px;
            }}
        """)
        self._build_ui()

    def _build_ui(self):
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(10)

        self.lbl_model = QLabel("gemini-2.5-flash")
        self.lbl_model.setFont(keena_font(7, bold=True))
        self.lbl_model.setStyleSheet(f"color: {C.PRI};")
        h.addWidget(self.lbl_model)

        self.lbl_link = QLabel("LINK 0 ms")
        self.lbl_link.setFont(keena_font(7))
        self.lbl_link.setStyleSheet("color: #00e5ff;")
        h.addWidget(self.lbl_link)

        self.lbl_req = QLabel("REQUESTS 0")
        self.lbl_req.setFont(keena_font(7))
        self.lbl_req.setStyleSheet(f"color: {C.TEXT_DIM};")
        h.addWidget(self.lbl_req)

        self.lbl_act = QLabel("ACTIONS 0")
        self.lbl_act.setFont(keena_font(7))
        self.lbl_act.setStyleSheet(f"color: {C.TEXT_DIM};")
        h.addWidget(self.lbl_act)

        self.lbl_tok = QLabel("TOKENS 0")
        self.lbl_tok.setFont(keena_font(7))
        self.lbl_tok.setStyleSheet(f"color: {C.TEXT_DIM};")
        h.addWidget(self.lbl_tok)

        h.addStretch()

    def update_perf(self, perf: Dict[str, Any]):
        self.lbl_model.setText(perf.get("model", "gemini-2.5-flash"))
        self.lbl_link.setText(f"LINK {perf.get('latency_ms', 0)} ms")
        self.lbl_req.setText(f"REQUESTS {perf.get('requests', 0)}")
        self.lbl_act.setText(f"ACTIONS {perf.get('actions', 0)}")
        self.lbl_tok.setText(f"TOKENS {perf.get('tokens', 0)}")


# ─────────────────────────────────────────────────────────────────────────────
# 10. Master Telemetry Panel (Unified Command-Center View)
# ─────────────────────────────────────────────────────────────────────────────

class MasterTelemetryPanel(QWidget):
    """
    Comprehensive command-center layout combining all telemetry widgets.
    Autonomously subscribes to TelemetryEventBus signals.
    """

    def __init__(self, main_win=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._main_win = main_win
        self.setStyleSheet("background: transparent;")
        self._build_ui()
        self._wire_bus()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # Top status bar inside panel
        self.top_status = TopStatusBarWidget()
        root.addWidget(self.top_status)

        # Main 3-column / split layout inside a scroll area for responsive scaling
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent; border: none;")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        c_lay = QHBoxLayout(content)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(8)

        # Left Column: System Vitals & Net Link
        col_left = QVBoxLayout()
        col_left.setSpacing(8)
        self.vitals_widget = SystemVitalsWidget()
        col_left.addWidget(self.vitals_widget)
        self.net_widget = NetLinkWaveformWidget()
        col_left.addWidget(self.net_widget)
        c_lay.addLayout(col_left, stretch=4)

        # Center Column: Rolling Load Chart & Process Radar
        col_center = QVBoxLayout()
        col_center.setSpacing(8)
        self.load_chart = RollingLoadChartWidget()
        col_center.addWidget(self.load_chart)
        self.radar_widget = ProcessRadarWidget()
        col_center.addWidget(self.radar_widget)
        c_lay.addLayout(col_center, stretch=5)

        # Right Column: Event Stream, Model Bench, Actions History
        col_right = QVBoxLayout()
        col_right.setSpacing(8)
        self.event_stream = EventStreamWidget()
        col_right.addWidget(self.event_stream, stretch=2)
        self.model_bench = ModelBenchWidget()
        col_right.addWidget(self.model_bench, stretch=1)
        self.actions_history = ActionsHistoryWidget()
        col_right.addWidget(self.actions_history, stretch=1)
        c_lay.addLayout(col_right, stretch=5)

        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

    def _wire_bus(self):
        bus = get_event_bus()
        bus.vitals_updated.connect(self.vitals_widget.update_vitals)
        bus.load_history_updated.connect(self.load_chart.update_history)
        bus.processes_updated.connect(self.radar_widget.update_processes)
        bus.net_updated.connect(self.net_widget.update_net)
        bus.clock_updated.connect(self.top_status.update_clock)
        bus.status_pill_updated.connect(self.top_status.update_status_pill)
        bus.event_logged.connect(self.event_stream.add_event)
        bus.model_bench_logged.connect(self.model_bench.update_model)
        bus.action_logged.connect(self.actions_history.add_action)

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
        """Legacy compatibility slot for existing timer loops and tests."""
        cpu = snap.get("cpu", 0.0)
        gpu = snap.get("gpu", -1.0)
        tmp = snap.get("tmp", -1.0)
        self.vitals_widget.lbl_cpu_val.setText(f"{cpu:.0f} %")
        self.vitals_widget.bar_cpu.setValue(int(cpu))
        if gpu >= 0:
            self.vitals_widget.lbl_gpu_val.setText(f"{gpu:.0f} %")
            self.vitals_widget.bar_gpu.setValue(int(gpu))
        if tmp >= 0:
            self.vitals_widget.lbl_gpu_temp.setText(f"{tmp:.0f} °C")

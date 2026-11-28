"""
tests/test_telemetry_service.py - Comprehensive Unit & Integration Tests for Telemetry Suite
"""

import collections
import datetime
import os
import sys
import time
import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from core.telemetry_service import (
    TelemetryEventBus,
    TelemetryWorker,
    TelemetryService,
    get_telemetry_service,
    get_event_bus,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_telemetry_event_bus_singleton():
    bus1 = get_event_bus()
    bus2 = TelemetryEventBus.get_instance()
    assert bus1 is bus2


def test_telemetry_event_bus_signals(qapp):
    bus = get_event_bus()
    received_events = []
    received_actions = []
    received_status = []
    received_perf = []
    received_errors = []

    bus.event_logged.connect(lambda e: received_events.append(e))
    bus.action_logged.connect(lambda a: received_actions.append(a))
    bus.status_pill_updated.connect(lambda s: received_status.append(s))
    bus.response_perf_updated.connect(lambda p: received_perf.append(p))
    bus.error_alert.connect(lambda err: received_errors.append(err))

    # Test event logging
    bus.emit_event("ROUTE", "route -> Google Gemini gemini-2.5-flash", level="info")
    assert len(received_events) > 0
    assert received_events[-1]["tag"] == "ROUTE"
    assert "gemini-2.5-flash" in received_events[-1]["message"]

    # Test action logging
    bus.emit_action("Play: https://open.spotify.com/track/123", category="TO", duration_ms=56, success=True)
    assert len(received_actions) > 0
    assert received_actions[-1]["checkmark"] == "✓"
    assert received_actions[-1]["category"] == "TO"
    assert received_actions[-1]["duration_ms"] == 56

    # Test status pill
    bus.emit_status_pill(connection="CORE ONLINE", agent_state="ACTING", brain_mode="HYBRID")
    assert len(received_status) > 0
    assert received_status[-1]["connection"] == "CORE ONLINE"
    assert received_status[-1]["agent_state"] == "ACTING"
    assert received_status[-1]["brain_mode"] == "HYBRID"

    # Test response perf
    bus.emit_response_perf(model="gemini-3.6-flash", latency_ms=592, requests_inc=1)
    assert len(received_perf) > 0
    assert received_perf[-1]["model"] == "gemini-3.6-flash"
    assert received_perf[-1]["latency_ms"] == 592

    # Test error alert
    bus.emit_error("Google Gemini rejected the request: Error code: 400")
    assert len(received_errors) > 0
    assert "400" in received_errors[-1]


def test_telemetry_worker_collectors(qapp):
    bus = get_event_bus()
    worker = TelemetryWorker(bus)

    # 1. System Vitals
    vitals = worker._collect_vitals()
    assert isinstance(vitals, dict)
    assert "cpu_pct" in vitals
    assert "mem_used_gb" in vitals
    assert "mem_total_gb" in vitals
    assert "mem_pct" in vitals
    assert "disks" in vitals
    assert isinstance(vitals["disks"], list)
    assert "battery_formatted" in vitals

    # 2. Net Link throughput
    net_info = worker._collect_net_throughput()
    assert isinstance(net_info, dict)
    assert "dl_rate_bps" in net_info
    assert "ul_rate_bps" in net_info
    assert "dl_formatted" in net_info
    assert "ul_formatted" in net_info
    assert "net_history" in net_info

    # 3. Process Radar
    procs = worker._collect_top_processes()
    assert isinstance(procs, list)
    assert len(procs) <= 5
    if procs:
        top_p = procs[0]
        assert "name" in top_p
        assert "ram_formatted" in top_p
        assert "cpu_pct" in top_p
        assert "angle_rad" in top_p
        assert "radius_norm" in top_p


def test_telemetry_custom_widgets(qapp):
    from gui.telemetry_widgets import (
        SystemVitalsWidget,
        RollingLoadChartWidget,
        ProcessRadarWidget,
        NetLinkWaveformWidget,
        EventStreamWidget,
        ModelBenchWidget,
        ActionsHistoryWidget,
        TopStatusBarWidget,
        ResponseCardPerfHeader,
        MasterTelemetryPanel,
    )
    from gui.display_windows import TelemetryDisplayWindow

    # 1. SystemVitalsWidget
    vw = SystemVitalsWidget()
    vw.update_vitals({
        "cpu_pct": 25.4,
        "cpu_freq_mhz": 3100.0,
        "mem_used_gb": 8.5,
        "mem_total_gb": 16.0,
        "mem_pct": 53.0,
        "mem_formatted": "8.5 / 16.0 GB",
        "gpu_pct": 18.0,
        "gpu_temp_c": 48.0,
        "gpu_vram_used_gb": 1.2,
        "gpu_vram_total_gb": 4.0,
        "gpu_vram_pct": 30.0,
        "disks": [{"mount": "C:\\", "label": "C:", "formatted": "180 GB free", "pct": 45.0}],
        "battery_pct": 80.0,
        "battery_plugged": True,
        "battery_formatted": "80% AC",
    })
    assert "25.4 %" in vw.lbl_cpu_val.text()

    # 2. RollingLoadChartWidget
    lw = RollingLoadChartWidget()
    lw.resize(300, 150)
    samples = [(time.time() - i, 20.0 + i, 50.0, 15.0) for i in range(10)]
    lw.update_history(samples)

    # 3. ProcessRadarWidget
    rw = ProcessRadarWidget()
    rw.update_processes([
        {"name": "msedge.exe", "ram_formatted": "1.5 GB", "cpu_pct": 2.1, "count": 12, "angle_rad": 0.0, "radius_norm": 0.6},
        {"name": "python.exe", "ram_formatted": "450 MB", "cpu_pct": 1.5, "count": 2, "angle_rad": 1.2, "radius_norm": 0.4},
    ])
    assert "msedge.exe" in rw.proc_rows[0][0].text()

    # 4. NetLinkWaveformWidget
    nw = NetLinkWaveformWidget()
    nw.resize(300, 120)
    nw.update_net({
        "dl_formatted": "↓ 128 kb/s",
        "ul_formatted": "↑ 45 kb/s",
        "net_history": [(time.time() - i, 1024.0 * 100, 1024.0 * 20) for i in range(10)],
    })

    # 5. EventStreamWidget
    ew = EventStreamWidget()
    ew.add_event({"timestamp": "14:50:00", "tag": "ROUTE", "message": "route -> Google Gemini", "level": "info"})
    ew.add_event({"timestamp": "14:50:01", "tag": "FAULT", "message": "Connection reset", "level": "error"})
    assert "ROUTE" in ew.log_text.toPlainText() or "ROUTE" in ew.log_text.toHtml()

    # 6. ModelBenchWidget
    mbw = ModelBenchWidget()
    mbw.update_model({"model": "gemini-2.5-flash", "tier": "FAST", "avg_latency": "0.7s", "pass_ratio": "16/16 pass"})

    # 7. ActionsHistoryWidget
    ahw = ActionsHistoryWidget()
    ahw.add_action({"checkmark": "✓", "label": "Play: Spotify", "category": "TO", "duration_ms": 42, "success": True})

    # 8. TopStatusBarWidget
    tbw = TopStatusBarWidget()
    tbw.update_clock({"time_str": "15:00:00.0", "date_str": "SAT, 28 NOV, 2026", "uptime_str": "01:23:45"})
    assert "15:00:00.0" in tbw.lbl_clock.text()
    assert "SAT, 28 NOV, 2026" in tbw.lbl_date.text()

    # 9. ResponseCardPerfHeader
    rc = ResponseCardPerfHeader()
    rc.update_perf({"model": "gemini-3.6-flash", "latency_ms": 592, "requests": 10, "actions": 4, "tokens": 0})
    assert "gemini-3.6-flash" in rc.lbl_model.text()
    assert "592 ms" in rc.lbl_link.text()

    # 10. MasterTelemetryPanel
    mp = MasterTelemetryPanel()
    assert mp.vitals_widget is not None
    assert mp.radar_widget is not None

    # 11. TelemetryDisplayWindow
    tw = TelemetryDisplayWindow()
    assert tw.panel is not None
    tw.close()

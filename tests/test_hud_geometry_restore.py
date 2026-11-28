"""
tests/test_hud_geometry_restore.py - Unit tests for HUD window geometry restoration,
MINI mode toggle, conversation deduplication, and layout non-shrinkage.
"""

import time
import pytest
from PyQt6.QtCore import QEvent, QRect, Qt
from PyQt6.QtWidgets import QApplication, QSizePolicy

from gui.window import MainWindow
from gui.hud import HudCanvas, HudConversationWidget


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def hud_window(qapp):
    win = MainWindow("")
    yield win
    win.close()


def test_normal_geometry_captured_and_restored(hud_window):
    # Verify initial minimum size constraint
    assert hud_window.minimumWidth() >= 420
    assert hud_window.minimumHeight() >= 540

    # Initial show capture
    hud_window.show()
    assert getattr(hud_window, "_normal_geometry", None) is not None
    norm_geom = hud_window._normal_geometry
    assert norm_geom.width() >= 420
    assert norm_geom.height() >= 540

    # Enter MINI mode
    hud_window.toggle_mini_mode(True)
    assert hud_window._is_minimized_top is True
    assert hud_window.hud._mini_mode is True
    assert hud_window.hud_ctrl_bar.isVisible() is False
    assert hud_window.width() <= 60
    assert hud_window.height() <= 60

    # Restore from MINI mode
    hud_window.toggle_mini_mode(False)
    assert hud_window._is_minimized_top is False
    assert hud_window.hud._mini_mode is False
    assert hud_window.hud_ctrl_bar.isVisible() is True
    assert hud_window.hud_box.width() == 420

    # Fast-forward animation if still running
    if hasattr(hud_window, "_geom_anim") and hud_window._geom_anim:
        hud_window._geom_anim.setCurrentTime(350)

    # Verify exact geometry restoration
    assert hud_window.geometry().width() == norm_geom.width()
    assert hud_window.geometry().height() == norm_geom.height()
    assert hud_window.minimumWidth() >= 420
    assert hud_window.minimumHeight() >= 580


def test_os_window_state_minimize_and_restore(hud_window):
    hud_window.show()
    norm_geom = hud_window._normal_geometry

    # Simulate OS window state change to Minimized
    min_event = QEvent(QEvent.Type.WindowStateChange)
    hud_window.setWindowState(Qt.WindowState.WindowMinimized)
    hud_window.changeEvent(min_event)

    assert hud_window._normal_geometry is not None

    # Simulate OS window state restore
    hud_window.setWindowState(Qt.WindowState.WindowNoState)
    hud_window.changeEvent(min_event)

    assert hud_window.geometry().width() == norm_geom.width()
    assert hud_window.geometry().height() == norm_geom.height()
    assert hud_window.hud_box.width() == 420


def test_conversation_widget_size_policy_and_deduplication(qapp):
    conv = HudConversationWidget(assistant_name="JARVIS")

    # 1. Size policy must be Ignored so text growth never pushes HUD frame
    assert conv.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
    assert conv.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Ignored

    # 2. Test assistant message deduplication
    greeting = "Good afternoon, sir! It is 3:02 PM, Saturday, November 28th."
    conv.append_message("jarvis", greeting)
    assert len(conv._messages) == 1
    assert conv._messages[0] == ("jarvis", greeting)

    # Adding exact duplicate should be ignored
    conv.append_message("jarvis", greeting)
    assert len(conv._messages) == 1

    # Streaming the exact same greeting should not duplicate in rendering
    conv.stream_reply(greeting)
    conv._render_conversation()
    html = conv.toHtml()
    assert html.count("Good afternoon") == 1


def test_action_card_and_conversation_positioning(qapp):
    hud = HudCanvas()
    hud.resize(420, 540)

    # Without action cards
    hud._update_conversation_geometry()
    geom_no_card = hud.conversation.geometry()

    # Add action card
    hud.add_card("JARVIS ONLINE - LISTENING FOR...")
    hud._update_conversation_geometry()
    geom_with_card = hud.conversation.geometry()

    # Conversation top must be pushed below cards cleanly without overlap
    assert geom_with_card.top() > geom_no_card.top() or geom_with_card.top() >= 360
    assert geom_with_card.height() >= 50

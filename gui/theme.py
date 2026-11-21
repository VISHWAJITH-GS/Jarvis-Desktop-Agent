from __future__ import annotations
import colorsys
from PyQt6.QtGui import QColor


class C:
    BG = "#0a0203"
    PANEL = "#130305"
    PANEL2 = "#1a0508"
    BORDER = "#420c12"
    BORDER_B = "#8a1520"
    BORDER_A = "#661019"
    PRI = "#ff1744"
    PRI_DIM = "#990e28"
    PRI_GHO = "#2e040a"
    ACC = "#ff5252"
    ACC2 = "#ff5252"
    GREEN = "#00ffaa"
    GREEN_D = "#00aa66"
    RED = "#ff1744"
    MUTED_C = "#ff4444"
    TEXT = "#ffeded"
    TEXT_DIM = "#d69da4"
    TEXT_MED = "#f0b8bd"
    WHITE = "#ffffff"
    DARK = "#0a0203"
    BAR_BG = "#180406"


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG",
    "PANEL",
    "PANEL2",
    "BORDER",
    "BORDER_B",
    "BORDER_A",
    "PRI",
    "PRI_DIM",
    "PRI_GHO",
    "TEXT",
    "TEXT_DIM",
    "TEXT_MED",
    "WHITE",
    "DARK",
    "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]

# ── State-Based Color System ─────────────────────────────────────────────────
# Listening:  Red (#ff1744)
# Speaking:   Yellow (#ffd700)
# Thinking:   Bright glowing light blue (#38BDF8, #7DD3FC, rgba(56,189,248,0.65), #BAE6FD)
# Processing: Bright glowing light blue (#38BDF8, #7DD3FC, rgba(56,189,248,0.65), #BAE6FD)
COLOR_STATE_LISTENING = QColor("#ff1744")
COLOR_STATE_SPEAKING = QColor(255, 215, 0)
COLOR_STATE_THINKING = QColor("#38BDF8")
COLOR_STATE_PROCESSING = QColor("#38BDF8")

COLOR_BLUE_PRIMARY = QColor("#38BDF8")
COLOR_BLUE_GLOW = QColor("#7DD3FC")
COLOR_BLUE_OUTER = QColor(56, 189, 248, 166)  # rgba(56, 189, 248, 0.65)
COLOR_BLUE_HIGHLIGHT = QColor("#BAE6FD")


def _lerp_color(c1: QColor, c2: QColor, factor: float) -> QColor:
    factor = max(0.0, min(1.0, factor))
    r = int(c1.red() + (c2.red() - c1.red()) * factor)
    g = int(c1.green() + (c2.green() - c1.green()) * factor)
    b = int(c1.blue() + (c2.blue() - c1.blue()) * factor)
    a = int(c1.alpha() + (c2.alpha() - c1.alpha()) * factor)
    return QColor(r, g, b, a)


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Re-derives the whole teal-family palette from the chosen accent colour
    (hue shift — brightness/saturation ratios are preserved, design stays intact).
    Painted elements (HUD, waveform, metrics) pick up the new colour on the next
    frame; stylesheet-based panels pick it up when they are rebuilt.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh = acc_h - base_h
    grey = acc_s < 0.08  # near-grey accent → the whole theme is desaturated

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


_NO_SCROLLBAR_STYLE = """
QScrollBar:vertical, QScrollBar:horizontal {
    background: transparent !important;
    width: 0px !important;
    height: 0px !important;
    max-width: 0px !important;
    max-height: 0px !important;
    border: none !important;
    margin: 0px !important;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: transparent !important;
    width: 0px !important;
    height: 0px !important;
    border: none !important;
}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {
    width: 0px !important;
    height: 0px !important;
    background: none !important;
    border: none !important;
}
"""

_RESIZE_MARGIN = 8


_RESIZE_MARGIN = 8
RESIZE_MARGIN = 8
__all__ = [
    "C",
    "_HUE_LINKED",
    "_PALETTE_DEFAULTS",
    "DEFAULT_UI_COLOR",
    "COLOR_STATE_LISTENING",
    "COLOR_STATE_SPEAKING",
    "COLOR_STATE_THINKING",
    "COLOR_STATE_PROCESSING",
    "COLOR_BLUE_PRIMARY",
    "COLOR_BLUE_GLOW",
    "COLOR_BLUE_OUTER",
    "COLOR_BLUE_HIGHLIGHT",
    "_lerp_color",
    "apply_ui_accent",
    "_NO_SCROLLBAR_STYLE",
    "_RESIZE_MARGIN",
    "RESIZE_MARGIN",
]

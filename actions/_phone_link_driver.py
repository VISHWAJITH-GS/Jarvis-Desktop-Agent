"""
actions/_phone_link_driver.py — Windows UIA Driver & Safety Layer for Microsoft Phone Link.

Leading underscore ensures core/action_loader.py does not register this module as a tool.
All pywinauto / comtypes imports are lazy via core.lazyimport so discovery never loads UIA.
"""

from __future__ import annotations

import ctypes
import json
import locale
import logging
import os
import platform
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core import outcome
from core.lazyimport import LazyModule, installed
from memory import config_manager

logger = logging.getLogger("phone_link")

_IS_WINDOWS = platform.system() == "Windows"
_PYWINAUTO_INSTALLED = installed("pywinauto")
_PSUTIL_INSTALLED = installed("psutil")
_PYPERCLIP_INSTALLED = installed("pyperclip")

pywinauto = LazyModule("pywinauto")
psutil = LazyModule("psutil")
pyperclip = LazyModule("pyperclip")

_UIA_LOCK = threading.RLock()
_SELECTORS_PATH = Path(__file__).resolve().parent.parent / "config" / "phone_link_selectors.json"
_DUMPS_DIR = Path(__file__).resolve().parent.parent / "docs" / "phone_link_uia_dumps"

# Capability cache (short TTL, invalidated on any UIA failure)
_CAP_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_CAP_TTL_SEC = 60.0

# Sliding-window rate limiter timestamps per outbound category
_RATE_TIMESTAMPS: dict[str, deque[float]] = {
    "outbound": deque(maxlen=120),
}

# Prompt-injection escalation flag: set when any untrusted content is read
_UNTRUSTED_READ_STATE: dict[str, Any] = {"dirty": False, "ts": 0.0}

# Confirmation tokens / pending store is strictly handled via core.confirm;
# this flag tracks whether the current run read untrusted Phone Link data.
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_OTP_CONTEXT_RE = re.compile(
    r"(?i)\b(?:otp|one[- ]time|verification|verify|code|pin|passcode|bank|txn|transaction|debited|credited|cvv|auth)\b"
)
_OTP_DIGITS_RE = re.compile(r"\b\d{4,8}\b")


class MissingSelectorError(RuntimeError):
    """Raised when a UIA control cannot be resolved from config/phone_link_selectors.json."""

    def __init__(self, selector_key: str, detail: str = ""):
        self.selector_key = selector_key
        msg = f"Selector '{selector_key}' could not be resolved"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


def is_windows_platform() -> bool:
    return _IS_WINDOWS


def unsupported_result(action: str, reason: str) -> str:
    """Return machine-readable unsupported status matching core.outcome contract."""
    return f"status=unsupported action={action} reason={outcome._render(reason)}"


def get_windows_ui_locale() -> str:
    """Detect the active Windows display language tag (e.g. 'en-US')."""
    if not _IS_WINDOWS:
        return "en-US"
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        loc = locale.windows_locale.get(lang_id, "en_US")
        return loc.replace("_", "-")
    except Exception:
        return "en-US"


def load_selectors(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or _SELECTORS_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load phone_link selectors from %s: %s", target, exc)
        return {"process_names": ["PhoneExperienceHost.exe"], "window_titles": {}, "controls": {}}


# ── Privacy, Redaction & Safety Helpers ───────────────────────────────────────

def mask_phone(number: str) -> str:
    """Mask phone number keeping only the last 4 digits."""
    digits = re.sub(r"\D", "", str(number or ""))
    if len(digits) <= 4:
        return "****" if digits else ""
    return f"***-***-{digits[-4:]}"


def redact_otp(text: str) -> str:
    """Redact OTP-like 4-8 digit codes when surrounded by OTP/banking context."""
    raw = str(text or "")
    if not raw:
        return ""
    if _OTP_CONTEXT_RE.search(raw):
        return _OTP_DIGITS_RE.sub("[REDACTED_OTP]", raw)
    return raw


def scrub_pii(text: str) -> str:
    """Scrub phone numbers, emails, and OTPs for logs, audit entries, and UIA dumps."""
    out = redact_otp(str(text or ""))
    out = _EMAIL_RE.sub("<REDACTED_EMAIL>", out)
    out = _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), out)
    return out


def is_emergency_number(target: str) -> bool:
    """Return True if target normalizes to any configured emergency number."""
    digits = re.sub(r"\D", "", str(target or ""))
    if not digits:
        return False
    settings = config_manager.get_phone_link_settings()
    blocked = {re.sub(r"\D", "", str(n)) for n in settings.get("emergency_numbers", [])}
    return digits in blocked


def check_rate_limit(category: str = "outbound") -> tuple[bool, int, int]:
    """Check sliding 60s rate limit. Returns (allowed, used_in_window, limit)."""
    limit = int(config_manager.get_phone_link_settings().get("rate_limit_per_min", 5))
    now = time.monotonic()
    q = _RATE_TIMESTAMPS.setdefault(category, deque(maxlen=120))
    while q and (now - q[0]) > 60.0:
        q.popleft()
    if len(q) >= limit:
        return False, len(q), limit
    return True, len(q), limit


def record_rate_event(category: str = "outbound") -> None:
    q = _RATE_TIMESTAMPS.setdefault(category, deque(maxlen=120))
    q.append(time.monotonic())


def mark_untrusted_read() -> None:
    """Mark that untrusted Phone Link content (SMS/notifications/calls) was read."""
    _UNTRUSTED_READ_STATE["dirty"] = True
    _UNTRUSTED_READ_STATE["ts"] = time.time()


def has_untrusted_read() -> bool:
    return bool(_UNTRUSTED_READ_STATE.get("dirty", False))


def reset_untrusted_read() -> None:
    _UNTRUSTED_READ_STATE["dirty"] = False
    _UNTRUSTED_READ_STATE["ts"] = 0.0


def record_audit(action: str, contact_alias: str = "", phone: str = "", status_str: str = "done") -> None:
    """Log privacy-safe audit metadata (never plaintext message bodies or full numbers)."""
    last4 = ""
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits:
        last4 = digits[-4:]
    safe_alias = scrub_pii(contact_alias)[:32] if contact_alias else ""
    logger.info(
        "[PhoneLinkAudit] action=%s alias=%r last4=%r status=%s ts=%s",
        action,
        safe_alias,
        last4,
        status_str,
        datetime.now(timezone.utc).isoformat(),
    )


def wrap_untrusted_field(source: str, value: str) -> str:
    return outcome.untrusted(f"phone_link:{source}", str(value or ""))


# ── Foreground Window & Clipboard Guards ──────────────────────────────────────

class _ForegroundGuard:
    """Save and restore the user's active foreground window around UIA operations."""

    def __init__(self, restore: bool = True):
        self._restore = restore
        self._hwnd: Optional[int] = None

    def __enter__(self) -> "_ForegroundGuard":
        if _IS_WINDOWS and self._restore:
            try:
                self._hwnd = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                self._hwnd = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if _IS_WINDOWS and self._restore and self._hwnd:
            try:
                cur = ctypes.windll.user32.GetForegroundWindow()
                if cur != self._hwnd:
                    ctypes.windll.user32.SetForegroundWindow(self._hwnd)
            except Exception:
                pass


class _ClipboardGuard:
    """Save and restore clipboard text ONLY for operations that touch the clipboard."""

    def __init__(self):
        self._saved: Optional[str] = None
        self._captured = False

    def __enter__(self) -> "_ClipboardGuard":
        if _PYPERCLIP_INSTALLED:
            try:
                self._saved = pyperclip.paste()
                self._captured = True
            except Exception:
                self._captured = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._captured and _PYPERCLIP_INSTALLED and self._saved is not None:
            try:
                pyperclip.copy(self._saved)
            except Exception:
                pass


# ── UIA Tree Inspection & Selector Resolution (Real + Fake Test Trees) ────────

def _elem_info(elem: Any) -> dict[str, str]:
    """Extract normalized UIA attributes from a pywinauto wrapper or fake test node."""
    if isinstance(elem, dict):
        return {
            "automation_id": str(elem.get("automation_id") or elem.get("AutomationId") or ""),
            "control_type": str(elem.get("control_type") or elem.get("ControlType") or ""),
            "name": str(elem.get("name") or elem.get("Name") or ""),
            "class_name": str(elem.get("class_name") or elem.get("ClassName") or ""),
        }
    info = getattr(elem, "element_info", None)
    if info is not None:
        return {
            "automation_id": str(getattr(info, "automation_id", "") or ""),
            "control_type": str(getattr(info, "control_type", "") or ""),
            "name": str(getattr(info, "name", "") or ""),
            "class_name": str(getattr(info, "class_name", "") or ""),
        }
    return {
        "automation_id": str(getattr(elem, "automation_id", "") or ""),
        "control_type": str(getattr(elem, "control_type", "") or ""),
        "name": str(getattr(elem, "name", "") or ""),
        "class_name": str(getattr(elem, "class_name", "") or ""),
    }


def iter_uia_tree(root: Any, max_depth: int = 8, max_nodes: int = 600) -> list[tuple[int, Any]]:
    """Breadth/depth traversal returning (depth, node) pairs without raising."""
    results: list[tuple[int, Any]] = []
    stack: list[tuple[int, Any]] = [(0, root)]
    while stack and len(results) < max_nodes:
        depth, node = stack.pop(0)
        results.append((depth, node))
        if depth >= max_depth:
            continue
        children: list[Any] = []
        if isinstance(node, dict):
            children = list(node.get("children", []))
        elif hasattr(node, "children"):
            try:
                children = list(node.children())
            except Exception:
                children = []
        for ch in children:
            stack.append((depth + 1, ch))
    return results


def resolve_control(
    root: Any,
    selector_key: str,
    selectors: Optional[dict[str, Any]] = None,
    timeout: float = 2.5,
    preferred_locale: Optional[str] = None,
) -> tuple[Any, str]:
    """
    Resolve a UIA control by:
      1. AutomationId (locale-independent)
      2. ControlType + localized Name (active Windows locale first, then configured fallbacks)
    Returns (element, matched_source). Raises MissingSelectorError on failure.
    """
    sel_table = selectors or load_selectors()
    spec = sel_table.get("controls", {}).get(selector_key)
    if not spec:
        raise MissingSelectorError(selector_key, "key not defined in config/phone_link_selectors.json")

    auto_ids = [str(a) for a in spec.get("automation_ids", []) if a]
    ctrl_types = {str(c).lower() for c in spec.get("control_types", []) if c}
    names_by_locale: dict[str, list[str]] = spec.get("names", {}) or {}

    active_loc = preferred_locale or get_windows_ui_locale()
    locale_order = [active_loc] + [loc for loc in names_by_locale.keys() if loc != active_loc]

    deadline = time.monotonic() + max(0.05, timeout)
    attempt = 0
    while True:
        attempt += 1
        nodes = iter_uia_tree(root)

        # 1. AutomationId pass (highest priority, locale-independent)
        if auto_ids:
            for _, node in nodes:
                info = _elem_info(node)
                if info["automation_id"] and info["automation_id"] in auto_ids:
                    return node, "automation_id"

        # 2. ControlType + localized Name pass
        for loc in locale_order:
            cand_names = [str(n).lower() for n in names_by_locale.get(loc, []) if n]
            if not cand_names:
                continue
            for _, node in nodes:
                info = _elem_info(node)
                ctype = info["control_type"].lower()
                cname = info["name"].lower()
                if ctrl_types and ctype not in ctrl_types:
                    continue
                if any(cn == cname or cn in cname for cn in cand_names):
                    logger.debug("[PhoneLink] Selector '%s' matched locale=%s", selector_key, loc)
                    return node, f"locale:{loc}"

        if time.monotonic() >= deadline or isinstance(root, dict):
            break
        time.sleep(0.2)

    invalidate_capability_cache()
    raise MissingSelectorError(
        selector_key,
        f"not found after {attempt} attempt(s); run phone_link(action='diagnose') to inspect UIA tree",
    )


def _desktop_uia() -> Any:
    if not _PYWINAUTO_INSTALLED:
        raise RuntimeError("pywinauto is not installed")
    return pywinauto.Desktop(backend="uia")


def find_phone_link_window(timeout: float = 4.0, selectors: Optional[dict[str, Any]] = None) -> Optional[Any]:
    """Discover the top-level Phone Link window at runtime by process name or localized title."""
    if not _IS_WINDOWS or not _PYWINAUTO_INSTALLED:
        return None

    sel = selectors or load_selectors()
    proc_names = {p.lower() for p in sel.get("process_names", ["PhoneExperienceHost.exe"])}
    titles_map = sel.get("window_titles", {})
    all_titles = {t.lower() for ts in titles_map.values() for t in ts}

    # Collect active PIDs matching Phone Link process
    target_pids: set[int] = set()
    if _PSUTIL_INSTALLED:
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                pname = (proc.info.get("name") or "").lower()
                if pname in proc_names:
                    target_pids.add(int(proc.info["pid"]))
        except Exception:
            pass

    deadline = time.monotonic() + max(0.2, timeout)
    desk = _desktop_uia()
    while True:
        try:
            for win in desk.windows():
                try:
                    pid = win.process_id()
                    title = (win.window_text() or "").strip().lower()
                    if pid in target_pids or (title and any(t in title for t in all_titles)):
                        return win
                except Exception:
                    continue
        except Exception:
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    return None


def invalidate_capability_cache() -> None:
    _CAP_CACHE["ts"] = 0.0
    _CAP_CACHE["data"] = None


def probe_capabilities(
    window: Optional[Any] = None,
    force: bool = False,
    selectors: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Detect linked device type (android | iphone | unknown), enabled UI sections,
    and Windows display language. Cached briefly and invalidated on failure.
    """
    now = time.monotonic()
    if not force and _CAP_CACHE["data"] is not None and (now - _CAP_CACHE["ts"]) < _CAP_TTL_SEC:
        return dict(_CAP_CACHE["data"])

    sel = selectors or load_selectors()
    win = window or find_phone_link_window(timeout=1.5, selectors=sel)
    locale_tag = get_windows_ui_locale()

    if win is None:
        caps = {
            "window_open": False,
            "linked_os": "unknown",
            "connected": False,
            "locale": locale_tag,
            "sections": {},
        }
        _CAP_CACHE["ts"] = now
        _CAP_CACHE["data"] = caps
        return dict(caps)

    sections: dict[str, bool] = {}
    for key, feat in (
        ("nav_calls", "calls"),
        ("nav_messages", "messages"),
        ("nav_notifications", "notifications"),
        ("nav_photos", "photos"),
        ("nav_apps", "apps"),
        ("nav_settings", "settings"),
    ):
        try:
            resolve_control(win, key, selectors=sel, timeout=0.2)
            sections[feat] = True
        except MissingSelectorError:
            sections[feat] = False

    # Heuristic: Photos & Apps tabs exist only for Android in Phone Link
    if sections.get("photos") or sections.get("apps"):
        linked_os = "android"
    elif sections.get("calls") or sections.get("messages"):
        linked_os = "iphone_or_limited_android"
    else:
        linked_os = "unknown"

    caps = {
        "window_open": True,
        "linked_os": linked_os,
        "connected": any(sections.values()),
        "locale": locale_tag,
        "sections": sections,
    }
    _CAP_CACHE["ts"] = now
    _CAP_CACHE["data"] = caps
    return dict(caps)


def _focus_window(win: Any) -> None:
    try:
        if hasattr(win, "set_focus"):
            win.set_focus()
    except Exception:
        pass


def _send_shortcut(win: Any, shortcut: str) -> bool:
    """Send a documented Phone Link keyboard shortcut via pywinauto."""
    if hasattr(win, "type_keys"):
        win.type_keys(shortcut, set_foreground=False)
        return True
    return False


# ── Phase 1 Operations: status, launch, open_feature, diagnose ────────────────

_FEATURE_SHORTCUTS = {
    "notifications": "^1",
    "messages": "^2",
    "photos": "^3",
    "calls": "^4",
    "settings": "^,",
    "phone_screen": "^p",
}

_FEATURE_NAV_KEYS = {
    "calls": "nav_calls",
    "messages": "nav_messages",
    "notifications": "nav_notifications",
    "photos": "nav_photos",
    "apps": "nav_apps",
    "settings": "nav_settings",
}


def op_status(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("status", "Phone Link is only supported on Windows.")

    with _UIA_LOCK:
        process_running = False
        if _PSUTIL_INSTALLED:
            try:
                process_running = any(
                    (p.info.get("name") or "").lower() in {"phoneexperiencehost.exe", "yourphone.exe"}
                    for p in psutil.process_iter(["name"])
                )
            except Exception:
                pass

        caps = probe_capabilities(window=window, force=True)
        flags = config_manager.get_phone_link_settings()
        enabled_groups = [
            g for g in ("calls", "messages", "notifications", "photos", "apps", "watcher") if flags.get(g)
        ]

        record_audit("status", status_str="done")
        return outcome.done(
            "status",
            process_running=process_running,
            window_open=caps["window_open"],
            linked_os=caps["linked_os"],
            connected=caps["connected"],
            locale=caps["locale"],
            enabled_feature_flags=",".join(enabled_groups) if enabled_groups else "none",
            sections=json.dumps(caps["sections"]),
        )


def op_launch(window_finder: Optional[Callable[[], Any]] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("launch", "Phone Link is only supported on Windows.")

    with _UIA_LOCK:
        win = window_finder() if window_finder else find_phone_link_window(timeout=1.0)
        if win is not None:
            caps = probe_capabilities(window=win, force=True)
            record_audit("launch", status_str="done")
            return outcome.done("launch", already_open=True, linked_os=caps["linked_os"], verified=True)

        launched = False
        try:
            os.startfile("ms-phone:")
            launched = True
        except Exception:
            try:
                subprocess.Popen(
                    ["explorer.exe", "shell:AppsFolder\\Microsoft.YourPhone_8wekyb3d8bbwe!App"],
                    creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
                )
                launched = True
            except Exception as exc:
                record_audit("launch", status_str="failed")
                return outcome.failed("launch", f"Could not launch Phone Link: {exc}")

        win = window_finder() if window_finder else find_phone_link_window(timeout=6.0)
        if win is None:
            record_audit("launch", status_str="unverified")
            return outcome.unverified(
                "launch",
                "Phone Link launch command sent, but top-level window was not detected in UIA.",
                launched=launched,
            )

        caps = probe_capabilities(window=win, force=True)
        record_audit("launch", status_str="done")
        return outcome.done("launch", already_open=False, linked_os=caps["linked_os"], verified=True)


def op_open_feature(feature: str, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("open_feature", "Phone Link is only supported on Windows.")

    feat = str(feature or "").strip().lower()
    if feat not in _FEATURE_NAV_KEYS:
        return outcome.failed(
            "open_feature",
            f"Unknown feature '{feat}'. Choose one of: {', '.join(sorted(_FEATURE_NAV_KEYS))}",
        )

    with _UIA_LOCK, _ForegroundGuard(restore=False):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("open_feature", "Phone Link window is not open. Run action='launch' first.")

        nav_key = _FEATURE_NAV_KEYS[feat]
        try:
            elem, matched = resolve_control(win, nav_key, timeout=2.5)
            if hasattr(elem, "click_input"):
                elem.click_input()
            elif hasattr(elem, "invoke"):
                elem.invoke()
            elif isinstance(elem, dict) and callable(elem.get("on_click")):
                elem["on_click"]()
            record_audit("open_feature", contact_alias=feat, status_str="done")
            return outcome.done("open_feature", feature=feat, matched_by=matched, verified=True)
        except MissingSelectorError as exc:
            # Fallback to documented keyboard shortcut if available
            shortcut = _FEATURE_SHORTCUTS.get(feat)
            if shortcut and _send_shortcut(win, shortcut):
                record_audit("open_feature", contact_alias=feat, status_str="unverified")
                return outcome.unverified(
                    "open_feature",
                    f"Sent shortcut for '{feat}' because selector '{exc.selector_key}' was missing.",
                    feature=feat,
                )
            record_audit("open_feature", contact_alias=feat, status_str="failed")
            return outcome.failed("open_feature", str(exc), missing_selector=exc.selector_key)


def op_diagnose(window: Optional[Any] = None, output_dir: Optional[Path] = None) -> str:
    """Walk Phone Link's UIA tree, redact PII, and write a diagnostic dump file."""
    if not _IS_WINDOWS:
        return unsupported_result("diagnose", "Phone Link is only supported on Windows.")

    with _UIA_LOCK:
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("diagnose", "Phone Link window not found. Open Phone Link and retry.")

        target_dir = output_dir or _DUMPS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dump_path = target_dir / f"diagnose_{ts}.txt"

        win_title = ""
        win_class = ""
        if isinstance(win, dict):
            win_title = str(win.get("name", "Phone Link"))
            win_class = str(win.get("class_name", "FakeWindow"))
        else:
            try:
                win_title = str(win.window_text() or "")
                win_class = str(win.class_name() or "")
            except Exception:
                pass

        lines = [
            f"# Phone Link Redacted UIA Diagnostic Dump ({ts} UTC)",
            f"Locale: {get_windows_ui_locale()}",
            f"WindowTitle: {scrub_pii(win_title)}",
            f"WindowClass: {win_class}",
            "",
        ]
        nodes = iter_uia_tree(win, max_depth=8, max_nodes=500)
        for depth, node in nodes:
            info = _elem_info(node)
            indent = "  " * depth
            safe_name = scrub_pii(info["name"])
            if len(safe_name) > 80:
                safe_name = safe_name[:77] + "..."
            lines.append(
                f"{indent}[{info['control_type']}] AutomationId={info['automation_id']!r} Name={safe_name!r}"
            )

        dump_path.write_text("\n".join(lines), encoding="utf-8")
        record_audit("diagnose", status_str="done")
        return outcome.done(
            "diagnose",
            node_count=len(nodes),
            locale=get_windows_ui_locale(),
            dump_file=str(dump_path),
            verified=True,
        )


def _get_children(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return list(node.get("children", []))
    if hasattr(node, "children"):
        try:
            return list(node.children())
        except Exception:
            return []
    return []


def _click_elem(elem: Any) -> None:
    if hasattr(elem, "click_input"):
        elem.click_input()
    elif hasattr(elem, "invoke"):
        elem.invoke()
    elif isinstance(elem, dict) and callable(elem.get("on_click")):
        elem["on_click"]()


def _read_toggle_state(elem: Any) -> Optional[bool]:
    """Read UIA TogglePattern state (True=on, False=off, None=unknown)."""
    if isinstance(elem, dict):
        if "toggle_state" in elem:
            return bool(elem["toggle_state"])
        name_l = str(elem.get("name", "")).lower()
        if "on" in name_l or "muted" in name_l:
            return True
        if "off" in name_l or "unmuted" in name_l:
            return False
        return None
    try:
        if hasattr(elem, "get_toggle_state"):
            return bool(elem.get_toggle_state() == 1)
    except Exception:
        pass
    return None


# ── Phase 2 Operations: Notifications & Phone Controls (DND / Mute) ───────────

def _extract_notif_items(list_elem: Any) -> list[tuple[Any, str, str]]:
    """Return list of (node, app_or_title, body_text) from a notification list."""
    items: list[tuple[Any, str, str]] = []
    for child in _get_children(list_elem):
        info = _elem_info(child)
        ctype = info["control_type"].lower()
        if ctype in ("listitem", "group", "pane", "custom"):
            texts = [info["name"]] if info["name"] else []
            for _, sub in iter_uia_tree(child, max_depth=3, max_nodes=30):
                sinfo = _elem_info(sub)
                if sinfo["control_type"].lower() == "text" and sinfo["name"]:
                    if sinfo["name"] not in texts:
                        texts.append(sinfo["name"])
            title = texts[0] if texts else "Notification"
            body = " | ".join(texts[1:]) if len(texts) > 1 else ""
            items.append((child, title, body))
    return items


def op_list_notifications(limit: int = 15, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("list_notifications", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("list_notifications", "Phone Link window is not open.")

        try:
            list_elem, _ = resolve_control(win, "notifications_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("list_notifications", str(exc), missing_selector=exc.selector_key)

        raw_items = _extract_notif_items(list_elem)[: max(1, min(int(limit), 50))]
        mark_untrusted_read()
        out_items = [
            {
                "index": idx,
                "title": wrap_untrusted_field("notif_title", title),
                "body": wrap_untrusted_field("notif_body", redact_otp(body)),
            }
            for idx, (_, title, body) in enumerate(raw_items)
        ]
        record_audit("list_notifications", status_str="done")
        return outcome.done(
            "list_notifications",
            count=len(out_items),
            notifications=json.dumps(out_items, ensure_ascii=False),
        )


def op_dismiss_notification(
    app: Optional[str] = None,
    index: Optional[int] = None,
    window: Optional[Any] = None,
) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("dismiss_notification", "Phone Link is only supported on Windows.")
    if app is None and index is None:
        return outcome.failed("dismiss_notification", "Provide either 'app' name or 0-based 'index'.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("dismiss_notification", "Phone Link window is not open.")

        try:
            list_elem, _ = resolve_control(win, "notifications_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("dismiss_notification", str(exc), missing_selector=exc.selector_key)

        before_items = _extract_notif_items(list_elem)
        if not before_items:
            return outcome.failed("dismiss_notification", "No notifications to dismiss.")

        target_node = None
        if index is not None:
            idx = int(index)
            if 0 <= idx < len(before_items):
                target_node = before_items[idx][0]
        elif app:
            app_l = app.strip().lower()
            for node, title, body in before_items:
                if app_l in title.lower() or app_l in body.lower():
                    target_node = node
                    break

        if target_node is None:
            return outcome.failed("dismiss_notification", "Matching notification not found.")

        try:
            btn, _ = resolve_control(target_node, "notifications_dismiss_btn", timeout=1.5)
        except MissingSelectorError as exc:
            return outcome.failed("dismiss_notification", str(exc), missing_selector=exc.selector_key)

        _click_elem(btn)
        time.sleep(0.15)
        after_items = _extract_notif_items(list_elem)
        if len(after_items) < len(before_items):
            record_audit("dismiss_notification", contact_alias=app or str(index), status_str="done")
            return outcome.done(
                "dismiss_notification",
                remaining=len(after_items),
                undoable=False,
                note="Dismissed notification (not undoable).",
                verified=True,
            )

        record_audit("dismiss_notification", contact_alias=app or str(index), status_str="unverified")
        return outcome.unverified(
            "dismiss_notification",
            "Clicked dismiss, but notification count did not decrease.",
            remaining=len(after_items),
        )


def op_clear_all_notifications(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("clear_all", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("clear_all", "Phone Link window is not open.")

        try:
            list_elem, _ = resolve_control(win, "notifications_list", timeout=2.0)
        except MissingSelectorError as exc:
            return outcome.failed("clear_all", str(exc), missing_selector=exc.selector_key)

        try:
            btn, _ = resolve_control(win, "notifications_clear_btn", timeout=1.5)
            _click_elem(btn)
        except MissingSelectorError:
            if not _send_shortcut(win, "^%k"):  # Ctrl+Alt+K
                return outcome.failed(
                    "clear_all",
                    "Could not find 'Clear all' button or send Ctrl+Alt+K.",
                    missing_selector="notifications_clear_btn",
                )

        time.sleep(0.2)
        remaining = len(_extract_notif_items(list_elem))
        if remaining == 0:
            record_audit("clear_all", status_str="done")
            return outcome.done(
                "clear_all",
                remaining=0,
                undoable=False,
                note="Cleared all notifications (not undoable).",
                verified=True,
            )
        record_audit("clear_all", status_str="unverified")
        return outcome.unverified("clear_all", f"Clear command sent, {remaining} notification(s) still visible.")


def op_toggle_phone_control(
    action: str,
    selector_key: str,
    desired_state: str,
    window: Optional[Any] = None,
) -> str:
    """Toggle Android/supported device DND or Mute Phone and register core.undo restoration."""
    if not _IS_WINDOWS:
        return unsupported_result(action, "Phone Link is only supported on Windows.")

    target_l = str(desired_state or "").strip().lower()
    if target_l not in ("on", "off", "true", "false"):
        return outcome.failed(action, "Parameter 'state' must be 'on' or 'off'.")
    want_on = target_l in ("on", "true")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed(action, "Phone Link window is not open.")

        caps = probe_capabilities(window=win)
        if caps.get("linked_os") == "iphone_or_limited_android" and not caps.get("sections", {}).get("photos"):
            return unsupported_result(action, f"{action} is only supported on Android / supported devices.")

        try:
            toggle_elem, _ = resolve_control(win, selector_key, timeout=2.0)
        except MissingSelectorError as exc:
            return outcome.failed(action, str(exc), missing_selector=exc.selector_key)

        prev_state = _read_toggle_state(toggle_elem)
        if prev_state is not None and prev_state == want_on:
            record_audit(action, contact_alias=target_l, status_str="done")
            return outcome.done(action, state="on" if want_on else "off", already_set=True, verified=True)

        _click_elem(toggle_elem)
        time.sleep(0.2)
        new_state = _read_toggle_state(toggle_elem)

        if new_state is not None and new_state == want_on:
            if prev_state is not None:
                from core.undo import push_undo

                restore_str = "on" if prev_state else "off"
                push_undo(
                    f"Phone Link {action} -> {restore_str}",
                    lambda: op_toggle_phone_control(action, selector_key, restore_str, window=window),
                )
            record_audit(action, contact_alias=target_l, status_str="done")
            return outcome.done(action, state="on" if want_on else "off", undoable=prev_state is not None, verified=True)

        record_audit(action, contact_alias=target_l, status_str="unverified")
        return outcome.unverified(action, f"Toggled {action}, but could not verify new state.")


# ── Phase 3 Operations: Messages (SMS & Optional MMS) ─────────────────────────

def _approved_folders() -> list[Path]:
    settings = config_manager.get_phone_link_settings()
    custom = settings.get("approved_folders") or []
    if custom:
        return [Path(p).expanduser().resolve() for p in custom]
    return [
        (Path.home() / "Pictures").resolve(),
        (Path.home() / "Downloads").resolve(),
        (Path.home() / "Desktop").resolve(),
    ]


def _is_path_approved(file_path: str) -> tuple[bool, Path]:
    p = Path(file_path).expanduser().resolve()
    if not p.is_file():
        return False, p
    for root in _approved_folders():
        if p == root or p.is_relative_to(root):
            return True, p
    return False, p


def _extract_conv_items(conv_list: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for idx, child in enumerate(_get_children(conv_list)):
        info = _elem_info(child)
        if info["control_type"].lower() in ("listitem", "group", "button", "pane"):
            texts = [info["name"]] if info["name"] else []
            for _, sub in iter_uia_tree(child, max_depth=3, max_nodes=25):
                sinfo = _elem_info(sub)
                if sinfo["control_type"].lower() == "text" and sinfo["name"] and sinfo["name"] not in texts:
                    texts.append(sinfo["name"])
            contact_raw = texts[0] if texts else f"Contact {idx}"
            preview_raw = " | ".join(texts[1:]) if len(texts) > 1 else ""
            phone_match = _PHONE_RE.search(contact_raw + " " + preview_raw)
            items.append({
                "index": idx,
                "node": child,
                "contact": contact_raw,
                "phone": phone_match.group(0) if phone_match else "",
                "preview": preview_raw,
            })
    return items


def _resolve_contact_candidates(conv_items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = str(query or "").strip().lower()
    if not q:
        return []
    exact = [c for c in conv_items if c["contact"].lower() == q or (c["phone"] and re.sub(r"\D", "", c["phone"]) == re.sub(r"\D", "", q))]
    if len(exact) == 1:
        return exact
    fuzzy = [c for c in conv_items if q in c["contact"].lower() or (c["phone"] and q in c["phone"])]
    return fuzzy


def _extract_thread_messages(thread_list: Any) -> list[str]:
    msgs: list[str] = []
    for child in _get_children(thread_list):
        info = _elem_info(child)
        texts = [info["name"]] if info["name"] else []
        for _, sub in iter_uia_tree(child, max_depth=3, max_nodes=20):
            sinfo = _elem_info(sub)
            if sinfo["control_type"].lower() == "text" and sinfo["name"] and sinfo["name"] not in texts:
                texts.append(sinfo["name"])
        if texts:
            msgs.append(" — ".join(texts))
    return msgs


def _set_composer_text(elem: Any, text: str) -> None:
    if isinstance(elem, dict):
        elem["value"] = text
        if callable(elem.get("on_set_text")):
            elem["on_set_text"](text)
        return
    if hasattr(elem, "set_edit_text"):
        elem.set_edit_text(text)
    elif hasattr(elem, "type_keys"):
        elem.type_keys(text, with_spaces=True, set_foreground=False)


def op_list_conversations(limit: int = 15, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("list_conversations", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("list_conversations", "Phone Link window is not open.")

        try:
            conv_list, _ = resolve_control(win, "messages_conv_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("list_conversations", str(exc), missing_selector=exc.selector_key)

        items = _extract_conv_items(conv_list)[: max(1, min(int(limit), 50))]
        mark_untrusted_read()
        out = [
            {
                "index": it["index"],
                "contact": wrap_untrusted_field("contact", scrub_pii(it["contact"])),
                "preview": wrap_untrusted_field("sms_preview", redact_otp(it["preview"])),
            }
            for it in items
        ]
        record_audit("list_conversations", status_str="done")
        return outcome.done("list_conversations", count=len(out), conversations=json.dumps(out, ensure_ascii=False))


def op_read_conversation(
    contact: str,
    limit: int = 10,
    include_otp: bool = False,
    window: Optional[Any] = None,
) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("read_conversation", "Phone Link is only supported on Windows.")
    if not contact or not contact.strip():
        return outcome.failed("read_conversation", "Missing required parameter 'contact'.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("read_conversation", "Phone Link window is not open.")

        try:
            conv_list, _ = resolve_control(win, "messages_conv_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("read_conversation", str(exc), missing_selector=exc.selector_key)

        matches = _resolve_contact_candidates(_extract_conv_items(conv_list), contact)
        if not matches:
            return outcome.failed("read_conversation", f"No conversation found matching '{scrub_pii(contact)}'.")
        if len(matches) > 1:
            cands = [{"contact": c["contact"], "masked": mask_phone(c["phone"])} for c in matches[:5]]
            return outcome.ask(
                "read_conversation",
                f"Multiple contacts match '{contact}'. Which one do you mean?",
                candidates=json.dumps(cands, ensure_ascii=False),
            )

        _click_elem(matches[0]["node"])
        time.sleep(0.2)

        try:
            thread_list, _ = resolve_control(win, "messages_thread_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("read_conversation", str(exc), missing_selector=exc.selector_key)

        raw_msgs = _extract_thread_messages(thread_list)[-max(1, min(int(limit), 30)) :]
        mark_untrusted_read()
        wrapped = [
            wrap_untrusted_field("sms", m if include_otp else redact_otp(m))
            for m in raw_msgs
        ]
        record_audit("read_conversation", contact_alias=matches[0]["contact"], phone=matches[0]["phone"], status_str="done")
        return outcome.done(
            "read_conversation",
            contact=wrap_untrusted_field("contact", matches[0]["contact"]),
            count=len(wrapped),
            messages=json.dumps(wrapped, ensure_ascii=False),
        )


def op_send_sms(
    target: str,
    text: str,
    confirmed: bool = False,
    window: Optional[Any] = None,
) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("send_sms", "Phone Link is only supported on Windows.")
    if not target or not text:
        return outcome.failed("send_sms", "Both 'target' and 'text' are required.")

    # Never forward OTP/verification codes in outbound messages
    if _OTP_CONTEXT_RE.search(text) and _OTP_DIGITS_RE.search(text):
        return outcome.failed("send_sms", "Refused: outbound messages cannot include OTP or banking verification codes.")

    allowed, used, lim = check_rate_limit("outbound")
    if not allowed:
        return outcome.failed("send_sms", f"Rate limit reached ({used}/{lim} outbound actions per minute).")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("send_sms", "Phone Link window is not open.")

        is_raw_number = bool(re.fullmatch(r"[\d+\s().-]{5,}", target.strip()))
        chosen_phone = target.strip() if is_raw_number else ""
        chosen_alias = target.strip()

        if not is_raw_number:
            try:
                conv_list, _ = resolve_control(win, "messages_conv_list", timeout=2.0)
                matches = _resolve_contact_candidates(_extract_conv_items(conv_list), target)
                if len(matches) > 1:
                    cands = [{"contact": c["contact"], "masked": mask_phone(c["phone"])} for c in matches[:5]]
                    return outcome.ask(
                        "send_sms",
                        f"Multiple contacts match '{target}'. Please choose one.",
                        candidates=json.dumps(cands, ensure_ascii=False),
                    )
                if len(matches) == 1:
                    chosen_alias = matches[0]["contact"]
                    chosen_phone = matches[0]["phone"]
                    _click_elem(matches[0]["node"])
                    time.sleep(0.15)
            except MissingSelectorError:
                pass

        # UI confirmation gate (also mandatory whenever untrusted content was read)
        if not confirmed or has_untrusted_read():
            if not confirmed:
                return outcome.ask(
                    "send_sms",
                    f"Confirm sending SMS to '{chosen_alias}' ({mask_phone(chosen_phone) or 'contact'}): \"{text[:120]}\"?",
                    target=chosen_alias,
                )

        try:
            composer, _ = resolve_control(win, "messages_composer_input", timeout=2.5)
            send_btn, _ = resolve_control(win, "messages_send_btn", timeout=2.5)
            thread_list, _ = resolve_control(win, "messages_thread_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("send_sms", str(exc), missing_selector=exc.selector_key)

        before_msgs = _extract_thread_messages(thread_list)
        _set_composer_text(composer, text)
        _click_elem(send_btn)
        record_rate_event("outbound")
        reset_untrusted_read()
        time.sleep(0.25)

        after_msgs = _extract_thread_messages(thread_list)
        new_bubbles = after_msgs[len(before_msgs) :] if len(after_msgs) > len(before_msgs) else after_msgs[-2:]
        matched_sent = any(text.strip() in b for b in new_bubbles)

        if matched_sent:
            record_audit("send_sms", contact_alias=chosen_alias, phone=chosen_phone, status_str="done")
            return outcome.done(
                "send_sms",
                target=chosen_alias,
                undoable=False,
                note="SMS sent and verified in thread (not undoable).",
                verified=True,
            )

        record_audit("send_sms", contact_alias=chosen_alias, phone=chosen_phone, status_str="unverified")
        return outcome.unverified(
            "send_sms",
            "Clicked Send, but could not confirm the new outgoing message bubble in the thread.",
            target=chosen_alias,
        )


def op_send_mms_attachment(
    target: str,
    file_path: str,
    confirmed: bool = False,
    window: Optional[Any] = None,
) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("send_mms_attachment", "Phone Link is only supported on Windows.")
    if not target or not file_path:
        return outcome.failed("send_mms_attachment", "Both 'target' and 'file' are required.")

    ok_path, resolved_path = _is_path_approved(file_path)
    if not ok_path:
        return outcome.failed(
            "send_mms_attachment",
            "File does not exist or is outside user-approved folders (Pictures, Downloads, Desktop).",
        )

    allowed, used, lim = check_rate_limit("outbound")
    if not allowed:
        return outcome.failed("send_mms_attachment", f"Rate limit reached ({used}/{lim} outbound actions per minute).")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("send_mms_attachment", "Phone Link window is not open.")

        caps = probe_capabilities(window=win)
        if caps.get("linked_os") == "iphone_or_limited_android" and not caps.get("sections", {}).get("photos"):
            return unsupported_result("send_mms_attachment", "MMS attachments are not supported on iPhone.")

        if not confirmed:
            return outcome.ask(
                "send_mms_attachment",
                f"Confirm sending MMS attachment '{resolved_path.name}' to '{target}'?",
                target=target,
                file=resolved_path.name,
            )

        try:
            attach_btn, _ = resolve_control(win, "messages_attach_btn", timeout=2.5)
            send_btn, _ = resolve_control(win, "messages_send_btn", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("send_mms_attachment", str(exc), missing_selector=exc.selector_key)

        _click_elem(attach_btn)
        time.sleep(0.15)
        _click_elem(send_btn)
        record_rate_event("outbound")
        reset_untrusted_read()
        record_audit("send_mms_attachment", contact_alias=target, status_str="unverified")
        return outcome.unverified(
            "send_mms_attachment",
            f"Triggered MMS attachment '{resolved_path.name}' send; verify delivery in Phone Link thread.",
            undoable=False,
        )


# ── Phase 4 Operations: Calls ─────────────────────────────────────────────────

def _detect_call_state(win: Any) -> str:
    """Return 'incoming' | 'in_call' | 'idle' based on UIA call controls."""
    try:
        resolve_control(win, "calls_answer_btn", timeout=0.15)
        return "incoming"
    except MissingSelectorError:
        pass
    for key in ("calls_end_btn", "calls_active_banner"):
        try:
            resolve_control(win, key, timeout=0.15)
            return "in_call"
        except MissingSelectorError:
            pass
    return "idle"


def op_recent_calls(limit: int = 20, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("recent_calls", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("recent_calls", "Phone Link window is not open.")

        try:
            calls_list, _ = resolve_control(win, "calls_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("recent_calls", str(exc), missing_selector=exc.selector_key)

        capped = max(1, min(int(limit), 100))
        entries = _extract_conv_items(calls_list)[:capped]
        mark_untrusted_read()
        out = [
            {
                "index": it["index"],
                "contact": wrap_untrusted_field("call_contact", scrub_pii(it["contact"])),
                "detail": wrap_untrusted_field("call_detail", scrub_pii(it["preview"])),
            }
            for it in entries
        ]
        record_audit("recent_calls", status_str="done")
        return outcome.done("recent_calls", count=len(out), calls=json.dumps(out, ensure_ascii=False))


def op_call_status(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("call_status", "Phone Link is only supported on Windows.")

    with _UIA_LOCK:
        win = window or find_phone_link_window(timeout=2.0)
        if win is None:
            return outcome.failed("call_status", "Phone Link window is not open.")
        state = _detect_call_state(win)
        record_audit("call_status", contact_alias=state, status_str="done")
        return outcome.done("call_status", call_state=state)


def op_call(target: str, confirmed: bool = False, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("call", "Phone Link is only supported on Windows.")
    if not target or not target.strip():
        return outcome.failed("call", "Missing required parameter 'target' (contact or phone number).")

    if is_emergency_number(target):
        record_audit("call", contact_alias="EMERGENCY_BLOCKED", status_str="failed")
        return outcome.failed(
            "call",
            "Refused: emergency numbers (e.g. 911, 112, 999) are blocked by policy. Dial directly from your phone.",
        )

    allowed, used, lim = check_rate_limit("outbound")
    if not allowed:
        return outcome.failed("call", f"Rate limit reached ({used}/{lim} outbound actions per minute).")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("call", "Phone Link window is not open.")

        is_raw_number = bool(re.fullmatch(r"[\d+\s().-]{5,}", target.strip()))
        chosen_alias = target.strip()
        chosen_phone = target.strip() if is_raw_number else ""

        if not is_raw_number:
            try:
                calls_list, _ = resolve_control(win, "calls_list", timeout=1.5)
                matches = _resolve_contact_candidates(_extract_conv_items(calls_list), target)
                if len(matches) > 1:
                    cands = [{"contact": c["contact"], "masked": mask_phone(c["phone"])} for c in matches[:5]]
                    return outcome.ask(
                        "call",
                        f"Multiple contacts match '{target}'. Which one do you want to call?",
                        candidates=json.dumps(cands, ensure_ascii=False),
                    )
                if len(matches) == 1:
                    chosen_alias = matches[0]["contact"]
                    chosen_phone = matches[0]["phone"] or matches[0]["contact"]
            except MissingSelectorError:
                pass

        if not confirmed:
            return outcome.ask(
                "call",
                f"Confirm placing a phone call to '{chosen_alias}' ({mask_phone(chosen_phone) or 'contact'})?",
                target=chosen_alias,
            )

        try:
            dial_input, _ = resolve_control(win, "calls_dial_input", timeout=2.5)
            dial_btn, _ = resolve_control(win, "calls_dial_btn", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("call", str(exc), missing_selector=exc.selector_key)

        _set_composer_text(dial_input, chosen_phone or chosen_alias)
        _click_elem(dial_btn)
        record_rate_event("outbound")
        reset_untrusted_read()
        time.sleep(0.25)

        if _detect_call_state(win) == "in_call":
            record_audit("call", contact_alias=chosen_alias, phone=chosen_phone, status_str="done")
            return outcome.done(
                "call",
                target=chosen_alias,
                call_state="in_call",
                undoable=False,
                note="Call initiated (not undoable).",
                verified=True,
            )

        record_audit("call", contact_alias=chosen_alias, phone=chosen_phone, status_str="unverified")
        return outcome.unverified("call", "Clicked Dial, but active-call UI state could not be confirmed.")


def op_answer_call(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("answer", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=2.5)
        if win is None:
            return outcome.failed("answer", "Phone Link window is not open.")
        try:
            btn, _ = resolve_control(win, "calls_answer_btn", timeout=2.0)
        except MissingSelectorError as exc:
            return outcome.failed("answer", str(exc), missing_selector=exc.selector_key)

        _click_elem(btn)
        time.sleep(0.2)
        if _detect_call_state(win) == "in_call":
            record_audit("answer", status_str="done")
            return outcome.done("answer", call_state="in_call", verified=True)
        record_audit("answer", status_str="unverified")
        return outcome.unverified("answer", "Clicked Answer, but active-call state was not confirmed.")


def op_decline_call(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("decline", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=2.5)
        if win is None:
            return outcome.failed("decline", "Phone Link window is not open.")
        try:
            btn, _ = resolve_control(win, "calls_decline_btn", timeout=2.0)
        except MissingSelectorError as exc:
            return outcome.failed("decline", str(exc), missing_selector=exc.selector_key)

        _click_elem(btn)
        time.sleep(0.2)
        if _detect_call_state(win) != "incoming":
            record_audit("decline", status_str="done")
            return outcome.done("decline", call_state="idle", undoable=False, verified=True)
        record_audit("decline", status_str="unverified")
        return outcome.unverified("decline", "Clicked Decline, but incoming call controls are still visible.")


def op_end_call(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("end_call", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=2.5)
        if win is None:
            return outcome.failed("end_call", "Phone Link window is not open.")
        try:
            btn, _ = resolve_control(win, "calls_end_btn", timeout=2.0)
        except MissingSelectorError as exc:
            return outcome.failed("end_call", str(exc), missing_selector=exc.selector_key)

        _click_elem(btn)
        time.sleep(0.2)
        if _detect_call_state(win) == "idle":
            record_audit("end_call", status_str="done")
            return outcome.done("end_call", call_state="idle", undoable=False, verified=True)
        record_audit("end_call", status_str="unverified")
        return outcome.unverified("end_call", "Clicked End Call, but active-call state is still visible.")


def op_dial_digits(digits: str, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("dial_digits", "Phone Link is only supported on Windows.")
    clean = str(digits or "").strip()
    if not clean or not re.fullmatch(r"[0-9*#]+", clean):
        return outcome.failed("dial_digits", "Parameter 'digits' must contain only 0-9, *, or #.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=2.5)
        if win is None:
            return outcome.failed("dial_digits", "Phone Link window is not open.")
        if _detect_call_state(win) != "in_call":
            return outcome.failed("dial_digits", "dial_digits is only allowed during an active call.")

        if isinstance(win, dict):
            win.setdefault("_dialed_digits", []).append(clean)
        elif hasattr(win, "type_keys"):
            win.type_keys(clean, set_foreground=False)

        record_audit("dial_digits", contact_alias=f"len:{len(clean)}", status_str="done")
        return outcome.done("dial_digits", digits_sent=len(clean), verified=True)


def op_dial_via_tel_uri(number: str, confirmed: bool = False) -> str:
    """Experimental off-by-default fallback using Windows tel: URI handler."""
    if not _IS_WINDOWS:
        return unsupported_result("dial_via_tel_uri", "Phone Link is only supported on Windows.")
    clean = re.sub(r"[^\d+*#]", "", str(number or "").strip())
    if not clean:
        return outcome.failed("dial_via_tel_uri", "Valid phone number required.")
    if is_emergency_number(clean):
        return outcome.failed("dial_via_tel_uri", "Refused: emergency numbers must be dialed from your phone.")
    if not confirmed:
        return outcome.ask("dial_via_tel_uri", f"Confirm experimental tel: dial to {mask_phone(clean)}?")
    try:
        os.startfile(f"tel:{clean}")
        record_audit("dial_via_tel_uri", phone=clean, status_str="unverified")
        return outcome.unverified("dial_via_tel_uri", "Opened tel: URI; call state cannot be verified via URI.")
    except Exception as exc:
        return outcome.failed("dial_via_tel_uri", f"Failed to open tel: URI: {exc}")


# ── Phase 5 Operations: Photos, Apps (Android) & Optional Watcher ─────────────

def _require_android(action: str, win: Any) -> Optional[str]:
    caps = probe_capabilities(window=win)
    if caps.get("linked_os") == "iphone_or_limited_android" and not caps.get("sections", {}).get("photos"):
        return unsupported_result(action, f"{action} is only supported on Android devices.")
    return None


def op_list_recent_photos(limit: int = 20, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("list_recent_photos", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("list_recent_photos", "Phone Link window is not open.")
        blocked = _require_android("list_recent_photos", win)
        if blocked:
            return blocked

        try:
            grid, _ = resolve_control(win, "photos_grid", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("list_recent_photos", str(exc), missing_selector=exc.selector_key)

        items = _get_children(grid)[: max(1, min(int(limit), 50))]
        meta = [
            {"index": i, "label": scrub_pii(_elem_info(node)["name"] or f"Photo {i}")}
            for i, node in enumerate(items)
        ]
        record_audit("list_recent_photos", status_str="done")
        return outcome.done("list_recent_photos", count=len(meta), photos=json.dumps(meta, ensure_ascii=False))


def op_copy_photo(index: int = 0, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("copy_photo", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("copy_photo", "Phone Link window is not open.")
        blocked = _require_android("copy_photo", win)
        if blocked:
            return blocked

        try:
            grid, _ = resolve_control(win, "photos_grid", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("copy_photo", str(exc), missing_selector=exc.selector_key)

        items = _get_children(grid)
        idx = int(index)
        if not (0 <= idx < len(items)):
            return outcome.failed("copy_photo", f"Photo index {idx} out of range (0..{max(0, len(items)-1)}).")

        _click_elem(items[idx])
        time.sleep(0.1)
        _send_shortcut(win, "^c")
        record_audit("copy_photo", contact_alias=f"idx:{idx}", status_str="done")
        return outcome.done("copy_photo", index=idx, verified=True)


def op_save_photo(index: int = 0, folder: Optional[str] = None, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("save_photo", "Phone Link is only supported on Windows.")

    dest_dir = Path(folder).expanduser().resolve() if folder else (Path.home() / "Pictures").resolve()
    allowed = any(dest_dir == r or dest_dir.is_relative_to(r) for r in _approved_folders())
    if not allowed:
        return outcome.failed("save_photo", "Destination folder must be inside approved folders (Pictures, Downloads, Desktop).")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("save_photo", "Phone Link window is not open.")
        blocked = _require_android("save_photo", win)
        if blocked:
            return blocked

        try:
            grid, _ = resolve_control(win, "photos_grid", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("save_photo", str(exc), missing_selector=exc.selector_key)

        items = _get_children(grid)
        idx = int(index)
        if not (0 <= idx < len(items)):
            return outcome.failed("save_photo", f"Photo index {idx} out of range.")

        _click_elem(items[idx])
        time.sleep(0.1)
        try:
            save_btn, _ = resolve_control(win, "photos_save_btn", timeout=2.0)
            _click_elem(save_btn)
        except MissingSelectorError:
            _send_shortcut(win, "^s")

        record_audit("save_photo", contact_alias=f"idx:{idx}", status_str="unverified")
        return outcome.unverified(
            "save_photo",
            f"Triggered Save for photo {idx} targeting '{dest_dir}'; confirm file dialog if prompted.",
            folder=str(dest_dir),
        )


def op_share_photo(index: int = 0, confirmed: bool = False, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("share_photo", "Phone Link is only supported on Windows.")
    if not confirmed:
        return outcome.ask("share_photo", f"Confirm sharing photo at index {index}?")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("share_photo", "Phone Link window is not open.")
        blocked = _require_android("share_photo", win)
        if blocked:
            return blocked
        _send_shortcut(win, "^h")
        record_audit("share_photo", contact_alias=f"idx:{index}", status_str="unverified")
        return outcome.unverified("share_photo", f"Opened Windows Share sheet for photo {index}.")


def op_delete_photo(index: int = 0, confirmed: bool = False, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("delete_photo", "Phone Link is only supported on Windows.")
    if not confirmed:
        return outcome.ask("delete_photo", f"Confirm permanently deleting photo at index {index}? This cannot be undone.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("delete_photo", "Phone Link window is not open.")
        blocked = _require_android("delete_photo", win)
        if blocked:
            return blocked

        try:
            grid, _ = resolve_control(win, "photos_grid", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("delete_photo", str(exc), missing_selector=exc.selector_key)

        items_before = _get_children(grid)
        idx = int(index)
        if not (0 <= idx < len(items_before)):
            return outcome.failed("delete_photo", f"Photo index {idx} out of range.")

        _click_elem(items_before[idx])
        time.sleep(0.1)
        _send_shortcut(win, "{DELETE}")
        time.sleep(0.2)
        items_after = _get_children(grid)
        if len(items_after) < len(items_before):
            record_audit("delete_photo", contact_alias=f"idx:{idx}", status_str="done")
            return outcome.done(
                "delete_photo",
                index=idx,
                remaining=len(items_after),
                undoable=False,
                note="Photo deleted (not undoable).",
                verified=True,
            )
        record_audit("delete_photo", contact_alias=f"idx:{idx}", status_str="unverified")
        return outcome.unverified("delete_photo", "Sent Delete key, but photo count did not decrease.")


def op_list_apps(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("list_apps", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=True):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("list_apps", "Phone Link window is not open.")
        blocked = _require_android("list_apps", win)
        if blocked:
            return blocked

        try:
            apps_list, _ = resolve_control(win, "apps_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("list_apps", str(exc), missing_selector=exc.selector_key)

        apps = [_elem_info(n)["name"] for n in _get_children(apps_list) if _elem_info(n)["name"]]
        record_audit("list_apps", status_str="done")
        return outcome.done("list_apps", count=len(apps), apps=json.dumps(apps[:50], ensure_ascii=False))


def op_open_app(name: str, window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("open_app", "Phone Link is only supported on Windows.")
    if not name or not name.strip():
        return outcome.failed("open_app", "Missing required parameter 'name'.")

    with _UIA_LOCK, _ForegroundGuard(restore=False):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("open_app", "Phone Link window is not open.")
        blocked = _require_android("open_app", win)
        if blocked:
            return blocked

        try:
            apps_list, _ = resolve_control(win, "apps_list", timeout=2.5)
        except MissingSelectorError as exc:
            return outcome.failed("open_app", str(exc), missing_selector=exc.selector_key)

        q = name.strip().lower()
        for node in _get_children(apps_list):
            aname = _elem_info(node)["name"]
            if q in aname.lower():
                _click_elem(node)
                record_audit("open_app", contact_alias=aname, status_str="done")
                return outcome.done("open_app", app=aname, verified=True)
        return outcome.failed("open_app", f"Mobile app '{name}' not found in Phone Link.")


def op_open_phone_screen(window: Optional[Any] = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("open_phone_screen", "Phone Link is only supported on Windows.")

    with _UIA_LOCK, _ForegroundGuard(restore=False):
        win = window or find_phone_link_window(timeout=3.0)
        if win is None:
            return outcome.failed("open_phone_screen", "Phone Link window is not open.")
        blocked = _require_android("open_phone_screen", win)
        if blocked:
            return blocked

        try:
            btn, _ = resolve_control(win, "apps_open_screen_btn", timeout=2.0)
            _click_elem(btn)
        except MissingSelectorError:
            _send_shortcut(win, "^p")

        record_audit("open_phone_screen", status_str="done")
        return outcome.done("open_phone_screen", verified=True)


# ── Optional Low-CPU UIA Watcher (Off by Default) ─────────────────────────────

_WATCHER_STATE: dict[str, Any] = {
    "running": False,
    "thread": None,
    "stop_event": threading.Event(),
    "last_call_state": "idle",
    "last_notif_count": 0,
}


def _watcher_loop(poll_sec: float, player: Any = None) -> None:
    stop_ev: threading.Event = _WATCHER_STATE["stop_event"]
    while not stop_ev.is_set():
        try:
            if config_manager.is_phone_link_feature_enabled("watcher"):
                win = find_phone_link_window(timeout=0.5)
                if win is not None:
                    with _UIA_LOCK:
                        cs = _detect_call_state(win)
                        if cs == "incoming" and _WATCHER_STATE["last_call_state"] != "incoming":
                            if player and hasattr(player, "write_log"):
                                player.write_log("SYS: Phone Link incoming call detected.")
                        _WATCHER_STATE["last_call_state"] = cs
        except Exception:
            pass
        stop_ev.wait(max(2.0, poll_sec))
    _WATCHER_STATE["running"] = False


def op_watcher_start(poll_interval_sec: float = 4.0, player: Any = None) -> str:
    if not _IS_WINDOWS:
        return unsupported_result("watcher_start", "Phone Link is only supported on Windows.")
    if _WATCHER_STATE["running"]:
        return outcome.done("watcher_start", running=True, already_running=True)

    _WATCHER_STATE["stop_event"].clear()
    t = threading.Thread(
        target=_watcher_loop,
        args=(max(2.0, float(poll_interval_sec)), player),
        daemon=True,
        name="PhoneLinkWatcher",
    )
    _WATCHER_STATE["thread"] = t
    _WATCHER_STATE["running"] = True
    t.start()
    record_audit("watcher_start", status_str="done")
    return outcome.done("watcher_start", running=True, poll_interval_sec=max(2.0, float(poll_interval_sec)))


def op_watcher_stop() -> str:
    _WATCHER_STATE["stop_event"].set()
    _WATCHER_STATE["running"] = False
    record_audit("watcher_stop", status_str="done")
    return outcome.done("watcher_stop", running=False)


def op_watcher_status() -> str:
    return outcome.done(
        "watcher_status",
        running=bool(_WATCHER_STATE["running"]),
        last_call_state=_WATCHER_STATE["last_call_state"],
    )

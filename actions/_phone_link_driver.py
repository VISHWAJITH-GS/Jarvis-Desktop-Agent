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

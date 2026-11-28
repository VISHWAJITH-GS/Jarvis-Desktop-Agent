"""
actions/browser/provider_manager.py — Intelligent Hybrid Browser Provider Manager.

Coordinates interchangeable browser providers (EgoLite and CDP/Playwright),
handles health-aware provider selection, graceful auto-fallback, and session isolation.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple

from actions.browser.base_provider import BrowserProvider
from actions.browser.browser_models import BrowserStatus, SpaceInfo, TabInfo
from actions.browser.cdp_provider import CDPProvider
from actions.browser.ego_provider import EgoLiteProvider
from memory import config_manager

logger = logging.getLogger("provider_manager")


class BrowserProviderManager:
    """
    Unified manager for hybrid browser automation engines in JARVIS.
    Supports Ego(lite) and CDP (Chrome/Edge) with automatic and manual selection modes.
    """

    _instance: Optional[BrowserProviderManager] = None
    _singleton_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.Lock()
        self._providers: Dict[str, BrowserProvider] = {}
        self._active_provider_name: Optional[str] = None
        self._mode: str = "auto"  # "auto" | "manual"
        self._last_switch_notice: Optional[str] = None
        self._last_used_provider_name: Optional[str] = None

        self._init_providers()

    @classmethod
    def get_instance(cls) -> BrowserProviderManager:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _init_providers(self) -> None:
        """Initialize provider registry based on system and config."""
        # 1. Ego(lite) provider
        try:
            self._providers["ego"] = EgoLiteProvider()
        except Exception as e:
            logger.warning(f"Failed to instantiate EgoLiteProvider: {e}")

        # 2. CDP providers (Chrome & Edge)
        configured_engine = config_manager.get_browser_engine() or "chrome"
        try:
            self._providers["chrome"] = CDPProvider(engine="chrome")
        except Exception as e:
            logger.warning(f"Failed to instantiate Chrome CDPProvider: {e}")

        try:
            self._providers["edge"] = CDPProvider(engine="edge")
        except Exception as e:
            logger.warning(f"Failed to instantiate Edge CDPProvider: {e}")

        # Default alias 'cdp' maps to configured engine
        if configured_engine in self._providers:
            self._providers["cdp"] = self._providers[configured_engine]
        elif "chrome" in self._providers:
            self._providers["cdp"] = self._providers["chrome"]

        # Read config
        cfg = config_manager.get_browser_provider_config()
        self._mode = cfg.get("mode", "auto").lower()
        preferred = cfg.get("preferred", "ego").lower()
        self._active_provider_name = preferred

    # ── Provider Resolution & Selection ───────────────────────────────────────

    def get_provider(self, name: Optional[str] = None) -> BrowserProvider:
        """Retrieve a specific provider by name."""
        target = (name or self._active_provider_name or "ego").lower()
        if target in self._providers:
            return self._providers[target]
        if "ego" in self._providers:
            return self._providers["ego"]
        if "cdp" in self._providers:
            return self._providers["cdp"]
        raise RuntimeError(f"No valid browser provider registered for '{target}'.")

    def get_active_provider(self) -> BrowserProvider:
        """
        Intelligently resolves and returns the active BrowserProvider.
        In 'auto' mode:
          - Prefers Ego(lite) if installed and healthy.
          - Automatically falls back to CDP if Ego is unreachable.
          - Informs the system of the switch without misrepresenting session transfer.
        In 'manual' mode:
          - Strictly returns the explicitly configured provider.
        """
        with self._lock:
            cfg = config_manager.get_browser_provider_config()
            mode = cfg.get("mode", self._mode).lower()
            preferred = cfg.get("preferred", "ego").lower()
            fallback_target = cfg.get("fallback", "cdp").lower()
            auto_fallback = bool(cfg.get("auto_fallback", True))

            # Manual mode
            if mode == "manual":
                target = self._active_provider_name or preferred
                prov = self._providers.get(target) or self._providers.get("cdp")
                self._last_used_provider_name = prov.name if prov else target
                return prov

            # Auto mode
            # 1. Test preferred (Ego)
            pref_prov = self._providers.get(preferred)
            if pref_prov:
                is_healthy, h_msg = pref_prov.check_health(timeout=1.5)
                if is_healthy:
                    if self._last_used_provider_name and self._last_used_provider_name != pref_prov.name:
                        self._last_switch_notice = (
                            f"[Provider Notice] Switched to {pref_prov.name.upper()} browser engine. "
                            "Note: Sessions and cookies are kept in this engine."
                        )
                    self._last_used_provider_name = pref_prov.name
                    self._active_provider_name = preferred
                    return pref_prov

            # 2. Preferred is unavailable; check auto_fallback
            if auto_fallback:
                fb_prov = self._providers.get(fallback_target) or self._providers.get("cdp") or self._providers.get("chrome")
                if fb_prov:
                    if self._last_used_provider_name != fb_prov.name:
                        self._last_switch_notice = (
                            f"[Provider Fallback Notice] {preferred.capitalize()}(lite) is unavailable; "
                            f"automatically using {fb_prov.name.upper()}. "
                            "Notice: Browser authentication and session states are NOT transferred between engines."
                        )
                        logger.info(self._last_switch_notice)
                    self._last_used_provider_name = fb_prov.name
                    self._active_provider_name = fallback_target
                    return fb_prov

            # Fallback to whatever provider is registered
            for p in self._providers.values():
                return p

            raise RuntimeError("No browser automation providers are available.")

    def set_provider(self, name: str) -> str:
        """Explicitly switch active browser provider (sets manual mode)."""
        clean = name.strip().lower()
        if clean not in self._providers and clean not in ("auto", "ego", "chrome", "edge", "cdp"):
            return f"Invalid provider '{name}'. Available: auto, ego, chrome, edge, cdp."

        with self._lock:
            if clean == "auto":
                self._mode = "auto"
                config_manager.save_browser_provider_config(preferred="ego", mode="auto")
                return "Switched browser engine selection to AUTOMATIC mode (prefers Ego Lite with CDP fallback)."

            self._mode = "manual"
            self._active_provider_name = clean
            config_manager.save_browser_provider_config(preferred=clean, mode="manual")
            prov = self._providers.get(clean)
            if prov:
                self._last_switch_notice = (
                    f"Manually switched active browser provider to {prov.name.upper()}. "
                    "Notice: Session cookies and logins are engine-specific."
                )
            return f"Switched browser engine to {clean.upper()} (Manual mode)."

    def set_mode(self, mode: str) -> str:
        clean = mode.strip().lower()
        if clean not in ("auto", "manual"):
            return f"Invalid mode '{mode}'. Choose 'auto' or 'manual'."
        with self._lock:
            self._mode = clean
            config_manager.save_browser_provider_config(mode=clean)
            return f"Browser provider selection mode set to {clean.upper()}."

    def consume_switch_notice(self) -> Optional[str]:
        """Return and clear pending provider switch notice for inclusion in Gemini outputs."""
        with self._lock:
            n = self._last_switch_notice
            self._last_switch_notice = None
            return n

    # ── Diagnostics & Telemetry ───────────────────────────────────────────────

    def check_all_health(self) -> Dict[str, Tuple[bool, str]]:
        """Run health check against all registered browser engines."""
        results = {}
        for name, prov in self._providers.items():
            if name == "cdp":
                continue  # Alias for chrome/edge
            try:
                ok, msg = prov.check_health(timeout=1.5)
                results[name] = (ok, msg)
            except Exception as e:
                results[name] = (False, str(e))
        return results

    def get_status(self) -> BrowserStatus:
        """Return comprehensive status for active browser provider and environment."""
        prov = self.get_active_provider()
        st = prov.get_status()
        st.mode = self._mode
        cfg = config_manager.get_browser_provider_config()
        preferred = cfg.get("preferred", "ego").lower()
        st.is_fallback = (self._mode == "auto" and prov.name != preferred and not prov.name.startswith(preferred))
        st.switch_notice = self._last_switch_notice
        return st

    def restart_active_provider(self) -> str:
        prov = self.get_active_provider()
        try:
            prov.disconnect()
            prov.open_browser(headed=True)
            return f"Restarted {prov.name} browser engine."
        except Exception as e:
            return f"Error restarting {prov.name}: {e}"

    def disconnect_all(self) -> None:
        for prov in self._providers.values():
            try:
                prov.disconnect()
            except Exception:
                pass


def get_browser_provider_manager() -> BrowserProviderManager:
    """Public singleton accessor."""
    return BrowserProviderManager.get_instance()

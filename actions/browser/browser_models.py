"""
actions/browser/browser_models.py — Data contracts and models for hybrid browser architecture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TabInfo:
    """Unified tab information representation."""
    index: int
    title: str
    url: str
    target_id: str = ""
    is_active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "url": self.url,
            "target_id": self.target_id,
            "is_active": self.is_active,
        }


@dataclass
class SpaceInfo:
    """Browser workspace (Space) information for task isolation."""
    id: int | str
    name: str
    ownership: str = "agent"  # "agent", "user", "agentDelegatedToUser"
    profile_id: str = ""
    profile_name: str = ""
    is_active: bool = False
    tab_count: int = 0
    recent_titles: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ownership": self.ownership,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "is_active": self.is_active,
            "tab_count": self.tab_count,
            "recent_titles": self.recent_titles,
        }


@dataclass
class ElementRef:
    """Normalized distilled or semantic element reference."""
    index: int
    role: str = ""
    tag: str = ""
    text: str = ""
    name: str = ""
    aria_label: str = ""
    placeholder: str = ""
    value: str = ""
    loc: str = ""
    raw_ref: str = ""
    is_focused: bool = False

    def to_prompt_line(self) -> str:
        parts = [f"[{self.index}]"]
        role_label = self.role or self.tag or "element"
        parts.append(f"<{role_label}>")
        if self.text:
            cleaned_text = " ".join(self.text.split())
            if len(cleaned_text) > 80:
                cleaned_text = cleaned_text[:77] + "..."
            parts.append(f'text="{cleaned_text}"')
        if self.aria_label and self.aria_label != self.text:
            parts.append(f'aria-label="{self.aria_label}"')
        if self.placeholder:
            parts.append(f'placeholder="{self.placeholder}"')
        if self.value and not self.text:
            parts.append(f'value="{self.value}"')
        if self.name and not self.text and not self.aria_label:
            parts.append(f'name="{self.name}"')
        if self.is_focused:
            parts.append("focused=true")
        return " ".join(parts)


@dataclass
class BrowserStatus:
    """Complete health and operational status of browser subsystem."""
    provider: str  # "ego", "chrome", "edge", "cdp"
    is_connected: bool
    version: str = "unknown"
    active_space: Optional[str | int] = None
    active_title: str = ""
    active_url: str = ""
    tab_count: int = 0
    tabs: List[TabInfo] = field(default_factory=list)
    current_task: Optional[str] = None
    health: str = "healthy"  # "healthy", "degraded", "disconnected", "error"
    last_error: Optional[str] = None
    spaces: List[SpaceInfo] = field(default_factory=list)
    console_errors_count: int = 0
    network_errors_count: int = 0
    mode: str = "auto"  # "auto", "manual"
    is_fallback: bool = False
    switch_notice: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "is_connected": self.is_connected,
            "version": self.version,
            "active_space": self.active_space,
            "active_title": self.active_title,
            "active_url": self.active_url,
            "tab_count": self.tab_count,
            "tabs": [t.to_dict() for t in self.tabs],
            "current_task": self.current_task,
            "health": self.health,
            "last_error": self.last_error,
            "spaces": [s.to_dict() for s in self.spaces],
            "console_errors_count": self.console_errors_count,
            "network_errors_count": self.network_errors_count,
            "mode": self.mode,
            "is_fallback": self.is_fallback,
            "switch_notice": self.switch_notice,
            "timestamp": self.timestamp,
        }

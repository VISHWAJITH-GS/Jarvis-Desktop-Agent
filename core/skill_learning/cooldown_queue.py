"""
core/skill_learning/cooldown_queue.py — API Quota & Cooldown Queue for Skill Learning.

Handles rate limits and API quota exhaustion (e.g., Gemini 429 / RESOURCE_EXHAUSTED):
- Tracks cooldown expiry timestamps thread-safely.
- Enqueues pending skill learning requests (demonstrations or descriptions).
- Notifies user once with remaining cooldown seconds via player.write_log without spamming.
- Allows resuming pending requests when cooldown expires.
"""

from __future__ import annotations

import logging
from threading import RLock
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def is_quota_error(exc_or_msg: Any) -> bool:
    """Check if an exception or message indicates an API rate limit or quota exhaustion."""
    if not exc_or_msg:
        return False
    msg = str(exc_or_msg).lower()
    return any(
        term in msg
        for term in (
            "429",
            "quota",
            "resource_exhausted",
            "rate_limit",
            "rate limit",
            "too many requests",
            "quota exceeded",
        )
    )


class CooldownQueue:
    """Thread-safe cooldown timer and request queue for skill learning tasks."""

    _instance: Optional[CooldownQueue] = None
    _lock = RLock()

    def __init__(self) -> None:
        self.cooldown_until: float = 0.0
        self.last_notification_time: float = 0.0
        self.queue: List[Dict[str, Any]] = []

    @classmethod
    def get_instance(cls) -> CooldownQueue:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def set_cooldown(self, seconds: float = 60.0) -> None:
        """Set or extend cooldown timer."""
        with self._lock:
            now = time.monotonic()
            proposed = now + max(0.0, float(seconds))
            if proposed > self.cooldown_until:
                self.cooldown_until = proposed
            logger.warning("Skill learning placed on cooldown for %.1f seconds.", seconds)

    def is_in_cooldown(self) -> bool:
        """Check if currently waiting on cooldown."""
        with self._lock:
            return time.monotonic() < self.cooldown_until

    def get_remaining_cooldown(self) -> float:
        """Return remaining seconds of cooldown (0 if expired)."""
        with self._lock:
            rem = self.cooldown_until - time.monotonic()
            return max(0.0, round(rem, 1))

    def enqueue(self, req_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Enqueue a skill learning request to be resumed after cooldown."""
        with self._lock:
            item = {
                "type": req_type,
                "data": data,
                "created_at": time.time(),
            }
            self.queue.append(item)
            logger.info("Enqueued skill request (%s). Queue size: %d", req_type, len(self.queue))
            return item

    def dequeue(self) -> Optional[Dict[str, Any]]:
        """Pop the oldest queued skill learning request if cooldown has expired."""
        with self._lock:
            if self.is_in_cooldown() or not self.queue:
                return None
            return self.queue.pop(0)

    def peek(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.queue[0] if self.queue else None

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self.queue)

    def clear(self) -> None:
        with self._lock:
            self.queue.clear()
            self.cooldown_until = 0.0
            self.last_notification_time = 0.0

    def notify_user_cooldown(self, player=None, custom_msg: Optional[str] = None) -> str:
        """Notify user once about the cooldown without spamming."""
        with self._lock:
            now = time.monotonic()
            rem = self.get_remaining_cooldown()
            msg = custom_msg or f"API quota reached. Skill learning request is on cooldown ({rem:.0f}s remaining)."

            # Only notify once every 15 seconds to avoid spamming
            if (now - self.last_notification_time) >= 15.0:
                self.last_notification_time = now
                if player and hasattr(player, "write_log"):
                    player.write_log(f"⏳ {msg}")
                logger.warning(msg)

            return msg

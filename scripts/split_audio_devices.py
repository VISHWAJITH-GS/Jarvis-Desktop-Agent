"""
scripts/split_audio_devices.py — Extract WASAPI endpoint management from core/audio_devices.py
into core/audio_endpoints.py.
"""

from __future__ import annotations


def main():
    with open("core/audio_devices.py", "r", encoding="utf-8") as f:
        lines = f.readlines()

    s1 = None
    e1 = None
    s2 = None
    e2 = len(lines)

    for i, l in enumerate(lines):
        if l.startswith("def get_current_default_endpoint("):
            s1 = i
        elif l.startswith("def select_best_input("):
            e1 = i
        elif l.startswith("def _endpoint_for("):
            s2 = i

    block1 = lines[s1:e1]
    block2 = lines[s2:e2]

    endpoints_header = '''"""
core/audio_endpoints.py — Windows CoreAudio/WASAPI audio endpoints and volume management.
"""
from __future__ import annotations

import logging
import os
import platform
import re
from typing import Optional, Set, Tuple

from core import platform_hooks

logger = logging.getLogger("jarvis.audio_endpoints")

def _platform_is() -> str:
    return platform.system()

def _fold(text: str) -> str:
    try:
        from core.appidentity import fold
        return fold(text)
    except Exception:
        return "".join(c for c in str(text or "").lower() if c.isalnum())

'''
    endpoints_content = [endpoints_header] + block1 + block2
    with open("core/audio_endpoints.py", "w", encoding="utf-8") as f:
        f.writelines(endpoints_content)
    print(f"Wrote core/audio_endpoints.py: {len(endpoints_content)} lines")

    # Replace block1 and block2 in core/audio_devices.py
    import_bridge = [
        "\n",
        "# ── Re-export WASAPI / CoreAudio endpoint helpers from core.audio_endpoints ──\n",
        "from core.audio_endpoints import (\n",
        "    get_current_default_endpoint,\n",
        "    get_connected_endpoints_snapshot,\n",
        "    set_default_endpoint,\n",
        "    _is_wasapi_endpoint,\n",
        "    _endpoint_for,\n",
        "    _platform_is,\n",
        "    _fold,\n",
        "    get_output_volume,\n",
        "    set_output_volume,\n",
        ")\n",
        "\n",
    ]

    new_audio_devices = lines[:s1] + import_bridge + lines[e1:s2]
    with open("core/audio_devices.py", "w", encoding="utf-8") as f:
        f.writelines(new_audio_devices)
    print(f"Updated core/audio_devices.py: {len(new_audio_devices)} lines")


if __name__ == "__main__":
    main()

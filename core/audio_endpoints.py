"""
core/audio_endpoints.py — Windows CoreAudio/WASAPI audio endpoints and volume management.
"""

from __future__ import annotations

import logging
import os
import platform
import re
from typing import Optional, Set, Tuple, Any

from core import platform_hooks

logger = logging.getLogger("jarvis.audio_endpoints")


def _get_devices_module() -> Any:
    from core import audio_devices

    return audio_devices


def _display_name(name: str, devices: Any) -> str:
    return _get_devices_module()._display_name(name, devices)


def _invalidate_cache() -> None:
    _get_devices_module().invalidate_cache()


DEFAULT_LABEL = "System default"


def _platform_is() -> str:
    return platform.system()


def _fold(text: str) -> str:
    try:
        from core.appidentity import fold

        return fold(text)
    except Exception:
        return "".join(c for c in str(text or "").lower() if c.isalnum())


def get_current_default_endpoint(kind: str = "output") -> tuple[str | None, str | None]:
    """Return (endpoint_id, friendly_name) for current default Windows audio device."""
    if _platform_is() != "Windows":
        return (None, None)
    try:
        import comtypes

        comtypes.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities

            if kind == "output":
                dev = AudioUtilities.GetSpeakers()
            else:
                dev = AudioUtilities.GetMicrophone()
            if dev is not None:
                ep_id = getattr(dev, "id", None)
                ep_fn = getattr(dev, "FriendlyName", None)
                if ep_fn:
                    return (ep_id, ep_fn)
                # Fall through to sounddevice display name while preserving ep_id
                try:
                    import sounddevice as sd

                    info = sd.query_devices(kind=kind)
                    if info and info.get("name"):
                        return (ep_id, _display_name(str(info["name"]).strip(), list(sd.query_devices())))
                except Exception:
                    pass
                return (ep_id, None)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass
    # Fallback to sounddevice default device name when pycaw GetMicrophone/GetSpeakers returns None
    try:
        import sounddevice as sd

        info = sd.query_devices(kind=kind)
        if info and info.get("name"):
            return (None, _display_name(str(info["name"]).strip(), list(sd.query_devices())))
    except Exception:
        pass
    return (None, None)


def get_connected_endpoints_snapshot() -> set[str]:
    """Return set of ACTIVE (connected) endpoint IDs and friendly names.

    Filters out NotPresent / Unplugged / Disabled remembered Windows Bluetooth
    endpoints so connect/disconnect hotplug transitions are detected accurately.
    """
    snapshot: set[str] = set()
    if _platform_is() != "Windows":
        return snapshot
    try:
        import comtypes

        comtypes.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities

            for dev in AudioUtilities.GetAllDevices():
                state = getattr(dev, "state", None)
                if state is not None and "active" not in str(state).lower():
                    continue
                d_id = getattr(dev, "id", None)
                if d_id:
                    snapshot.add(str(d_id))
                fn = getattr(dev, "FriendlyName", None)
                if fn:
                    snapshot.add(str(fn))
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass
    return snapshot


def set_default_endpoint(device_name_or_id: str, kind: str = "output") -> bool:
    """Set the Windows default audio endpoint (Console, Multimedia, Communications).

    Accepts an endpoint ID (e.g. '{0.0.0.00000000}.{...}') or a friendly device name.
    Degrades safely on non-Windows platforms.
    """
    if _platform_is() != "Windows":
        return False
    target = (device_name_or_id or "").strip()
    if not target or target.lower() == DEFAULT_LABEL.lower():
        return False

    try:
        import ctypes
        import comtypes
        import comtypes.client
        from comtypes import GUID, COMMETHOD, IUnknown
        from pycaw.pycaw import AudioUtilities

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
            _methods_ = [
                COMMETHOD(
                    [],
                    ctypes.HRESULT,
                    "GetMixFormat",
                    (["in"], ctypes.c_wchar_p, "pwszDeviceId"),
                    (["out"], ctypes.c_void_p, "ppFormat"),
                ),
                COMMETHOD([], ctypes.HRESULT, "GetDeviceFormat"),
                COMMETHOD([], ctypes.HRESULT, "ResetDeviceFormat"),
                COMMETHOD([], ctypes.HRESULT, "SetDeviceFormat"),
                COMMETHOD([], ctypes.HRESULT, "GetProcessingPeriod"),
                COMMETHOD([], ctypes.HRESULT, "SetProcessingPeriod"),
                COMMETHOD([], ctypes.HRESULT, "GetShareMode"),
                COMMETHOD([], ctypes.HRESULT, "SetShareMode"),
                COMMETHOD([], ctypes.HRESULT, "GetPropertyValue"),
                COMMETHOD([], ctypes.HRESULT, "SetPropertyValue"),
                COMMETHOD(
                    [],
                    ctypes.HRESULT,
                    "SetDefaultEndpoint",
                    (["in"], ctypes.c_wchar_p, "pwszDeviceId"),
                    (["in"], ctypes.c_uint, "role"),
                ),
                COMMETHOD([], ctypes.HRESULT, "SetEndpointVisibility"),
            ]

        comtypes.CoInitialize()
        try:
            target_id = None
            if target.startswith("{") and "}" in target:
                target_id = target
            else:
                target_low = _fold(target)
                for dev in AudioUtilities.GetAllDevices():
                    dev_fn = _fold(getattr(dev, "FriendlyName", "") or "")
                    if target_low in dev_fn or dev_fn in target_low:
                        target_id = getattr(dev, "id", None)
                        break

            if not target_id:
                return False

            CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
            policy = comtypes.client.CreateObject(CLSID_PolicyConfigClient, interface=IPolicyConfig)
            # 0: eConsole, 1: eMultimedia, 2: eCommunications
            for role in (0, 1, 2):
                policy.SetDefaultEndpoint(target_id, role)
            _invalidate_cache()
            return True
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    except Exception as e:
        logger.debug("Failed to set default endpoint via IPolicyConfig: %s", e)
        return False


def _is_wasapi_endpoint(name: str) -> bool:
    """Check if an input device name or endpoint corresponds to WASAPI."""
    if not name:
        return False
    low = name.lower()
    if "wasapi" in low:
        return True
    try:
        import sounddevice as sd

        apis = [a.get("name", "").lower() for a in sd.query_hostapis()]
        target = _fold(name)
        for d in sd.query_devices():
            if d.get("max_input_channels", 0) > 0:
                d_name = d.get("name") or ""
                if d_name == name or _fold(d_name) == target:
                    h_idx = d.get("hostapi", -1)
                    if 0 <= h_idx < len(apis) and "wasapi" in apis[h_idx]:
                        return True
    except Exception:
        pass
    return False


def _endpoint_for(name: str):
    """The Windows volume interface for ONE named output, or None.

    WHY THIS IS NOT JUST THE DEFAULT DEVICE
        The first version set the system's default endpoint, so moving the
        slider in this panel changed the volume of whichever output Windows
        happened to consider default — not the one chosen two lines above it.

        That is wrong in a way that matters to how people actually use this:
        somebody puts the assistant on their speakers and keeps everything else
        in their headphones, and a slider labelled with one device silently
        moving the other is worse than having no slider.

        It also explains a complaint that looked separate. The selected output
        here was sitting at 10% while the default endpoint was at 100% — so the
        assistant was almost inaudible, and the slider "fixed" it by turning up
        something the user could not hear either.

    Matching is by the name Windows itself reports, which is the same string
    sounddevice lists, folded so case and punctuation cannot separate them.
    """
    if _platform_is() != "Windows" or not name:
        return None
    try:
        from pycaw.pycaw import AudioUtilities

        wanted = _fold(name)
        if not wanted:
            return None

        # Collect first, decide after. An earlier version returned the first
        # candidate that matched on a PREFIX, which meant a loosely-similar
        # name could win over the exact one simply by coming first in the
        # enumeration — and the slider then moved a device the user had not
        # chosen. Exact wins, always; the prefix pass exists only because MME
        # truncates long names to 31 characters.
        exact, partial = None, None
        for dev in AudioUtilities.GetAllDevices():
            # ONLY ACTIVE ENDPOINTS, AND THIS IS NOT A TIDINESS FILTER.
            #
            # Windows keeps a record of hardware that is no longer here, and it
            # keeps the SAME FRIENDLY NAME. Measured on this machine, two
            # devices answered to "Hoparlör (Realtek(R) Audio)": one
            # NotPresent sitting at 100%, and the real one, Active, at 10%.
            # Matching by name alone returned whichever came first — so the
            # panel read 100% for a speaker that was actually at 10%, and
            # setting it moved a device that does not exist.
            try:
                if str(getattr(dev, "state", "")) != "AudioDeviceState.Active":
                    continue
            except Exception:
                continue
            try:
                # Reading this property activates a COM interface and raises
                # for devices that are unplugged or disabled — normal, and not
                # worth a line in the log for each one.
                endpoint = dev.EndpointVolume
            except Exception:
                continue
            if endpoint is None:
                continue
            try:
                friendly = _fold(getattr(dev, "FriendlyName", "") or "")
            except Exception:
                continue
            if not friendly:
                continue
            if friendly == wanted:
                exact = endpoint
                break
            if partial is None and (friendly.startswith(wanted[:24]) or wanted.startswith(friendly[:24])):
                partial = endpoint
        return exact or partial
    except Exception as e:
        print(f"[Audio] per-device volume unavailable: {e}")
    return None


def get_output_volume(name: str = "") -> Optional[int]:
    """Volume 0-100 of ONE output, or of the system default when unnamed.

    None where the platform will not report it — macOS and Linux have no
    per-device volume through this plumbing, so a name there falls back to the
    master level rather than silently answering about the wrong device.
    """
    endpoint = _endpoint_for(name)
    if endpoint is not None:
        try:
            return max(0, min(100, round(endpoint.GetMasterVolumeLevelScalar() * 100)))
        except Exception as e:
            print(f"[Audio] could not read '{name}' volume: {e}")
    try:
        from core import platform_hooks

        vol_fn = platform_hooks.get_hook("volume_get")
        if vol_fn:
            return vol_fn()
        return None
    except Exception as e:
        print(f"[Audio] could not read the output volume: {e}")
        return None


def set_output_volume(percent: int, name: str = "") -> bool:
    """Set ONE output's volume 0-100. -> did it take.

    Falls back to the system master only when the named device cannot be
    reached, and says so, because silently moving a different device's volume
    is the bug this function exists to fix.
    """
    percent = max(0, min(100, int(percent)))
    endpoint = _endpoint_for(name)
    if endpoint is not None:
        try:
            endpoint.SetMasterVolumeLevelScalar(percent / 100.0, None)
            return True
        except Exception as e:
            print(f"[Audio] could not set '{name}' volume: {e}")
    if name:
        print(f"[Audio] '{name}' has no separate volume control here — setting the system level instead.")
    try:
        from core import platform_hooks

        vol_set_fn = platform_hooks.get_hook("volume_set")
        vol_get_fn = platform_hooks.get_hook("volume_get")
        if vol_set_fn:
            vol_set_fn(percent)
            now = vol_get_fn() if vol_get_fn else None
            return now is None or abs(now - percent) <= 3
        return False
    except Exception as e:
        print(f"[Audio] could not set the output volume: {e}")
        return False

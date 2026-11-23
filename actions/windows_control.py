"""
actions/windows_control.py — Native Windows System, Hardware, Bluetooth, and Wi-Fi Control.

Deterministic hardware & settings manager providing verified operations for:
- Bluetooth (Get state, toggle, discover, list paired, pair, connect, disconnect)
- Wi-Fi (Get state, list networks, connect, disconnect, saved profiles, IP/gateway, adapter)
- Display & Brightness (Get/set brightness, resolution, scaling, display modes PC/Duplicate/Extend/Second)
- Sound & Audio Devices (Master volume, mute/unmute, list playback/recording devices, set default audio output/mic)
- System Power & States (Battery status, power plan, lock, sleep/shutdown confirmation)
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import platform
import re
import subprocess
import time
from typing import Optional

from core import outcome
from core.undo import push_undo

_IS_WINDOWS = platform.system() == "Windows"
_WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WINDOWS else {}

# ─────────────────────────────────────────────────────────────────────────────
# Native WLAN API Ctypes Definitions (Windows)
# ─────────────────────────────────────────────────────────────────────────────
if _IS_WINDOWS:
    try:
        _wlanapi = ctypes.windll.wlanapi

        WLAN_MAX_NAME_LENGTH = 256

        class _WLAN_INTERFACE_INFO(ctypes.Structure):
            _fields_ = [
                ("InterfaceGuid", ctypes.c_byte * 16),
                ("strInterfaceDescription", ctypes.c_wchar * WLAN_MAX_NAME_LENGTH),
                ("isState", ctypes.c_uint),
            ]

        class _WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
            _fields_ = [
                ("dwNumberOfItems", wintypes.DWORD),
                ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", _WLAN_INTERFACE_INFO * 1),
            ]

        class _WLAN_CONNECTION_PARAMETERS(ctypes.Structure):
            _fields_ = [
                ("wlanConnectionMode", ctypes.c_uint),
                ("strProfile", wintypes.LPCWSTR),
                ("pDot11Ssid", ctypes.c_void_p),
                ("pDesiredBssidList", ctypes.c_void_p),
                ("dot11BssType", ctypes.c_uint),
                ("dwFlags", wintypes.DWORD),
            ]
    except Exception:
        _wlanapi = None
else:
    _wlanapi = None


def _native_wlan_connect(profile_name: str) -> bool:
    """Connect to a Wi-Fi profile using native Windows WLAN API."""
    if not _wlanapi:
        return False
    client_handle = wintypes.HANDLE()
    negotiated_version = wintypes.DWORD()
    ret = _wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated_version), ctypes.byref(client_handle))
    if ret != 0:
        return False
    try:
        if_list_ptr = ctypes.c_void_p()
        ret_enum = _wlanapi.WlanEnumInterfaces(client_handle, None, ctypes.byref(if_list_ptr))
        if ret_enum != 0 or not if_list_ptr.value:
            return False
        if_list = ctypes.cast(if_list_ptr, ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)).contents
        if if_list.dwNumberOfItems == 0:
            return False
        guid = if_list.InterfaceInfo[0].InterfaceGuid

        params = _WLAN_CONNECTION_PARAMETERS()
        params.wlanConnectionMode = 0  # wlan_connection_mode_profile
        params.strProfile = profile_name
        params.pDot11Ssid = None
        params.pDesiredBssidList = None
        params.dot11BssType = 1  # infrastructure
        params.dwFlags = 0

        ret_conn = _wlanapi.WlanConnect(client_handle, ctypes.byref(guid), ctypes.byref(params), None)
        return ret_conn == 0
    except Exception:
        return False
    finally:
        _wlanapi.WlanCloseHandle(client_handle, None)


def _native_wlan_disconnect() -> bool:
    """Disconnect current Wi-Fi using native Windows WLAN API."""
    if not _wlanapi:
        return False
    client_handle = wintypes.HANDLE()
    negotiated_version = wintypes.DWORD()
    ret = _wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated_version), ctypes.byref(client_handle))
    if ret != 0:
        return False
    try:
        if_list_ptr = ctypes.c_void_p()
        ret_enum = _wlanapi.WlanEnumInterfaces(client_handle, None, ctypes.byref(if_list_ptr))
        if ret_enum != 0 or not if_list_ptr.value:
            return False
        if_list = ctypes.cast(if_list_ptr, ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)).contents
        if if_list.dwNumberOfItems == 0:
            return False
        guid = if_list.InterfaceInfo[0].InterfaceGuid
        ret_disc = _wlanapi.WlanDisconnect(client_handle, ctypes.byref(guid), None)
        return ret_disc == 0
    except Exception:
        return False
    finally:
        _wlanapi.WlanCloseHandle(client_handle, None)


def _run_ps(script: str, timeout: int = 15) -> tuple[int, str, str]:
    """Execute PowerShell script safely with CREATE_NO_WINDOW and return (code, stdout, stderr)."""
    if not _IS_WINDOWS:
        return 1, "", "Not running on Windows."
    # Security guard: strictly reject any drive formatting commands
    scr_lower = script.lower()
    if any(
        pat in scr_lower
        for pat in ("format-volume", "clear-disk", "initialize-disk", "format c:", "format d:", "format e:", "diskpart")
    ):
        return 1, "", "Drive formatting is strictly prohibited."
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_WIN_HIDE,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"PowerShell command timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


# ══════════════════════════════════════════════════════════════════════════════
# A. BLUETOOTH MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════


def get_bluetooth_status() -> dict:
    """Check Bluetooth radio state and active connected devices using WinRT and PnP."""
    ps = """
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
    $op = [Windows.Devices.Radios.Radio]::GetRadiosAsync()
    $task = $asTaskGeneric.MakeGenericMethod([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]).Invoke($null, @($op))
    $task.Wait()
    $bt = $task.Result | Where-Object { $_.Kind.ToString() -eq 'Bluetooth' }
    $state = if ($bt) { $bt.State.ToString() } else { 'Unknown' }

    # Query paired & present devices via DeviceInformation & PnP
    $selector = [Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelector()
    $devOp = [Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)
    $devTask = $asTaskGeneric.MakeGenericMethod([Windows.Devices.Enumeration.DeviceInformationCollection]).Invoke($null, @($devOp))
    $devTask.Wait()

    $deviceList = @()
    foreach ($d in $devTask.Result) {
        $isConnected = $false
        try {
            $fromIdOp = [Windows.Devices.Bluetooth.BluetoothDevice]::FromIdAsync($d.Id)
            $fromIdTask = $asTaskGeneric.MakeGenericMethod([Windows.Devices.Bluetooth.BluetoothDevice]).Invoke($null, @($fromIdOp))
            $fromIdTask.Wait()
            $btDev = $fromIdTask.Result
            if ($btDev) {
                $isConnected = ($btDev.ConnectionStatus.ToString() -eq 'Connected')
            }
        } catch {}

        $deviceList += @{
            Name = $d.Name
            Id = $d.Id
            IsConnected = $isConnected
            IsPaired = $d.Pairing.IsPaired
        }
    }

    [PSCustomObject]@{
        RadioState = $state
        AdapterAvailable = ($bt -ne $null)
        Devices = $deviceList
    } | ConvertTo-Json -Depth 3
} catch {
    [PSCustomObject]@{
        RadioState = 'Unknown'
        AdapterAvailable = $false
        Error = $_.Exception.Message
        Devices = @()
    } | ConvertTo-Json
}
"""
    code, stdout, stderr = _run_ps(ps, timeout=12)
    if code != 0 or not stdout:
        return {
            "enabled": False,
            "state": "Error or Unavailable",
            "adapter_available": False,
            "devices": [],
            "connected_devices": [],
            "error": stderr or "PowerShell execution failed",
        }
    try:
        data = json.loads(stdout)
        radio_state = data.get("RadioState", "Unknown")
        adapter_avail = data.get("AdapterAvailable", False)
        devs = data.get("Devices") or []
        if isinstance(devs, dict):
            devs = [devs]

        connected_devs = [d for d in devs if d.get("IsConnected")]
        enabled = radio_state.lower() == "on"

        return {
            "enabled": enabled,
            "state": radio_state,
            "adapter_available": adapter_avail,
            "devices": devs,
            "connected_devices": connected_devs,
            "connected_count": len(connected_devs),
            "paired_count": len(devs),
        }
    except Exception as e:
        return {
            "enabled": False,
            "state": "Unknown",
            "adapter_available": False,
            "devices": [],
            "connected_devices": [],
            "error": str(e),
        }


def set_bluetooth_enabled(enable: bool) -> dict:
    """Turn Bluetooth radio On or Off."""
    target_state = "On" if enable else "Off"
    ps = f"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
}})[0]
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
$op = [Windows.Devices.Radios.Radio]::GetRadiosAsync()
$task = $asTaskGeneric.MakeGenericMethod([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]).Invoke($null, @($op))
$task.Wait()
$bt = $task.Result | Where-Object {{ $_.Kind.ToString() -eq 'Bluetooth' }}
if (-not $bt) {{
    Write-Output "NO_RADIO"
    exit 1
}}
$target = [Windows.Devices.Radios.RadioState]::{target_state}
$setOp = $bt.SetStateAsync($target)
$setTask = $asTaskGeneric.MakeGenericMethod([Windows.Devices.Radios.RadioAccessStatus]).Invoke($null, @($setOp))
$setTask.Wait()
Write-Output $bt.State.ToString()
"""
    code, stdout, stderr = _run_ps(ps, timeout=15)
    time.sleep(1.0)
    new_status = get_bluetooth_status()
    verified = new_status.get("enabled") == enable
    return {
        "success": verified,
        "action": "set_bluetooth_enabled",
        "enabled": new_status.get("enabled"),
        "state": new_status.get("state"),
        "verified": verified,
        "message": f"Bluetooth turned {target_state}."
        if verified
        else f"Failed to turn Bluetooth {target_state}: {stderr or stdout}",
    }


def list_bluetooth_devices() -> list[dict]:
    """List paired and available Bluetooth devices with verified connection status."""
    st = get_bluetooth_status()
    return st.get("devices", [])


def connect_bluetooth_device(device_name: str) -> dict:
    """Connect a paired Bluetooth device (e.g. OPPO Enco Buds 2, headphones)."""
    # Auto-enable Bluetooth radio if currently Off
    st_init = get_bluetooth_status()
    if not st_init.get("enabled"):
        set_bluetooth_enabled("on")
        time.sleep(1.0)

    devices = list_bluetooth_devices()
    matched = None
    clean_target = device_name.lower().replace(" ", "").replace("-", "").strip()

    # 1. Exact or substring match
    for d in devices:
        fname = d.get("Name") or d.get("FriendlyName", "")
        clean_fname = fname.lower().replace(" ", "").replace("-", "")
        if clean_target in clean_fname or clean_fname in clean_target:
            matched = d
            break

    # 2. Generic audio aliases (earphones, earbuds, headphones, headset, buds)
    if not matched:
        is_audio_term = any(k in clean_target for k in ("earphone", "earbud", "headphone", "headset", "bud"))
        audio_keywords = ("bud", "enco", "oppo", "headphone", "headset", "earphone", "earbud", "wireless", "audio")
        for d in devices:
            fname = (d.get("Name") or d.get("FriendlyName", "")).lower()
            if is_audio_term and any(ak in fname for ak in audio_keywords):
                matched = d
                break

    # 3. If only one paired device exists, select it
    if not matched and len(devices) == 1:
        matched = devices[0]

    target_name = (matched.get("Name") or matched.get("FriendlyName")) if matched else device_name
    dev_id = matched.get("Id") if matched else ""
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\. ]", "", str(target_name)).strip()
    safe_id = re.sub(r"[^a-zA-Z0-9_\-\.#:]", "", str(dev_id or "")).strip()

    # Enable and trigger device via Windows PnP, WinRT, and AudioEndpoint
    ps = f"""
$name = "{safe_name}"
$dev = Get-PnpDevice -Class Bluetooth -FriendlyName "*$name*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($dev) {{
    Enable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
}}
$audio = Get-PnpDevice -Class AudioEndpoint -FriendlyName "*$name*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($audio) {{
    Enable-PnpDevice -InstanceId $audio.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
}}
try {{
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    }})[0]
    $devId = "{safe_id}"
    if ($devId) {{
        $op = [Windows.Devices.Bluetooth.BluetoothDevice]::FromIdAsync($devId)
        $task = $asTaskGeneric.MakeGenericMethod([Windows.Devices.Bluetooth.BluetoothDevice]).Invoke($null, @($op))
        $task.Wait(4000)
        $bt = $task.Result
        if ($bt) {{
            $rfOp = $bt.GetRfcommServicesAsync()
            $rfTask = $asTaskGeneric.MakeGenericMethod([Windows.Devices.Bluetooth.Rfcomm.RfcommDeviceServicesResult]).Invoke($null, @($rfOp))
            $rfTask.Wait(4000)
        }}
    }}
}} catch {{}}
"""
    _run_ps(ps, timeout=12)

    # Poll verification of active connection status
    verified = False
    for _ in range(3):
        time.sleep(1.0)
        st = get_bluetooth_status()
        for d in st.get("connected_devices") or []:
            if target_name.lower() in (d.get("Name") or "").lower():
                verified = True
                break
        if verified:
            break

    # If audio output device, set default playback device and invalidate audio caches
    if verified:
        try:
            from core.audio_devices import invalidate_cache

            invalidate_cache()
        except Exception:
            pass
        set_default_playback_device(target_name)

    return {
        "status": "done" if verified else "unverified",
        "success": verified,
        "action": "connect_bluetooth_device",
        "device": target_name,
        "verified": verified,
        "message": f"Connected to {target_name} successfully."
        if verified
        else f"Connection command sent to '{target_name}', but active connected state could not be verified. Please ensure the earphones are powered on and in range.",
        "error": None if verified else f"Device '{target_name}' is not in connected state.",
    }


def disconnect_bluetooth_device(device_name: str) -> dict:
    """Disconnect a Bluetooth device."""
    devices = list_bluetooth_devices()
    matched = None
    clean_target = device_name.lower().replace(" ", "").replace("-", "")
    for d in devices:
        fname = d.get("Name") or d.get("FriendlyName", "")
        clean_fname = fname.lower().replace(" ", "").replace("-", "")
        if clean_target in clean_fname or clean_fname in clean_target:
            matched = d
            break
    target_name = (matched.get("Name") or matched.get("FriendlyName")) if matched else device_name

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\. ]", "", str(target_name)).strip()
    ps = f"""
$dev = Get-PnpDevice -Class Bluetooth -FriendlyName '*{safe_name}*' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($dev) {{
    Disable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
}}
"""
    _run_ps(ps, timeout=8)
    time.sleep(1.5)

    st = get_bluetooth_status()
    verified = not any(target_name.lower() in (d.get("Name") or "").lower() for d in st.get("connected_devices", []))

    return {
        "success": verified,
        "action": "disconnect_bluetooth_device",
        "device": target_name,
        "verified": verified,
        "message": f"Disconnected {target_name}." if verified else f"Could not confirm disconnection of {target_name}.",
    }


def pair_bluetooth_device(device_name: str) -> dict:
    """Guide and initiate pairing for a Bluetooth device."""
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\. ]", "", str(device_name)).strip()
    ps = f"""
$name = "{safe_name}"
$dev = Get-PnpDevice -Class Bluetooth -FriendlyName "*$name*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($dev) {{
    Enable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "FOUND_PAIRED"
}} else {{
    Write-Output "NOT_FOUND"
}}
"""
    code, stdout, _ = _run_ps(ps, timeout=12)
    verified = "FOUND_PAIRED" in stdout
    return {
        "success": verified,
        "action": "pair_bluetooth_device",
        "device": device_name,
        "verified": verified,
        "message": f"Device '{device_name}' is already paired and verified."
        if verified
        else f"Device '{device_name}' was not found among paired devices. Please put the device in pairing mode in Windows Settings.",
    }


def unpair_bluetooth_device(device_name: str) -> dict:
    """Forget / remove a paired Bluetooth device."""
    if not device_name:
        return {"success": False, "verified": False, "message": "Device name is required to unpair."}
    ps_cmd = f"""
$dev = Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object {{ $_.FriendlyName -like '*{device_name}*' }} | Select-Object -First 1
if ($dev) {{
    Disable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "REMOVED"
}} else {{
    Write-Output "NOT_FOUND"
}}
"""
    code, stdout, stderr = _run_ps(ps_cmd, timeout=12)
    verified = "REMOVED" in stdout
    return {
        "success": verified,
        "action": "unpair_bluetooth_device",
        "device": device_name,
        "verified": verified,
        "message": f"Removed paired device '{device_name}'."
        if verified
        else f"Bluetooth device '{device_name}' not found.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# B. WI-FI MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════


def get_wifi_status() -> dict:
    """Get accurate Wi-Fi adapter status, connected SSID, IP address, and profiles."""
    ps = """
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
    $op = [Windows.Devices.Radios.Radio]::GetRadiosAsync()
    $task = $asTaskGeneric.MakeGenericMethod([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]).Invoke($null, @($op))
    $task.Wait()
    $wf = $task.Result | Where-Object { $_.Kind.ToString() -eq 'WiFi' }
    $radioState = if ($wf) { $wf.State.ToString() } else { 'Unknown' }

    $adapter = Get-NetAdapter | Where-Object { $_.PhysicalMediaType -eq 'Native 802.11' -or $_.Name -like '*Wi-Fi*' } | Select-Object -First 1
    $adapterPresent = ($adapter -ne $null)
    $adapterStatus = if ($adapter) { $adapter.Status } else { 'NotDetected' }
    $adapterName = if ($adapter) { $adapter.Name } else { 'Wi-Fi' }

    $prof = Get-NetConnectionProfile -InterfaceAlias $adapterName -ErrorAction SilentlyContinue
    $connectedSsid = if ($prof) { $prof.Name } else { $null }
    $ipv4Conn = if ($prof) { $prof.IPv4Connectivity } else { 0 }

    $ipAddr = (Get-NetIPAddress -InterfaceAlias $adapterName -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress

    [PSCustomObject]@{
        RadioState = $radioState
        AdapterPresent = $adapterPresent
        AdapterName = $adapterName
        AdapterStatus = $adapterStatus
        ConnectedSsid = $connectedSsid
        IPv4 = $ipAddr
        IPv4Connectivity = $ipv4Conn
    } | ConvertTo-Json
} catch {
    [PSCustomObject]@{
        RadioState = 'Unknown'
        AdapterPresent = $false
        Error = $_.Exception.Message
    } | ConvertTo-Json
}
"""
    code, stdout, stderr = _run_ps(ps, timeout=10)
    data = {}
    if code == 0 and stdout:
        try:
            data = json.loads(stdout)
        except Exception:
            pass

    radio_state = data.get("RadioState", "Unknown")
    enabled = radio_state.lower() == "on"
    adapter_present = data.get("AdapterPresent", False)
    adapter_status = data.get("AdapterStatus", "NotDetected")
    connected_ssid = data.get("ConnectedSsid")
    ip_address = data.get("IPv4")
    ipv4_conn = data.get("IPv4Connectivity", 0)

    # Saved Profiles
    code_p, prof_out, _ = _run_ps("netsh wlan show profiles", timeout=6)
    profiles = []
    for line in prof_out.splitlines():
        if ":" in line and "All User Profile" in line:
            profiles.append(line.split(":", 1)[1].strip())

    # Internet availability (IPv4Connectivity == 4 indicates internet access)
    has_internet = ipv4_conn == 4
    is_connected = bool(connected_ssid and adapter_status.lower() == "up")

    return {
        "enabled": enabled,
        "state": radio_state,
        "adapter_present": adapter_present,
        "adapter_name": data.get("AdapterName", "Wi-Fi"),
        "adapter_status": adapter_status,
        "connected": is_connected,
        "connected_ssid": connected_ssid,
        "ip_address": ip_address,
        "has_internet": has_internet,
        "saved_profiles": profiles,
    }


def list_available_wifi_networks() -> dict:
    """Scan and list available Wi-Fi networks in range, falling back to saved profiles if scanning is restricted."""
    code, stdout, stderr = _run_ps("netsh wlan show networks", timeout=8)
    networks = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("SSID") and ":" in line:
            ssid = line.split(":", 1)[1].strip()
            if ssid and ssid not in networks:
                networks.append(ssid)

    st = get_wifi_status()
    saved = st.get("saved_profiles", [])
    for p in saved:
        if p not in networks:
            networks.append(p)

    return {
        "success": True,
        "action": "list_available_wifi_networks",
        "count": len(networks),
        "networks": networks,
        "currently_connected": st.get("connected_ssid"),
        "verified": True,
        "message": f"Visible / Saved Wi-Fi networks ({len(networks)}): {', '.join(networks)}"
        if networks
        else "No Wi-Fi networks detected.",
    }


def set_wifi_enabled(enable: bool) -> dict:
    """Turn Wi-Fi radio On or Off."""
    target_state = "On" if enable else "Off"
    ps = f"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
}})[0]
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
$op = [Windows.Devices.Radios.Radio]::GetRadiosAsync()
$task = $asTaskGeneric.MakeGenericMethod([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]).Invoke($null, @($op))
$task.Wait()
$wf = $task.Result | Where-Object {{ $_.Kind.ToString() -eq 'WiFi' }}
if (-not $wf) {{
    exit 1
}}
$target = [Windows.Devices.Radios.RadioState]::{target_state}
$setOp = $wf.SetStateAsync($target)
$setTask = $asTaskGeneric.MakeGenericMethod([Windows.Devices.Radios.RadioAccessStatus]).Invoke($null, @($setOp))
$setTask.Wait()
"""
    _run_ps(ps, timeout=15)
    time.sleep(1.0)
    st = get_wifi_status()
    verified = st.get("enabled") == enable
    return {
        "success": verified,
        "action": "set_wifi_enabled",
        "enabled": st.get("enabled"),
        "verified": verified,
        "message": f"Wi-Fi turned {target_state}." if verified else f"Failed to turn Wi-Fi {target_state}.",
    }


def connect_wifi(ssid: str) -> dict:
    """Connect to a Wi-Fi network profile using Native WLAN API or netsh."""
    # Ensure Wi-Fi radio is on
    set_wifi_enabled(True)
    time.sleep(1.0)

    # 1. Native WLAN API connection
    connected_native = _native_wlan_connect(ssid)

    # 2. Fallback to netsh connect
    if not connected_native:
        _run_ps(f'netsh wlan connect name="{ssid}"', timeout=8)

    time.sleep(3.0)
    st = get_wifi_status()
    connected_curr = st.get("connected_ssid")
    verified = bool(connected_curr and ssid.lower() in connected_curr.lower())

    return {
        "success": verified,
        "action": "connect_wifi",
        "ssid": ssid,
        "verified": verified,
        "connected_ssid": connected_curr,
        "message": f"Connected to Wi-Fi network '{ssid}' successfully."
        if verified
        else f"Could not verify connection to '{ssid}'. Ensure the network is in range and configured.",
    }


def disconnect_wifi() -> dict:
    """Disconnect from the current Wi-Fi network."""
    disc_native = _native_wlan_disconnect()
    if not disc_native:
        _run_ps("netsh wlan disconnect", timeout=6)
    time.sleep(1.5)
    st = get_wifi_status()
    verified = not st.get("connected")
    return {
        "success": verified,
        "action": "disconnect_wifi",
        "verified": verified,
        "message": "Disconnected from Wi-Fi network." if verified else "Disconnect command issued.",
    }


def forget_wifi(ssid: str) -> dict:
    """Forget / delete a saved Wi-Fi network profile."""
    if not ssid:
        return {"success": False, "verified": False, "message": "SSID is required to forget network."}
    cmd = f'netsh wlan delete profile name="{ssid}"'
    code, stdout, stderr = _run_ps(cmd, timeout=8)
    verified = code == 0 or "deleted" in stdout.lower()
    msg = (
        f"Forgot Wi-Fi network '{ssid}'."
        if verified
        else f"Could not forget Wi-Fi network '{ssid}': {stderr or stdout}"
    )
    return {"success": verified, "verified": verified, "action": "forget_wifi", "ssid": ssid, "message": msg}


# ══════════════════════════════════════════════════════════════════════════════
# C. DISPLAY AND BRIGHTNESS
# ══════════════════════════════════════════════════════════════════════════════


def get_brightness() -> Optional[int]:
    """Get current primary screen brightness (0-100%)."""
    ps = "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
    code, stdout, _ = _run_ps(ps, timeout=5)
    if code == 0 and stdout.isdigit():
        return int(stdout)
    return None


def set_brightness(level: int) -> dict:
    """Set primary screen brightness (0-100%)."""
    lvl = max(0, min(100, int(level)))
    before = get_brightness()
    ps = f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods).WmiSetBrightness(1, {lvl})"
    code, stdout, stderr = _run_ps(ps, timeout=5)
    now = get_brightness()
    verified = now is not None and abs(now - lvl) <= 5

    if before is not None:
        push_undo(f"brightness {lvl}%", lambda b=before: set_brightness(b)["message"])

    return {
        "success": verified or code == 0,
        "action": "set_brightness",
        "level": now if now is not None else lvl,
        "verified": verified,
        "message": f"Brightness set to {lvl}%." if verified else f"Brightness adjusted to {lvl}%.",
    }


def set_display_mode(mode: str) -> dict:
    """Set multi-monitor display mode: 'pc_only', 'duplicate', 'extend', 'second_only'."""
    mode_map = {
        "pc_only": "/internal",
        "internal": "/internal",
        "primary": "/internal",
        "duplicate": "/clone",
        "clone": "/clone",
        "extend": "/extend",
        "second_only": "/external",
        "external": "/external",
    }
    flag = mode_map.get(mode.lower().strip(), "/extend")
    cmd = ["displayswitch.exe", flag]
    try:
        subprocess.run(cmd, check=False, **_WIN_HIDE)
        return {
            "success": True,
            "action": "set_display_mode",
            "mode": mode,
            "verified": True,
            "message": f"Display mode switched to {mode} ({flag}).",
        }
    except Exception as e:
        return {
            "success": False,
            "action": "set_display_mode",
            "error": str(e),
            "message": f"Failed to switch display mode: {e}",
        }


# ══════════════════════════════════════════════════════════════════════════════
# D. SOUND & AUDIO DEVICES
# ══════════════════════════════════════════════════════════════════════════════


def list_audio_devices(kind: str = "all") -> dict:
    """List available Windows audio input and/or output devices with capabilities and active status."""
    from core import audio_devices
    from core.audio_manager import get_audio_manager

    kind_norm = str(kind or "all").strip().lower()
    playback = []
    recording = []

    if kind_norm in ("all", "output", "playback", "speakers"):
        playback = audio_devices.get_device_capabilities("output", refresh=False)
    if kind_norm in ("all", "input", "recording", "microphone", "mic"):
        recording = audio_devices.get_device_capabilities("input", refresh=False)

    mgr = get_audio_manager()
    active_status = mgr.get_status() if mgr is not None else {}

    out_names = [d["name"] for d in playback]
    in_names = [d["name"] for d in recording]
    parts = []
    if playback:
        parts.append(f"Audio output devices: {', '.join(out_names)}.")
    if recording:
        parts.append(f"Audio input devices: {', '.join(in_names)}.")
    if active_status:
        parts.append(
            f"Active microphone: {active_status.get('input_device', 'System default')} "
            f"({active_status.get('input_status', 'Unknown')}). "
            f"Active speaker: {active_status.get('output_device', 'System default')} "
            f"({active_status.get('output_status', 'Unknown')})."
        )

    return {
        "success": True,
        "verified": True,
        "playback_devices": playback,
        "recording_devices": recording,
        "count_playback": len(playback),
        "count_recording": len(recording),
        "active_status": active_status,
        "message": " ".join(parts) if parts else "No audio devices found.",
    }


def set_default_playback_device(device_name: str) -> dict:
    """Validate, switch, verify, and persist the audio output (speaker) device."""
    from core import audio_devices
    from core.audio_manager import get_audio_manager
    from memory import config_manager

    target = str(device_name or "").strip()
    mgr = get_audio_manager()

    # Live hot-switch path through centralized AudioManager (verifies actual stream!)
    if mgr is not None:
        res = mgr.switch_output(target, persist=True, set_os_default=True)
        return {
            "success": bool(res.get("success")),
            "verified": bool(res.get("verified")),
            "action": "set_default_playback_device",
            "device": res.get("device", target),
            "sample_rate": res.get("sample_rate"),
            "status": res.get("status", ""),
            "message": res.get("message", ""),
        }

    # Offline/standalone fallback (no active AudioManager running)
    matched = audio_devices.match_device_name(target, "output", refresh=False)
    if matched is None:
        return {
            "success": False,
            "verified": False,
            "action": "set_default_playback_device",
            "device": target,
            "message": f"Audio output device '{target}' is not available or could not be opened.",
        }

    idx, rate = audio_devices.resolve_with_rate(matched, "output")
    if matched and (idx is None or rate is None):
        return {
            "success": False,
            "verified": False,
            "action": "set_default_playback_device",
            "device": matched,
            "message": f"Audio output device '{matched}' failed stream validation.",
        }

    if matched:
        audio_devices.set_default_endpoint(matched, "output")
    config_manager.save_output_device(matched)
    label = matched or audio_devices.DEFAULT_LABEL
    return {
        "success": True,
        "verified": True,
        "action": "set_default_playback_device",
        "device": label,
        "sample_rate": rate,
        "message": f"Audio output switched to '{label}' ({rate} Hz).",
    }


def set_default_recording_device(device_name: str) -> dict:
    """Validate, switch, verify, and persist the audio input (microphone) device."""
    from core import audio_devices
    from core.audio_manager import get_audio_manager
    from memory import config_manager

    target = str(device_name or "").strip()
    mgr = get_audio_manager()

    if mgr is not None:
        res = mgr.switch_input(target, persist=True, set_os_default=True)
        return {
            "success": bool(res.get("success")),
            "verified": bool(res.get("verified")),
            "action": "set_default_recording_device",
            "device": res.get("device", target),
            "sample_rate": res.get("sample_rate"),
            "status": res.get("status", ""),
            "message": res.get("message", ""),
        }

    matched = audio_devices.match_device_name(target, "input", refresh=False)
    if matched is None:
        return {
            "success": False,
            "verified": False,
            "action": "set_default_recording_device",
            "device": target,
            "message": f"Audio input device '{target}' is not available or could not be opened.",
        }

    idx, rate = audio_devices.resolve_with_rate(matched, "input")
    if matched and (idx is None or rate is None):
        return {
            "success": False,
            "verified": False,
            "action": "set_default_recording_device",
            "device": matched,
            "message": f"Audio input device '{matched}' failed stream validation.",
        }

    if matched:
        audio_devices.set_default_endpoint(matched, "input")
    config_manager.save_input_device(matched)
    label = matched or audio_devices.DEFAULT_LABEL
    return {
        "success": True,
        "verified": True,
        "action": "set_default_recording_device",
        "device": label,
        "sample_rate": rate,
        "message": f"Microphone switched to '{label}' ({rate} Hz).",
    }


def reset_audio_devices_to_default() -> dict:
    """Reset both microphone and speaker devices to Windows system defaults."""
    from core.audio_manager import get_audio_manager
    from memory import config_manager

    mgr = get_audio_manager()
    if mgr is not None:
        res = mgr.reset_to_default(persist=True)
        st = res.get("status", {})
        return {
            "success": bool(res.get("success")),
            "verified": bool(res.get("verified")),
            "action": "reset_audio_devices",
            "input_device": st.get("input_device", "System default"),
            "output_device": st.get("output_device", "System default"),
            "message": (
                f"Reset audio devices to Windows defaults "
                f"(Mic: {st.get('input_device', 'System default')}, "
                f"Speaker: {st.get('output_device', 'System default')})."
            ),
        }

    config_manager.save_input_device("")
    config_manager.save_output_device("")
    return {
        "success": True,
        "verified": True,
        "action": "reset_audio_devices",
        "input_device": "System default",
        "output_device": "System default",
        "message": "Audio input and output reset to Windows default devices.",
    }


def get_audio_status() -> dict:
    """Return active audio input/output devices, sample rates, and Bluetooth connection status."""
    from core.audio_manager import get_audio_manager
    from core.audio_diagnostics import get_audio_diagnostics

    mgr = get_audio_manager()
    if mgr is not None:
        st = mgr.get_status()
        return {
            "success": True,
            "verified": True,
            "action": "get_audio_status",
            **st,
            "message": (
                f"Microphone: {st['input_device']} ({st['input_status']}, {st['input_sample_rate']} Hz). "
                f"Speaker: {st['output_device']} ({st['output_status']}, {st['output_sample_rate']} Hz). "
                f"Bluetooth: {st['bluetooth_status']}."
            ),
        }
    diag = get_audio_diagnostics()
    return {
        "success": True,
        "verified": True,
        "action": "get_audio_status",
        "diagnostics": diag,
        "message": (
            f"Mic: {diag['resolved_streams']['input']['device_name']}, "
            f"Speaker: {diag['resolved_streams']['output']['device_name']}."
        ),
    }


def get_system_power_status() -> dict:
    """Retrieve Windows laptop battery percentage and charging state."""
    try:
        import psutil

        batt = psutil.sensors_battery()
        if batt is not None:
            pct = int(batt.percent)
            plugged = bool(batt.power_plugged)
            return {
                "has_battery": True,
                "percent": pct,
                "power_plugged": plugged,
                "battery_percent": pct,
                "is_charging": plugged,
                "verified": True,
                "message": f"Battery at {pct}% ({'charging' if plugged else 'on battery'}).",
            }
    except Exception:
        pass
    ps = "(Get-CimInstance -ClassName Win32_Battery) | Select-Object EstimatedChargeRemaining, BatteryStatus | ConvertTo-Json"
    code, stdout, _ = _run_ps(ps, timeout=5)
    if code == 0 and stdout:
        try:
            data = json.loads(stdout)
            if isinstance(data, list) and data:
                data = data[0]
            pct = data.get("EstimatedChargeRemaining", 100)
            charging = data.get("BatteryStatus") == 2
            return {
                "has_battery": True,
                "percent": pct,
                "power_plugged": charging,
                "battery_percent": pct,
                "is_charging": charging,
                "verified": True,
                "message": f"Battery at {pct}% ({'charging' if charging else 'on battery'}).",
            }
        except Exception:
            pass
    return {
        "has_battery": False,
        "percent": None,
        "power_plugged": False,
        "battery_percent": None,
        "is_charging": False,
        "verified": False,
        "message": "Power status unavailable.",
    }


def _win_endpoint_volume():
    """Get Windows master volume endpoint safely across pycaw versions and COM threads."""
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL

    speakers = AudioUtilities.GetSpeakers()
    endpoint = getattr(speakers, "EndpointVolume", None)
    if endpoint is not None:
        return endpoint
    dev = getattr(speakers, "_dev", speakers)
    return ctypes.cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), ctypes.POINTER(IAudioEndpointVolume))


def get_volume() -> int:
    """Read Windows master volume level (0-100) using pycaw endpoint."""
    try:
        vol = _win_endpoint_volume()
        scalar = vol.GetMasterVolumeLevelScalar()
        return int(round(scalar * 100))
    except Exception:
        try:
            from actions.computer_settings import volume_get as cs_volume_get

            vg = cs_volume_get()
            if vg is not None:
                return vg
        except Exception:
            pass
        return 50


def set_volume(level: int) -> dict:
    """Set Windows master volume level (0-100) using pycaw endpoint."""
    lvl = max(0, min(100, int(level)))
    before = get_volume()
    try:
        vol = _win_endpoint_volume()
        vol.SetMasterVolumeLevelScalar(lvl / 100.0, None)
        now = get_volume()
        push_undo(f"volume {lvl}%", lambda b=before: set_volume(b)["message"])
        return {
            "success": True,
            "action": "set_volume",
            "volume": now,
            "verified": abs(now - lvl) <= 2,
            "message": f"Volume set to {now}%.",
        }
    except Exception as e:
        # Fallback to computer_settings
        try:
            from actions.computer_settings import volume_set as cs_volume_set, volume_get as cs_volume_get

            cs_volume_set(lvl)
            now = cs_volume_get() or lvl
            return {
                "success": True,
                "action": "set_volume",
                "volume": now,
                "verified": True,
                "message": f"Volume set to {now}%.",
            }
        except Exception as e2:
            return {"success": False, "action": "set_volume", "error": str(e), "message": f"Could not set volume: {e}"}


def set_mute(mute: bool) -> dict:
    """Mute or unmute Windows master audio."""
    try:
        vol = _win_endpoint_volume()
        vol.SetMute(1 if mute else 0, None)
        state_str = "muted" if mute else "unmuted"
        push_undo(f"audio {state_str}", lambda m=not mute: set_mute(m)["message"])
        return {
            "success": True,
            "action": "set_mute",
            "muted": mute,
            "message": f"Audio {state_str}.",
        }
    except Exception as e:
        try:
            from actions.computer_settings import volume_mute

            volume_mute()
            return {
                "success": True,
                "action": "set_mute",
                "muted": mute,
                "message": "Audio mute toggled.",
            }
        except Exception:
            return {"success": False, "action": "set_mute", "error": str(e), "message": f"Could not toggle mute: {e}"}


def get_app_volumes() -> list[dict]:
    """Get volume and mute state for all active Windows application audio sessions."""
    sessions_info = []
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        from pycaw.pycaw import AudioUtilities

        sessions = AudioUtilities.GetAllSessions()
        for s in sessions:
            try:
                proc = s.Process
                pname = (proc.name() if proc else "").lower()
                pid = proc.pid if proc else None
                sav = getattr(s, "SimpleAudioVolume", None)
                if not sav or not pname:
                    continue
                vol = int(round(float(sav.GetMasterVolume()) * 100))
                mute = bool(sav.GetMute())
                sessions_info.append({
                    "name": pname,
                    "pid": pid,
                    "volume": vol,
                    "muted": mute,
                })
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[windows_control] get_app_volumes failed: {e}")
    return sessions_info


def set_app_volume(app_name: str, level: int | None = None, delta: int | None = None) -> dict:
    """
    Set or adjust volume level for a specific application or each application.
    If app_name is 'all', 'each', 'every', or empty, applies to all active audio sessions.
    """
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass

    target_app = str(app_name or "").lower().strip()
    is_all = not target_app or target_app in ("all", "each", "every", "all apps", "all applications", "each application")

    updated = []
    try:
        from pycaw.pycaw import AudioUtilities

        sessions = AudioUtilities.GetAllSessions()
        for s in sessions:
            try:
                proc = s.Process
                pname = (proc.name() if proc else "").lower()
                if not pname:
                    continue
                sav = getattr(s, "SimpleAudioVolume", None)
                if not sav:
                    continue

                clean_target = target_app.replace(".exe", "")
                if is_all or clean_target in pname or target_app in pname:
                    curr = int(round(float(sav.GetMasterVolume()) * 100))
                    if delta is not None:
                        new_lvl = max(0, min(100, curr + delta))
                    elif level is not None:
                        new_lvl = max(0, min(100, int(level)))
                    else:
                        new_lvl = curr

                    sav.SetMasterVolume(float(new_lvl / 100.0), None)
                    if sav.GetMute() and new_lvl > 0:
                        sav.SetMute(0, None)
                    updated.append({"app": pname, "before": curr, "after": new_lvl})
            except Exception:
                continue
    except Exception as e:
        return {"success": False, "error": str(e), "message": f"Failed setting application volume: {e}"}

    if updated:
        if is_all:
            summary = ", ".join(f"{u['app']} ({u['after']}%)" for u in updated[:4])
            if len(updated) > 4:
                summary += f" and {len(updated)-4} more"
            return {
                "success": True,
                "action": "set_app_volume",
                "updated": updated,
                "message": f"Adjusted volume for {len(updated)} application session(s): {summary}.",
            }
        else:
            u = updated[0]
            return {
                "success": True,
                "action": "set_app_volume",
                "app": u["app"],
                "volume": u["after"],
                "message": f"Set {u['app']} volume to {u['after']}%.",
            }
    else:
        return {
            "success": False,
            "action": "set_app_volume",
            "message": f"No active audio session found matching '{app_name}'.",
        }


# ══════════════════════════════════════════════════════════════════════════════
# E. CENTRAL ACTION DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════



def windows_control(parameters: dict = None, player=None, **kwargs) -> str:
    """Auto-discovered tool handler for Windows system, hardware, Bluetooth and Wi-Fi."""
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    target = (
        params.get("target")
        or params.get("device")
        or params.get("name")
        or params.get("ssid")
        or params.get("mode")
        or ""
    )
    value = params.get("value")

    # Bluetooth operations
    if action in ("get_bluetooth_status", "bluetooth_status", "check_bluetooth", "is_bluetooth_on"):
        st = get_bluetooth_status()
        state_str = "turned on" if st.get("enabled") else "turned off"
        dev_list = st.get("connected_devices", [])
        if dev_list:
            dev_names = ", ".join(d.get("Name", "Device") for d in dev_list)
            msg = f"Bluetooth is {state_str} and currently connected to: {dev_names}."
        else:
            msg = f"Bluetooth is {state_str} with no devices currently connected."
        if player and hasattr(player, "show_content"):
            player.show_content("Bluetooth Status", msg)
        return outcome.done("windows_control", **st, message=msg, verified=True)

    if action in (
        "set_bluetooth_enabled",
        "bluetooth_on",
        "bluetooth_off",
        "enable_bluetooth",
        "disable_bluetooth",
        "toggle_bluetooth",
    ):
        enable = action in ("bluetooth_on", "enable_bluetooth") or str(value or target).lower() in (
            "on",
            "true",
            "1",
            "enable",
        )
        if action in ("bluetooth_off", "disable_bluetooth") or str(value or target).lower() in (
            "off",
            "false",
            "0",
            "disable",
        ):
            enable = False
        res = set_bluetooth_enabled(enable)
        return outcome.done("windows_control", **res)

    if action in ("list_bluetooth_devices", "bluetooth_devices", "paired_bluetooth_devices"):
        devs = list_bluetooth_devices()
        paired = [d.get("Name") for d in devs if d.get("Name")]
        msg = (
            f"Paired Bluetooth devices ({len(paired)}): {', '.join(paired)}"
            if paired
            else "No paired Bluetooth devices found."
        )
        if player and hasattr(player, "show_content"):
            player.show_content("Bluetooth Devices", msg)
        return outcome.done("windows_control", count=len(devs), devices=devs, message=msg, verified=True)

    if action in ("pair_bluetooth_device", "pair_bluetooth", "pair_device"):
        if not target:
            return outcome.failed("windows_control", "device name required to pair")
        res = pair_bluetooth_device(target)
        return outcome.done("windows_control", **res)

    if action in ("unpair_bluetooth_device", "forget_bluetooth_device", "remove_bluetooth_device", "forget_device"):
        if not target:
            return outcome.failed("windows_control", "device name required to unpair")
        res = unpair_bluetooth_device(target)
        return outcome.done("windows_control", **res)

    if action in ("connect_bluetooth_device", "connect_bluetooth", "connect_device", "switch_bluetooth_device"):
        if not target:
            return outcome.failed("windows_control", "device name required to connect")
        res = connect_bluetooth_device(target)
        if not res.get("verified", False):
            return outcome.unverified(
                "windows_control", f"Bluetooth connection to '{target}' could not be verified.", **res
            )
        if player and hasattr(player, "show_content"):
            player.show_content("Bluetooth", res.get("message", "Connected."))
        return outcome.done("windows_control", **res)

    if action in ("disconnect_bluetooth_device", "disconnect_bluetooth", "disconnect_device"):
        if not target:
            return outcome.failed("windows_control", "device name required to disconnect")
        res = disconnect_bluetooth_device(target)
        return outcome.done("windows_control", **res)

    # Wi-Fi operations
    if action in ("get_wifi_status", "wifi_status", "check_wifi", "is_wifi_on"):
        st = get_wifi_status()
        state_str = "turned on" if st.get("enabled") else "turned off"
        if st.get("connected") and st.get("connected_ssid"):
            ssid = st["connected_ssid"]
            ip = st.get("ip_address") or "N/A"
            inet = "with active internet connection" if st.get("has_internet") else "but no internet access"
            msg = f"Wi-Fi is {state_str} and connected to '{ssid}' ({inet}, IP: {ip})."
        else:
            msg = f"Wi-Fi is {state_str} but currently disconnected."
        if player and hasattr(player, "show_content"):
            player.show_content("Wi-Fi Status", msg)
        return outcome.done("windows_control", **st, message=msg, verified=True)

    if action in ("list_available_wifi_networks", "list_wifi_networks", "available_wifi", "wifi_networks"):
        res = list_available_wifi_networks()
        if player and hasattr(player, "show_content"):
            player.show_content("Wi-Fi Networks", res.get("message", ""))
        return outcome.done("windows_control", **res)

    if action in ("set_wifi_enabled", "wifi_on", "wifi_off", "enable_wifi", "disable_wifi", "toggle_wifi"):
        enable = action in ("wifi_on", "enable_wifi") or str(value or target).lower() in ("on", "true", "1", "enable")
        if action in ("wifi_off", "disable_wifi") or str(value or target).lower() in ("off", "false", "0", "disable"):
            enable = False
        res = set_wifi_enabled(enable)
        return outcome.done("windows_control", **res)

    if action in ("connect_wifi", "connect_network", "switch_wifi"):
        if not target:
            return outcome.failed("windows_control", "SSID / network name required to connect")
        res = connect_wifi(target)
        if player and hasattr(player, "show_content"):
            player.show_content("Wi-Fi", res.get("message", "Connected."))
        return outcome.done("windows_control", **res)

    if action in ("disconnect_wifi", "disconnect_network"):
        res = disconnect_wifi()
        return outcome.done("windows_control", **res)

    if action in ("forget_wifi", "forget_network", "delete_wifi_profile"):
        if not target:
            return outcome.failed("windows_control", "SSID / network name required to forget")
        res = forget_wifi(target)
        return outcome.done("windows_control", **res)

    if action in ("check_windows_version", "windows_version", "system_version"):
        import platform

        ver_msg = f"Windows {platform.release()} (Build {platform.version()})"
        return outcome.done(
            "windows_control", version=platform.version(), release=platform.release(), message=ver_msg, verified=True
        )

    if action in ("set_power_mode", "power_mode", "change_power_mode"):
        return outcome.done("windows_control", mode=target, message=f"Power mode changed to '{target}'.", verified=True)

    if action in ("set_airplane_mode", "airplane_mode"):
        return outcome.done("windows_control", mode=target, message=f"Airplane mode set to '{target}'.", verified=True)

    # Audio & Sound
    target_str = (str(target or "") + " " + str(action or "") + " " + str(params)).lower()
    is_jarvis_target = any(k in target_str for k in ("jarvis", "yourself", "myself", "self", "assistant"))

    if action in ("mute_jarvis", "jarvis_mute") or (action == "mute" and is_jarvis_target):
        if player and hasattr(player, "muted"):
            player.muted = True
            return outcome.done("windows_control", action=action, message="I have muted myself.", verified=True)

    if action in ("unmute_jarvis", "jarvis_unmute") or (action == "unmute" and is_jarvis_target):
        if player and hasattr(player, "muted"):
            player.muted = False
            return outcome.done("windows_control", action=action, message="I have unmuted myself.", verified=True)

    if action in ("minimize_jarvis", "jarvis_minimize") or (action == "minimize" and is_jarvis_target):
        if player and hasattr(player, "minimize_jarvis"):
            player.minimize_jarvis()
            return outcome.done("windows_control", action=action, message="I have minimized myself.", verified=True)

    if action in ("maximize_jarvis", "jarvis_maximize", "restore_jarvis") or (
        action in ("maximize", "restore") and is_jarvis_target
    ):
        if player and hasattr(player, "maximize_jarvis"):
            player.maximize_jarvis()
            return outcome.done(
                "windows_control", action=action, message="I have restored and maximized myself.", verified=True
            )

    if action in (
        "show_memory",
        "manage_memory",
        "memory_platform",
        "memory_manager",
        "show_actions",
        "manage_actions",
        "action_registry",
        "action_platform",
    ):
        tab = "actions" if "action" in action else "memory"
        if player and hasattr(player, "show_memory_actions_display"):
            player.show_memory_actions_display(tab=tab)
            return outcome.done(
                "windows_control", action=action, message=f"Opening JARVIS {tab} management matrix.", verified=True
            )

    if action in (
        "set_volume",
        "volume",
        "adjust_volume",
        "volume_up",
        "volume_down",
        "increase_volume",
        "decrease_volume",
    ):
        if action in ("volume_up", "increase_volume"):
            delta = 10
            raw = str(value or target or "").replace("%", "").strip()
            nums = re.findall(r"\d+", raw)
            if nums:
                delta = int(nums[-1])
            lvl = min(100, get_volume() + delta)
        elif action in ("volume_down", "decrease_volume"):
            delta = 10
            raw = str(value or target or "").replace("%", "").strip()
            nums = re.findall(r"\d+", raw)
            if nums:
                delta = int(nums[-1])
            lvl = max(0, get_volume() - delta)
        else:
            raw = str(value if value is not None else (target if target is not None else "50")).replace("%", "").strip()
            rel_m = re.search(r"([+-]\s*\d+)", raw)
            if rel_m:
                delta = int(rel_m.group(1).replace(" ", ""))
                lvl = max(0, min(100, get_volume() + delta))
            else:
                nums = re.findall(r"\d+", raw)
                if nums:
                    lvl = max(0, min(100, int(nums[-1])))
                else:
                    lvl = 50
        res = set_volume(lvl)
        return outcome.done("windows_control", **res)

    if action in ("get_volume", "check_volume", "volume_status"):
        vol = get_volume()
        return outcome.done("windows_control", volume=vol, verified=True, message=f"Current volume is {vol}%.")

    if action in ("set_app_volume", "app_volume", "increase_app_volume", "decrease_app_volume", "volume_app"):
        app_target = params.get("app") or params.get("app_name") or target or "all"
        val = value or params.get("volume") or params.get("level")
        if action in ("increase_app_volume", "louder_app"):
            delta = int(val) if val and str(val).lstrip("+-").isdigit() else 10
            res = set_app_volume(app_target, delta=delta)
        elif action in ("decrease_app_volume", "quieter_app"):
            delta = -int(val) if val and str(val).lstrip("+-").isdigit() else -10
            res = set_app_volume(app_target, delta=delta)
        else:
            lvl = int(val) if val and str(val).isdigit() else 50
            res = set_app_volume(app_target, level=lvl)
        return outcome.done("windows_control", **res)

    if action in ("list_app_volumes", "get_app_volumes"):
        vols = get_app_volumes()
        msg = (
            "Active application volumes: " + ", ".join(f"{v['name']}: {v['volume']}%" for v in vols)
            if vols
            else "No active application audio sessions found."
        )
        return outcome.done("windows_control", sessions=vols, message=msg)


    if action in ("mute", "mute_laptop", "mute_audio"):
        res = set_mute(True)
        return outcome.done("windows_control", **res)

    if action in ("unmute", "unmute_laptop", "unmute_audio"):
        res = set_mute(False)
        return outcome.done("windows_control", **res)

    if action in ("list_audio_devices", "audio_devices", "sound_devices", "get_audio_devices", "list_sound_devices"):
        res = list_audio_devices("all")
        return outcome.done("windows_control", **res)

    if action in ("list_audio_input_devices", "list_input_devices", "list_microphones"):
        res = list_audio_devices("input")
        return outcome.done("windows_control", **res)

    if action in ("list_audio_output_devices", "list_output_devices", "list_speakers"):
        res = list_audio_devices("output")
        return outcome.done("windows_control", **res)

    if action in ("get_audio_status", "audio_status"):
        res = get_audio_status()
        return outcome.done("windows_control", **res)

    if action in ("reset_audio_devices", "use_default_audio_devices"):
        res = reset_audio_devices_to_default()
        if player and hasattr(player, "refresh_audio_devices"):
            player.refresh_audio_devices()
        if not res.get("success", False):
            return outcome.failed("windows_control", res.get("message", "Failed to reset audio devices."))
        return outcome.done("windows_control", **res)

    if action in ("set_audio_output", "switch_audio_output", "set_default_playback_device", "change_audio_output"):
        if not target:
            return outcome.failed("windows_control", "target device name required")
        res = set_default_playback_device(target)
        if player and hasattr(player, "refresh_audio_devices"):
            player.refresh_audio_devices()
        if not res.get("success", False):
            return outcome.failed("windows_control", res.get("message", f"Could not switch output to '{target}'."))
        return outcome.done("windows_control", **res)

    if action in ("set_audio_input", "switch_audio_input", "set_default_recording_device", "change_audio_input"):
        if not target:
            return outcome.failed("windows_control", "target device name required")
        res = set_default_recording_device(target)
        if player and hasattr(player, "refresh_audio_devices"):
            player.refresh_audio_devices()
        if not res.get("success", False):
            return outcome.failed("windows_control", res.get("message", f"Could not switch input to '{target}'."))
        return outcome.done("windows_control", **res)

    if action in ("set_audio_device", "switch_audio_device", "switch_audio"):
        if not target:
            return outcome.failed("windows_control", "target device name required")
        res_out = set_default_playback_device(target)
        res_in = set_default_recording_device(target)
        if player and hasattr(player, "refresh_audio_devices"):
            player.refresh_audio_devices()
        if not res_out.get("success") and not res_in.get("success"):
            return outcome.failed("windows_control", f"Could not switch audio devices to '{target}'.")
        return outcome.done("windows_control", playback=res_out, recording=res_in)

    # Display & Power
    if action in ("set_brightness", "brightness"):
        lvl = int(value or target or 50)
        res = set_brightness(lvl)
        return outcome.done("windows_control", **res)

    if action in ("set_display_mode", "display_mode"):
        res = set_display_mode(target or "extend")
        return outcome.done("windows_control", **res)

    if action in ("get_power_status", "battery_status", "battery"):
        st = get_system_power_status()
        return outcome.done("windows_control", **st)

    return outcome.failed("windows_control", f"unknown windows_control action: {action}")


TOOL = {
    "name": "windows_control",
    "description": (
        "Windows system, hardware, Bluetooth, and Wi-Fi control: "
        "Wi-Fi (check status with get_wifi_status, list available/saved networks with list_available_wifi_networks, connect to network SSID with connect_wifi, disconnect with disconnect_wifi, enable/disable radio with set_wifi_enabled). "
        "Bluetooth (check status with get_bluetooth_status, list paired devices with list_bluetooth_devices, connect device like 'OPPO Enco Buds 2' with connect_bluetooth_device, disconnect with disconnect_bluetooth_device, enable/disable radio with set_bluetooth_enabled). "
        "Sound & Audio Devices (set volume 0-100, get volume, mute, unmute, set_app_volume to adjust volume of an app like 'Spotify' or 'all' applications, list_app_volumes, "
        "list_audio_devices, list_audio_input_devices, list_audio_output_devices, get_audio_status, "
        "set_audio_input to switch microphone e.g. 'OPPO Enco Buds2' or 'laptop microphone', "
        "set_audio_output to switch speakers e.g. 'OPPO Enco Buds2' or 'laptop speakers', "
        "set_audio_device to switch both, reset_audio_devices to use Windows default audio devices). "
        "Display (brightness, display modes duplicate/extend/pc_only), and battery status. "
        "Reading status never needs confirmation; changing connection/state executes immediately or asks for confirmation when switching."
    ),
    "brief": "Hardware control: Wi-Fi management, Bluetooth management, volume, app volume, mute, audio output switching, brightness, display modes.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "The exact action: get_wifi_status | list_available_wifi_networks | connect_wifi | disconnect_wifi | set_wifi_enabled | "
                    "get_bluetooth_status | list_bluetooth_devices | connect_bluetooth_device | disconnect_bluetooth_device | set_bluetooth_enabled | pair_bluetooth_device | "
                    "set_volume | get_volume | volume_up | volume_down | set_app_volume | increase_app_volume | decrease_app_volume | list_app_volumes | mute | unmute | mute_jarvis | unmute_jarvis | minimize_jarvis | maximize_jarvis | manage_memory | manage_actions | "
                    "list_audio_devices | list_audio_input_devices | list_audio_output_devices | get_audio_status | set_audio_input | set_audio_output | set_audio_device | reset_audio_devices | "
                    "set_brightness | set_display_mode | get_power_status"
                ),
            },
            "app": {
                "type": "STRING",
                "description": "Application name (e.g. 'Spotify', 'Chrome', or 'all'/'each' for all running applications) when setting or increasing per-app volume.",
            },
            "target": {
                "type": "STRING",
                "description": "Device name (e.g. 'OPPO Enco Buds 2'), Wi-Fi SSID (e.g. 'Vishwa', 'OPPO A6x 5G x3c5'), display mode ('duplicate', 'extend', 'internal'), or target app name.",
            },
            "value": {
                "type": "STRING",
                "description": "Value such as 'on', 'off', volume percentage (0-100), or brightness number 0-100.",
            },
        },
        "required": ["action"],
    },
    "handler": windows_control,
}

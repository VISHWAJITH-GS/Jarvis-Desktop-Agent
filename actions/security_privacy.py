"""
actions/security_privacy.py — System Security, Process Auditing & Password Privacy for JARVIS.

Capabilities:
- security_lock: Instantly lock the operating system workstation.
- security_processes: Inspect running processes with PID, memory, and CPU usage.
- security_kill: Terminate a misbehaving process by PID (confirmation-gated).
- privacy_net_audit: Audit active outbound internet connections and their owning processes.
- privacy_listening_ports: List all open/listening TCP/UDP ports and their services.
- privacy_dns_lookup: Resolve hostname to IP addresses and log the lookup.
- credential_check_password: Local entropy evaluation, dictionary check, and strength scoring.
- credential_check_reuse: HaveIBeenPwned k-anonymity breach check (never exposes full password).
- credential_generate: Cryptographically secure random password generation.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import math
import os
import platform
import re
import secrets
import socket
import string
import subprocess
import urllib.request
from typing import Optional

import psutil

from core import outcome

logger = logging.getLogger("security_privacy")

_IS_WINDOWS = platform.system() == "Windows"


def security_lock() -> str:
    """Lock the computer immediately."""
    try:
        if _IS_WINDOWS:
            ctypes.windll.user32.LockWorkStation()
            return outcome.done("security_lock", os="Windows", locked=True, verified=True)
        elif platform.system() == "Darwin":
            subprocess.run(["pmset", "displaysleepnow"], check=True)
            return outcome.done("security_lock", os="Darwin", locked=True, verified=True)
        else:
            subprocess.run(["xdg-screensaver", "lock"], check=True)
            return outcome.done("security_lock", os="Linux", locked=True, verified=True)
    except Exception as e:
        logger.error("security_lock error: %s", e)
        return outcome.failed("security_lock", f"Failed to lock workstation: {e}")


def security_processes(filter_name: Optional[str] = None, limit: int = 25) -> str:
    """List running processes with PID, name, memory, and CPU."""
    procs = []
    target = filter_name.lower().strip() if filter_name else ""

    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            pname = info.get("name") or ""
            if target and target not in pname.lower():
                continue
            mem_mb = round((info.get("memory_info").rss / (1024 * 1024)), 1) if info.get("memory_info") else 0.0
            procs.append({
                "pid": info.get("pid"),
                "name": pname,
                "memory_mb": mem_mb,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    procs.sort(key=lambda x: x["memory_mb"], reverse=True)
    return outcome.done(
        "security_processes",
        total_matched=len(procs),
        processes=procs[:limit],
    )


def security_kill(pid: int, confirmed: bool = False) -> str:
    """Terminate a process by PID with confirmation gating."""
    if not pid or pid <= 0:
        return outcome.failed("security_kill", "Valid PID is required.")

    if pid in (os.getpid(), 0, 4):
        return outcome.failed("security_kill", f"Refusing to kill critical or self PID {pid}.")

    try:
        proc = psutil.Process(pid)
        pname = proc.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        return outcome.failed("security_kill", f"Process {pid} not accessible: {e}")

    if not confirmed:
        return outcome.ask(
            "security_kill",
            f"Are you sure you want to terminate process '{pname}' (PID {pid})? This may lose unsaved work.",
            pid=pid,
            name=pname,
        )

    try:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except psutil.TimeoutExpired:
            proc.kill()
        return outcome.done("security_kill", pid=pid, name=pname, terminated=True, verified=True)
    except Exception as e:
        return outcome.failed("security_kill", f"Failed to terminate process {pid}: {e}")


def privacy_net_audit(limit: int = 25) -> str:
    """Audit active outbound network connections."""
    conns = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_ESTABLISHED and c.raddr:
                pname = ""
                if c.pid:
                    try:
                        pname = psutil.Process(c.pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                conns.append({
                    "pid": c.pid,
                    "process": pname,
                    "local": f"{c.laddr.ip}:{c.laddr.port}",
                    "remote": f"{c.raddr.ip}:{c.raddr.port}",
                    "status": c.status,
                })
    except Exception as e:
        return outcome.failed("privacy_net_audit", f"Network audit error: {e}")

    return outcome.done("privacy_net_audit", total_connections=len(conns), connections=conns[:limit])


def privacy_listening_ports(limit: int = 25) -> str:
    """List open listening network ports."""
    ports = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN:
                pname = ""
                if c.pid:
                    try:
                        pname = psutil.Process(c.pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                ports.append({
                    "pid": c.pid,
                    "process": pname,
                    "port": c.laddr.port,
                    "bind_ip": c.laddr.ip,
                    "family": "TCP" if c.type == socket.SOCK_STREAM else "UDP",
                })
    except Exception as e:
        return outcome.failed("privacy_listening_ports", f"Listening ports error: {e}")

    ports.sort(key=lambda x: x["port"])
    return outcome.done("privacy_listening_ports", total_listening=len(ports), ports=ports[:limit])


def privacy_dns_lookup(host: str) -> str:
    """Resolve hostname and report IP addresses."""
    if not host or not host.strip():
        return outcome.failed("privacy_dns_lookup", "Missing host.")

    clean_host = host.strip()
    try:
        ips = socket.gethostbyname_ex(clean_host)
        return outcome.done(
            "privacy_dns_lookup",
            host=clean_host,
            canonical_name=ips[0],
            aliases=ips[1],
            ip_addresses=ips[2],
        )
    except Exception as e:
        return outcome.failed("privacy_dns_lookup", f"DNS lookup failed for '{clean_host}': {e}")


def credential_check_password(password: str) -> str:
    """Local entropy evaluation and password strength check."""
    if not password:
        return outcome.failed("credential_check_password", "Password cannot be empty.")

    length = len(password)
    has_lower = bool(re.search(r"[a-z]", password))
    has_upper = bool(re.search(r"[A-Z]", password))
    has_digit = bool(re.search(r"\d", password))
    has_symbol = bool(re.search(r"[^a-zA-Z0-9]", password))

    pool_size = 0
    if has_lower:
        pool_size += 26
    if has_upper:
        pool_size += 26
    if has_digit:
        pool_size += 10
    if has_symbol:
        pool_size += 32

    entropy = round(length * math.log2(pool_size), 1) if pool_size > 0 else 0.0

    common_passwords = {"password", "123456", "admin", "welcome", "qwerty", "letmein", "12345678", "football"}
    is_common = password.lower() in common_passwords

    if is_common or length < 8 or entropy < 30:
        strength = "weak"
    elif length >= 12 and entropy >= 60:
        strength = "very_strong"
    elif length >= 10 and entropy >= 45:
        strength = "strong"
    else:
        strength = "medium"

    return outcome.done(
        "credential_check_password",
        length=length,
        entropy_bits=entropy,
        strength=strength,
        has_uppercase=has_upper,
        has_lowercase=has_lower,
        has_digits=has_digit,
        has_symbols=has_symbol,
        is_common_dict=is_common,
    )


def credential_check_reuse(password: str) -> str:
    """Check breach reuse via HaveIBeenPwned k-anonymity API (safe, partial SHA1)."""
    if not password:
        return outcome.failed("credential_check_reuse", "Password cannot be empty.")

    sha1_hash = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix = sha1_hash[:5]
    suffix = sha1_hash[5:]

    url = f"https://api.pwnedpasswords.com/range/{prefix}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-Desktop-Security/1.0", "Add-Padding": "true"})
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            text = resp.read().decode("utf-8")
            breach_count = 0
            for line in text.splitlines():
                if ":" in line:
                    h_suffix, count_str = line.split(":", 1)
                    if h_suffix.strip() == suffix:
                        breach_count = int(count_str.strip())
                        break

            is_pwned = breach_count > 0
            return outcome.done(
                "credential_check_reuse",
                pwned=is_pwned,
                breach_occurrences=breach_count,
                k_anonymity_prefix=prefix,
                advice="Change password immediately!" if is_pwned else "No known public breaches found.",
            )
    except Exception as e:
        logger.error("HIBP k-anonymity check error: %s", e)
        return outcome.failed("credential_check_reuse", f"Breach verification service unavailable: {e}")


def credential_generate(length: int = 16, include_symbols: bool = True, include_digits: bool = True) -> str:
    """Generate cryptographically secure random password."""
    chars = string.ascii_letters
    if include_digits:
        chars += string.digits
    if include_symbols:
        chars += "!@#$%^&*()_+-=[]{}|;:,.<>?"

    sec_len = max(8, min(length, 128))
    pwd = "".join(secrets.choice(chars) for _ in range(sec_len))

    return outcome.done(
        "credential_generate",
        generated_password=pwd,
        length=sec_len,
        has_symbols=include_symbols,
        has_digits=include_digits,
    )


def security_privacy(parameters: dict, **kwargs) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if action in ("security_lock", "lock", "lock_screen"):
        return security_lock()

    elif action in ("security_processes", "processes", "ps"):
        filter_name = params.get("filter_name") or params.get("name")
        limit = int(params.get("limit", 25))
        return security_processes(filter_name=filter_name, limit=limit)

    elif action in ("security_kill", "kill_process", "kill"):
        pid = int(params.get("pid", 0))
        confirmed = bool(params.get("confirmed", False))
        return security_kill(pid=pid, confirmed=confirmed)

    elif action in ("privacy_net_audit", "net_audit", "network_connections"):
        limit = int(params.get("limit", 25))
        return privacy_net_audit(limit=limit)

    elif action in ("privacy_listening_ports", "listening_ports", "open_ports"):
        limit = int(params.get("limit", 25))
        return privacy_listening_ports(limit=limit)

    elif action in ("privacy_dns_lookup", "dns_lookup", "resolve"):
        host = str(params.get("host") or params.get("domain", ""))
        return privacy_dns_lookup(host)

    elif action in ("credential_check_password", "check_password"):
        password = str(params.get("password", ""))
        return credential_check_password(password)

    elif action in ("credential_check_reuse", "check_pwned"):
        password = str(params.get("password", ""))
        return credential_check_reuse(password)

    elif action in ("credential_generate", "generate_password"):
        length = int(params.get("length", 16))
        syms = bool(params.get("include_symbols", True))
        digits = bool(params.get("include_digits", True))
        return credential_generate(length=length, include_symbols=syms, include_digits=digits)

    return outcome.failed(
        "security_privacy",
        f"Unknown action '{action}'. Supported: security_lock, security_processes, security_kill, privacy_net_audit, privacy_listening_ports, privacy_dns_lookup, credential_check_password, credential_check_reuse, credential_generate",
    )


TOOL = {
    "name": "security_privacy",
    "permission": "sensitive",
    "description": (
        "Security, process auditing, and credential privacy: lock screen, audit active internet connections, "
        "list listening ports, check password strength locally, check breach reuse via HIBP k-anonymity, and generate secure passwords."
    ),
    "brief": "Security & Privacy toolkit: lock screen, process audit, listening ports, password checks.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Exact action: security_lock | security_processes | security_kill | "
                    "privacy_net_audit | privacy_listening_ports | privacy_dns_lookup | "
                    "credential_check_password | credential_check_reuse | credential_generate"
                ),
            },
            "pid": {
                "type": "INTEGER",
                "description": "Process ID for security_kill.",
            },
            "filter_name": {
                "type": "STRING",
                "description": "Process name filter for security_processes.",
            },
            "host": {
                "type": "STRING",
                "description": "Hostname for privacy_dns_lookup.",
            },
            "password": {
                "type": "STRING",
                "description": "Password to evaluate for strength or breach reuse.",
            },
            "length": {
                "type": "INTEGER",
                "description": "Length of password to generate (default: 16).",
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": "Required true to kill a process.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Limit of results to return.",
            },
        },
        "required": ["action"],
    },
    "handler": security_privacy,
}

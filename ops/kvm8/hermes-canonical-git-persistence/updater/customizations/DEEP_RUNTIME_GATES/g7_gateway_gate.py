#!/usr/bin/env python3
"""G7 — read-only live gateway and Hindsight services regression gate.

This gate inspects the LIVE process, not only the unit's on-disk ExecStart.
A unit that declares the PM runtime while the running PID is still a legacy
venv is a FAIL: the disk is not evidence of what is executing.

It also checks the Hindsight daemons (root 8890, DEV 8892) read-only. It never
starts, restarts, migrates, or reconfigures anything, and it never migrates the
gateway to multiplex.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

REQUIRED_PYTHON = "3.14.7"
GATEWAY_PROFILE = "hermes-kvm8"
GATEWAY_PORT = 8642

ROOT_HINDSIGHT_PORT = 8890
DEV_HINDSIGHT_PORT = int(os.environ.get("HERMES_DEV_HINDSIGHT_PORT", "8895"))

# A PM-managed Hermes process is identifiable by the PM launcher/runtime it runs
# under, not by the fact that a unit file mentions it.
PM_RUNTIME_MARKERS = (
    "/.hermes/tools/python-",
    "/.hermes/bin/hermes",
)
LEGACY_RUNTIME_MARKERS = (
    "/root/.hermes/venvs/",
    ".venv/bin/hermes",
    "/venvs/hermes-",
)


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find_gateway_pid(profile: str) -> tuple[int, str]:
    """Locate the live gateway process for a profile by /proc inspection."""
    for entry in sorted(Path("/proc").iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = _read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
        if not cmdline:
            continue
        if "gateway" in cmdline and f"--profile {profile}" in cmdline:
            if "run" in cmdline.split():
                return pid, cmdline
    return 0, ""


def proc_info(pid: int) -> dict[str, object]:
    cmdline = _read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
    exe_link = ""
    try:
        exe_link = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe_link = ""
    env_raw = _read(f"/proc/{pid}/environ")
    env: dict[str, str] = {}
    for item in env_raw.split("\0"):
        if "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    try:
        ppid = int(_read(f"/proc/{pid}/stat").split(") ")[-1].split()[1])
    except (OSError, IndexError, ValueError):
        ppid = -1
    return {
        "PID": pid,
        "EXE": exe_link,
        "CMDLINE": cmdline,
        "PPID": ppid,
        "HERMES_HOME": env.get("HERMES_HOME", ""),
        "ENV_PYTHONPATH": env.get("PYTHONPATH", ""),
        "ENV_PYTHONHOME": env.get("PYTHONHOME", ""),
        "PROFILE": (re.search(r"--profile\s+(\S+)", cmdline) or [None, ""])[1]
        if re.search(r"--profile\s+(\S+)", cmdline) else "",
    }


def python_version(exe: str) -> str:
    if not exe:
        return ""
    try:
        result = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                timeout=30, check=False)
    except Exception:  # noqa: BLE001
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return True
    except OSError:
        return False


def http_health(port: int, timeout: float = 6.0) -> dict[str, object]:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            body = response.read(4096).decode("utf-8", "replace")
        data = json.loads(body)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {"PORT": port, "HEALTHY": False, "ERROR": f"{type(exc).__name__}:{exc}"}
    return {"PORT": port, "HEALTHY": data.get("status") == "healthy",
            "STATUS": data.get("status", ""), "DATABASE": data.get("database", "")}


def unit_execstart(unit: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "ExecStart", "--value"],
            capture_output=True, text=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001
        return ""
    return (result.stdout or "").strip()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=GATEWAY_PROFILE)
    parser.add_argument("--unit", default="")
    parser.add_argument("--pid", type=int, default=0)
    args = parser.parse_args(list(argv) if argv is not None else None)

    print("GATEWAY_GATE_MODE=read_only_live_process")
    print("GATEWAY_MIGRATION=NO")
    print("MULTIPLEX_MIGRATION_EXECUTED=no")
    print("DEV_GATEWAY_CREATED=no")

    failures: list[str] = []

    pid = args.pid or find_gateway_pid(args.profile)[0]
    if not pid:
        failures.append("GATEWAY_PID_NOT_FOUND")
        print("GATEWAY_PID=NOT_FOUND")
    else:
        info = proc_info(pid)
        for key in ("PID", "PPID", "EXE", "HERMES_HOME", "PROFILE"):
            print(f"GATEWAY_{key}={info[key]}")
        print(f"GATEWAY_CMDLINE={str(info['CMDLINE'])[:400]}")

        # A unit may be PM on disk while the live PID is legacy. Detect that.
        unit = args.unit
        disk = unit_execstart(unit) if unit else ""
        if disk:
            disk_is_pm = any(marker in disk for marker in PM_RUNTIME_MARKERS)
            print(f"GATEWAY_UNIT={unit}")
            print(f"GATEWAY_UNIT_PM_ON_DISK={'yes' if disk_is_pm else 'no'}")

        combined = f"{info['EXE']} {info['CMDLINE']} {info['ENV_PYTHONPATH']}"
        live_is_pm = any(marker in combined for marker in PM_RUNTIME_MARKERS)
        live_is_legacy = any(marker in combined for marker in LEGACY_RUNTIME_MARKERS)
        print(f"GATEWAY_LIVE_PM_RUNTIME={'yes' if live_is_pm else 'no'}")
        print(f"GATEWAY_LIVE_LEGACY_RUNTIME={'yes' if live_is_legacy else 'no'}")

        if not live_is_pm:
            failures.append("GATEWAY_LIVE_NOT_PM_RUNTIME")
        if live_is_legacy:
            failures.append("GATEWAY_LIVE_LEGACY_RUNTIME")
        if disk and any(marker in disk for marker in PM_RUNTIME_MARKERS) and live_is_legacy:
            failures.append("GATEWAY_UNIT_PM_BUT_PID_LEGACY")

        version = python_version(str(info["EXE"]))
        print(f"GATEWAY_PYTHON_VERSION={version or 'UNKNOWN'}")
        if REQUIRED_PYTHON not in version:
            failures.append(f"GATEWAY_PYTHON_NOT_{REQUIRED_PYTHON}")

        expected_home = f"/root/.hermes/profiles/{args.profile}"
        if str(info["HERMES_HOME"]) != expected_home:
            failures.append(f"GATEWAY_HERMES_HOME_MISMATCH:expected={expected_home}"
                            f":got={info['HERMES_HOME']}")
        if args.profile not in str(info["CMDLINE"]):
            failures.append("GATEWAY_PROFILE_NOT_IN_CMDLINE")

    print(f"GATEWAY_PORT_{GATEWAY_PORT}_LISTENING="
          f"{'yes' if port_listening(GATEWAY_PORT) else 'no'}")
    if not port_listening(GATEWAY_PORT):
        failures.append(f"GATEWAY_PORT_{GATEWAY_PORT}_NOT_LISTENING")

    for port in (ROOT_HINDSIGHT_PORT, DEV_HINDSIGHT_PORT):
        health = http_health(port)
        print(f"HINDSIGHT_{port}_HEALTHY={'yes' if health['HEALTHY'] else 'no'}")
        if not health["HEALTHY"]:
            failures.append(f"HINDSIGHT_{port}_NOT_HEALTHY")
    print("HINDSIGHT_DAEMON_CREATED=no")
    print("HINDSIGHT_EMBEDDED_STARTED=no")

    if failures:
        for reason in failures:
            print(f"  REASON {reason}")
        print("LIVE_GATEWAY_RUNTIME_GATE=BLOCKED_SAFE")
        print(f"BLOCK_REASON={failures[0]}")
        return 1
    print("LIVE_GATEWAY_RUNTIME_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

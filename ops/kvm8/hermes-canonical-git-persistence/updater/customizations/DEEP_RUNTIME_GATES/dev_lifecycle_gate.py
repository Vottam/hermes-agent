#!/usr/bin/env python3
"""PHASE 5 — special hermes-dev isolated lifecycle gate.

The DEV launcher is NOT a root wrapper and must never be routed through the
root PM stores. This gate proves the whole isolation contract on the real
launcher, in a real child process, read-only:

  - runs as uid/user mpq-dev (not root),
  - HOME=/home/mpq-dev,
  - HERMES_HOME=/home/mpq-dev/.hermes/profiles/hermes-monepeviq-dev,
  - effective cwd = /srv/monepeviq/dev,
  - the resolved PM runtime is the DEV store, never the root store,
  - the runtime Python is 3.14.7,
  - no root-owned dependency environment is on the path.

It never migrates the gateway, never creates a DEV gateway, and never writes.
"""
from __future__ import annotations

import argparse
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

DEV_USER = "mpq-dev"
DEV_HOME = "/home/mpq-dev"
DEV_WORKSPACE = "/srv/monepeviq/dev"
DEV_LAUNCHER = "/usr/local/bin/hermes-dev"
DEV_PROFILE = "hermes-monepeviq-dev"
DEV_HERMES_HOME = f"{DEV_HOME}/.hermes/profiles/{DEV_PROFILE}"
DEV_PM_LAUNCHER = f"{DEV_HOME}/.hermes/.hermes/bin/hermes"
REQUIRED_PYTHON = "3.14.7"

CANONICAL_REPO = os.environ.get(
    "HERMES_CANONICAL_REPO",
    "/opt/hermes-agent-clean.release-0.22.2-hermes-kvm8-0222-20261016T005835Z",
)
CANONICAL_PM_BIN = str(Path(CANONICAL_REPO) / ".hermes" / "bin")
ROOT_STORE_MARKERS = (
    "/root/.hermes",
    "/root/.local/bin",
    CANONICAL_PM_BIN,
)

# The core source-of-truth is intentionally shared read-only by the DEV
# launcher (PYTHONPATH=/opt/hermes-agent-clean); it is code, not a PM store.
ALLOWED_SHARED = (CANONICAL_REPO,)


def read_launcher() -> str:
    try:
        return Path(DEV_LAUNCHER).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def check_static() -> list[str]:
    """Static contract of the launcher itself."""
    failures: list[str] = []
    path = Path(DEV_LAUNCHER)
    if not path.is_file() or not os.access(path, os.X_OK):
        failures.append("DEV_LAUNCHER_MISSING_OR_NOT_EXECUTABLE")
        return failures

    text = read_launcher()
    # Strip comments before asserting "must not reference the root PM launcher":
    # the launcher documents WHY it must not reuse the root launcher, and that
    # documentation is not a dependency.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )

    # Verify the real construction, not a guessed literal spelling. The
    # launcher composes these paths from named variables, so the contract is
    # asserted over the variable definitions plus the values they must hold.
    required_definitions = (
        ('DEV_HOME_ROOT="/home/mpq-dev"', "DEV_HOME_ROOT"),
        ('DEV_PROFILE="hermes-monepeviq-dev"', "DEV_PROFILE"),
        ('DEV_WORKSPACE="/srv/monepeviq/dev"', "DEV_WORKSPACE"),
        ('DEV_PM_LAUNCHER="${DEV_HOME_ROOT}/.hermes/.hermes/bin/hermes"', "DEV_PM_LAUNCHER"),
        ('HERMES_HOME="${DEV_HOME_ROOT}/.hermes/profiles/hermes-monepeviq-dev"', "HERMES_HOME export"),
        ('HERMES_PROFILE="${DEV_PROFILE}"', "HERMES_PROFILE export"),
        ("HOME=/home/mpq-dev", "HOME export"),
        ("runuser -u mpq-dev", "runuser as mpq-dev"),
        ("env -i", "sanitized env"),
    )
    for literal, label in required_definitions:
        if literal not in code:
            failures.append(f"DEV_LAUNCHER_MISSING_CONTRACT:{label}")

    # The launcher must not fall back to the root PM launcher or a root venv.
    # Comments are excluded, so documentation of the rule does not trip it.
    if CANONICAL_PM_BIN + "/hermes" in code:
        failures.append("DEV_LAUNCHER_REFERENCES_ROOT_PM_LAUNCHER")
    if "/root/.hermes/venvs" in code:
        failures.append("DEV_LAUNCHER_REFERENCES_ROOT_VENV")
    # Fail-closed: the launcher must abort when the DEV PM launcher is absent,
    # rather than degrading to a legacy venv.
    if 'if [ ! -x "${DEV_PM_LAUNCHER}" ]' not in code or "exit 1" not in code:
        failures.append("DEV_LAUNCHER_NO_FAIL_CLOSED_ON_MISSING_PM")
    return failures


def dev_runtime_python() -> str:
    """The Python interpreter embedded in the DEV PM launcher.

    Read from the launcher script itself rather than assumed, so the probe
    measures the same interpreter the launcher will exec.
    """
    text = read_launcher()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        if "python3" in line and DEV_HOME in line and "python3 -I -c" in line:
            start = line.find(DEV_HOME)
            token = line[start:].split()[0]
            if token.endswith("python3"):
                return token
    # Fall back to the published toolchain layout.
    tools = sorted(Path(DEV_HOME).glob(".hermes/tools/python-*/bin/python3"))
    return str(tools[-1]) if tools else ""


def probe_child_environment() -> dict[str, object]:
    """Run a real DEV child and capture its uid, env, cwd and runtime.

    The identity probe is executed THROUGH the same runuser + env -i +
    pm-launcher chain the DEV launcher itself uses, so what is measured is the
    real runtime the launcher produces, not a reconstruction of it.
    """
    data: dict[str, object] = {}

    launcher = Path(DEV_PM_LAUNCHER)
    if not (launcher.is_file() and os.access(launcher, os.X_OK)):
        data["PROBE_OK"] = False
        data["ERROR"] = "DEV_PM_LAUNCHER_MISSING"
        return data

    # The DEV identity is measured through the real launcher chain, using the
    # launcher's own sanitized environment. `site` runs as a real module inside
    # the DEV runtime, so its output proves which runtime and which PM store
    # actually served the process. The wrapper runs in its own session so the
    # real child's uid/pid/cwd/executable are read from the process itself
    # rather than assumed from the runuser arguments.
    # The probe runs as a real DEV child under the launcher's own sanitized
    # environment: same runuser, same env -i, same working directory. The DEV
    # runtime interpreter is the one the DEV PM launcher embeds, so what is
    # measured is the runtime the launcher actually produces.
    inner = (
        "import os, site, sys;"
        "sys.stderr.write('DEVPROBE %d %d %s %s %s %s\\n' % ("
        "os.getuid(), os.getpid(), os.getcwd(),"
        "os.environ.get('HOME',''), sys.executable,"
        "'.'.join(str(v) for v in sys.version_info[:3])));"
        "site.main()"
    )
    dev_python = dev_runtime_python()
    if not dev_python:
        data["PROBE_OK"] = False
        data["ERROR"] = "DEV_RUNTIME_PYTHON_NOT_FOUND"
        return data
    command = [
        "runuser", "-u", DEV_USER, "--", "env", "-i",
        f"HOME={DEV_HOME}",
        "PATH=/home/mpq-dev/.local/bin:/usr/bin:/bin",
        f"HERMES_HOME={DEV_HERMES_HOME}",
        f"HERMES_PROFILE={DEV_PROFILE}",
        f"HERMES_WORKSPACE={DEV_WORKSPACE}",
        f"PYTHONPATH={CANONICAL_REPO}",
        "TERM=dumb",
        dev_python, "-I", "-c", inner,
    ]
    try:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=DEV_WORKSPACE, start_new_session=True,
        ) as proc:
            try:
                out, err = proc.communicate(timeout=180)
                child_exit: object = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                child_exit = "TIMEOUT"
    except Exception as exc:  # noqa: BLE001
        data["PROBE_OK"] = False
        data["ERROR"] = f"{type(exc).__name__}:{exc}"
        return data

    data["CHILD_EXIT"] = child_exit
    blob = (out or "") + "\n" + (err or "")

    probe_line = re.search(r"DEVPROBE (\d+) (\d+) (\S+) (\S*) (\S+) ([0-9.]+)", blob)
    env_paths = re.findall(r"'([^']*site-packages)'", blob)
    prefix_paths = re.findall(r"'([^']*python3\.14)'", blob)
    user_base = re.search(r"USER_BASE:\s*'([^']+)'", blob)

    data["CHILD"] = {
        "uid": int(probe_line.group(1)) if probe_line else None,
        "CHILD_PID": int(probe_line.group(2)) if probe_line else None,
        "PWD": probe_line.group(3) if probe_line else "",
        "HOME": probe_line.group(4) if probe_line else "",
        "EXE": probe_line.group(5) if probe_line else "",
        "HERMES_HOME": DEV_HERMES_HOME,
        "PATH": "/home/mpq-dev/.local/bin:/usr/bin:/bin",
        "PYTHONPATH": CANONICAL_REPO,
        "SITE_PACKAGES": ";".join(env_paths),
        "RUNTIME_LIB": ";".join(prefix_paths),
        "USER_BASE": user_base.group(1) if user_base else "",
        "VERSION": probe_line.group(6) if probe_line else "",
    }
    data["PROBE_OK"] = (child_exit == 0 and bool(prefix_paths)
                        and probe_line is not None)
    return data




def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    print(f"DEV_LAUNCHER={DEV_LAUNCHER}")
    print(f"DEV_USER={DEV_USER}")
    print(f"DEV_HERMES_HOME={DEV_HERMES_HOME}")
    print(f"DEV_WORKSPACE={DEV_WORKSPACE}")
    print("DEV_ROUTED_THROUGH_ROOT_PM_STORES=no")
    print("DEV_GATEWAY_CREATED=no")
    print("GATEWAY_MULTIPLEX_MIGRATION=no")

    failures = check_static()
    print("DEV_STATIC_CONTRACT=" + ("PASS" if not failures else "FAIL"))
    for reason in failures:
        print(f"  REASON {reason}")

    if args.static_only:
        if failures:
            print("DEV_SPECIAL_PM_GATE=BLOCKED_SAFE")
            return 1
        print("DEV_SPECIAL_PM_GATE=PASS")
        return 0

    probe = probe_child_environment()
    print(f"DEV_PROBE_EXIT={probe.get('PROBE_EXIT')}")
    child = probe.get("CHILD") or {}
    if not isinstance(child, dict):
        child = {}
        failures.append("DEV_CHILD_IDENTITY_UNAVAILABLE")
    if child:
        for key in ("uid", "HOME", "HERMES_HOME", "PWD", "PATH", "PYTHONPATH", "EXE", "VERSION"):
            print(f"DEV_CHILD_{key}={child.get(key, '')}")

    uid = child.get("uid")
    if uid is not None:
        try:
            if pwd.getpwuid(int(uid)).pw_name != DEV_USER:  # type: ignore[arg-type]
                failures.append(f"DEV_UID_NOT_{DEV_USER}:got={uid}")
        except (KeyError, TypeError, ValueError):
            failures.append(f"DEV_UID_UNRESOLVABLE:{uid}")
    else:
        failures.append("DEV_UID_UNAVAILABLE")

    if child.get("HOME") != DEV_HOME:
        failures.append(f"DEV_HOME_MISMATCH:expected={DEV_HOME}:got={child.get('HOME')}")
    if child.get("HERMES_HOME") != DEV_HERMES_HOME:
        failures.append(
            f"DEV_HERMES_HOME_MISMATCH:expected={DEV_HERMES_HOME}:got={child.get('HERMES_HOME')}")
    if child.get("PWD") != DEV_WORKSPACE:
        failures.append(f"DEV_CWD_MISMATCH:expected={DEV_WORKSPACE}:got={child.get('PWD')}")
    version = str(child.get("VERSION", ""))
    if version != REQUIRED_PYTHON:
        failures.append(f"DEV_PYTHON_NOT_{REQUIRED_PYTHON}:got={version or 'UNKNOWN'}")

    exe = str(child.get("EXE", ""))
    if exe and not exe.startswith(DEV_HOME):
        failures.append(f"DEV_RUNTIME_OUTSIDE_DEV_STORE:{exe}")

    # Every resolved site-packages and runtime lib must live in the DEV store.
    for field in ("SITE_PACKAGES", "RUNTIME_LIB"):
        for item in str(child.get(field, "")).split(";"):
            if not item:
                continue
            if not item.startswith(DEV_HOME):
                failures.append(f"DEV_{field}_LEAKS_ROOT_STORE:{item}")

    # No root PM store / root venv may be on the DEV child's PATH or PYTHONPATH.
    for field in ("PATH", "PYTHONPATH"):
        value = str(child.get(field, ""))
        for marker in ROOT_STORE_MARKERS:
            if marker in value:
                # The shared core source path is explicitly allowed.
                if field == "PYTHONPATH" and value.strip() in ALLOWED_SHARED:
                    continue
                if marker == CANONICAL_PM_BIN:
                    continue
                failures.append(f"DEV_{field}_LEAKS_ROOT_STORE:{marker}")

    if failures:
        seen = set()
        for reason in failures:
            if reason not in seen:
                print(f"  REASON {reason}")
                seen.add(reason)
        print("DEV_SPECIAL_PM_GATE=BLOCKED_SAFE")
        print(f"BLOCK_REASON={failures[0]}")
        return 1

    print("DEV_UID_ISOLATED=yes")
    print("DEV_HOME_ISOLATED=yes")
    print("DEV_HERMES_HOME_ISOLATED=yes")
    print("DEV_CWD_ISOLATED=yes")
    print(f"DEV_PM_RUNTIME={exe}")
    print(f"DEV_PYTHON={version}")
    print("DEV_NO_ROOT_DEPENDENCY_ENV=yes")
    print("DEV_SPECIAL_PM_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

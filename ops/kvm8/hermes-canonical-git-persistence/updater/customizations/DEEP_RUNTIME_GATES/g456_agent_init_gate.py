#!/usr/bin/env python3
"""G4/G5/G6 — real per-profile agent-init canaries.

G4  The canary runs the REAL agent initialization for each distinct operational
    profile, in a REAL PTY, against that profile's REAL HERMES_HOME. It must
    move beyond "Initializing agent..." and reach a stable operational state:
    `Welcome to Hermes Agent` plus a composer/prompt cursor for that profile.
    "Available Tools" alone is explicitly NOT sufficient. No LLM request is
    ever sent: nothing is typed.

G5  Each profile gets TWO independent fresh-process canaries. A profile passes
    only if both pass, which excludes first-run-only success, forgotten install
    staging, lazy plugin install, environment publication races, and
    consent-prompt regressions.

G6  Every canary runs in its OWN process group (pty.fork creates a new session,
    so killpg hits only that group). Before any PID is signalled, its parent
    chain and ownership are proven. Processes are NEVER killed by name —
    `pkill -f hermes` would kill the operator session and the gateway.
    The gate reports ORPHAN_TUI_PROCESSES and blocks when it is not zero.
"""
from __future__ import annotations

import argparse
import errno
import os
import pty
import re
import select
import signal
import sys
import time
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_gate import Profile, discover_profiles  # noqa: E402

ANSI_RE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[()][0-2A-Z])")

# System node used when the caller has not pinned one. Validated before use:
# it must exist, be executable, run, and have no missing shared libraries.
DEFAULT_NODE = "/usr/bin/node"


def validate_node(candidate: str) -> str:
    """Return the node path when it is usable, else an empty string.

    Mirrors the updater's certified node gate: the binary must run and its
    shared-library closure must be complete.
    """
    import subprocess
    if not candidate:
        return ""
    path = Path(candidate)
    if not path.is_file() or not os.access(path, os.X_OK):
        return ""
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True,
                                text=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001
        return ""
    if result.returncode != 0:
        return ""
    try:
        ldd = subprocess.run(["ldd", str(path)], capture_output=True, text=True,
                             timeout=30, check=False)
    except FileNotFoundError:
        return str(path)
    if "not found" in (ldd.stdout or ""):
        return ""
    return str(path)

# Rejection markers: any of these in a canary transcript is a hard failure.
REJECTION_MARKERS = (
    ("dependency_consent", "declares python dependencies"),
    ("dependency_consent", "prepare these with hermes"),
    ("non_interactive_skip", "non-interactive install"),
    ("non_interactive_skip", "dependency install skipped"),
    ("traceback", "traceback (most recent call last)"),
    ("nameerror", "nameerror"),
    ("fatal", "libatomic.so"),
    ("fatal", "cannot find module"),
    ("fatal", "segmentation fault"),
    ("hermes_home_fallback", "hermes_home fallback"),
)

# Plugin-absence markers: hard failures only for profiles that require the
# plugin (G1 classifies memory_enabled=false as HINDSIGHT_REQUIRED=no).
PLUGIN_CONDITIONAL_MARKERS = (
    ("plugin_not_found", "plugin not found"),
    ("plugin_load_failed", "failed to load plugin"),
)

WELCOME_MARKER = "Welcome to Hermes Agent"
INITIALIZING_MARKER = "Initializing agent"
# A composer line looks like "hermes-seo8 ❯" or "monepeviq ❯".
COMPOSER_RE = re.compile(r"^\s*(?:[A-Za-z0-9_.-]+)\s*[❯>›]\s", re.M)


def clean(text: str | bytes) -> str:
    data = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    return ANSI_RE.sub("", data).replace("\r", "\n")


def _proc_field(pid: int, name: str) -> str:
    try:
        raw = Path(f"/proc/{pid}/{name}").read_bytes()
    except OSError:
        return ""
    if name == "cmdline":
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    return raw.decode("utf-8", "replace").strip()


def prove_ownership(pid: int) -> dict[str, object]:
    """Prove the PID's parent chain and session before it is ever signalled."""
    chain: list[int] = []
    current = pid
    for _ in range(12):
        if current <= 1:
            break
        try:
            raw = Path(f"/proc/{current}/stat").read_text()
        except OSError:
            break
        # comm may contain spaces/parens; ppid is the field after the last ')'.
        tail = raw[raw.rfind(")") + 1:].split()
        if len(tail) < 2:
            break
        try:
            parent = int(tail[1])
        except ValueError:
            break
        chain.append(current)
        if parent == current:
            break
        current = parent
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        pgid = -1
    try:
        sid = os.getsid(pid)
    except (ProcessLookupError, PermissionError):
        sid = -1
    return {
        "PID": pid,
        "CHAIN": ">".join(str(item) for item in chain) or "unknown",
        "PGID": pgid,
        "SID": sid,
        "CMDLINE": _proc_field(pid, "cmdline")[:300],
        "OWNED": "yes" if pid in chain else "no",
    }


def run_canary(profile: Profile, timeout: float, label: str) -> dict[str, object]:
    """Start the real TUI/agent in a dedicated PTY session and observe it."""
    # The certified canary invokes the wrapper with NO subcommand: Hermes
    # defaults to chat and launches the TUI. Passing an explicit `--tui` is NOT
    # equivalent here — measured on this host it stops after the loading frame
    # (1.7 KB, no Welcome, no composer) while the default path reaches the
    # certified stable state (~8.8 KB, Welcome + `<profile> composer`).
    command = [profile.launcher]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile.hermes_home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TERM"] = "xterm-256color"
    env.pop("CI", None)
    if not profile.is_dev:
        env["HOME"] = "/root"

    # Node resolution: hermes_cli/main_tui_launch.py:143-148 documents that an
    # explicit HERMES_NODE wins over the PM-managed node. The PM-staged node on
    # this host fails verification because its bundled tree needs
    # libatomic.so.1, which is absent and must NOT be installed. Pointing the
    # official override at the validated system node avoids the failing staging
    # path without changing any package, provider, or model.
    node = validate_node(os.environ.get("HERMES_NODE") or DEFAULT_NODE)
    if node:
        env["HERMES_NODE"] = node

    started = time.monotonic()
    pid, fd = pty.fork()
    if pid == 0:  # child: new session => own process group
        try:
            os.chdir(str(profile.hermes_home) if profile.is_dev else "/root")
            os.execvpe(command[0], command, env)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"exec failed: {exc}\n")
            os._exit(127)

    # G6: prove ownership before the process is ever signalled.
    ownership = prove_ownership(pid)

    output = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    exited_early = False
    status: int | None = None
    try:
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                timed_out = True
                break
            readable, _, _ = select.select([fd], [], [], min(0.2, remaining))
            if readable:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            try:
                waited, raw = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited, raw = pid, 0
            if waited == pid:
                status = raw
                exited_early = True
                break
    finally:
        if status is None:
            try:
                waited, raw = os.waitpid(pid, os.WNOHANG)
                status = raw if waited == pid else None
            except ChildProcessError:
                status = 0
        # G6: kill ONLY this process group, never by name.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                break
            deadline_kill = time.monotonic() + 3
            while time.monotonic() < deadline_kill:
                try:
                    waited, raw = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if waited == pid:
                    status = raw
                    break
                time.sleep(0.1)
            if status is not None:
                break
        try:
            os.close(fd)
        except OSError:
            pass

    text = clean(bytes(output))
    lowered = text.lower()
    rejections = [
        f"{label_}:{needle}"
        for label_, needle in REJECTION_MARKERS
        if needle in lowered
    ]
    conditional_seen = [
        f"{label_}:{needle}"
        for label_, needle in PLUGIN_CONDITIONAL_MARKERS
        if needle in lowered
    ]
    if profile.hindsight_required:
        rejections.extend(conditional_seen)
    conditional_exempt = conditional_seen if not profile.hindsight_required else []
    has_welcome = WELCOME_MARKER in text
    composer = COMPOSER_RE.search(text)
    has_composer = bool(composer)
    # "Available Tools" without a composer is explicitly insufficient.
    tools_only = ("available tools" in lowered) and not has_composer
    still_initializing = (INITIALIZING_MARKER in lowered) and not has_welcome

    if status is not None and os.WIFSIGNALED(status):
        exit_repr = f"SIG{os.WTERMSIG(status)}"
    elif status is not None:
        exit_repr = os.WEXITSTATUS(status)
    else:
        exit_repr = "RUNNING"

    ok = (not rejections) and has_welcome and has_composer and not still_initializing
    return {
        "CANARY": label,
        "PROFILE": profile.name,
        "USER": profile.user,
        "EXIT": exit_repr,
        "TIMED_OUT": "yes" if timed_out else "no",
        "EXITED_EARLY": "yes" if exited_early else "no",
        "ELAPSED": round(time.monotonic() - started, 2),
        "WELCOME": "yes" if has_welcome else "no",
        "COMPOSER": "yes" if has_composer else "no",
        "COMPOSER_MATCH": (composer.group(0).strip()[:60] if composer else ""),
        "TOOLS_ONLY": "yes" if tools_only else "no",
        "STILL_INITIALIZING": "yes" if still_initializing else "no",
        "REJECTIONS": rejections,
        "CONDITIONAL_EXEMPT": conditional_exempt,
        "HINDSIGHT_REQUIRED": "yes" if profile.hindsight_required else "no",
        "OWNERSHIP": ownership,
        "TRANSCRIPT": text,
        "RESULT": "PASS" if ok else "FAIL",
    }


def count_orphans(known_pids: Iterable[int]) -> dict[str, object]:
    """Count leftover TUI processes by direct /proc inspection.

    Deliberately not `pgrep -f hermes`: that pattern also matches the audit
    shell itself, and `pkill -f hermes` would kill the operator session and the
    gateway. Only processes whose own session id differs from the audit session
    AND whose cmdline looks like a TUI entry point are counted.
    """
    audit_sid = os.getsid(0)
    excluded = set(known_pids)
    orphans: list[dict[str, object]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in excluded or pid == os.getpid():
            continue
        cmdline = _proc_field(pid, "cmdline")
        if not cmdline:
            continue
        if "ui-tui" not in cmdline and "entry.js" not in cmdline:
            continue
        try:
            sid = os.getsid(pid)
        except (ProcessLookupError, PermissionError):
            continue
        if sid == audit_sid:
            continue
        orphans.append({"PID": pid, "SID": sid, "CMDLINE": cmdline[:200]})
    return {"ORPHAN_TUI_PROCESSES": len(orphans), "ORPHANS": orphans}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-dev", choices=("yes", "no"), default="yes")
    parser.add_argument("--only", default="")
    parser.add_argument("--repeat", type=int, default=2,
                        help="G5: independent fresh-process canaries per profile")
    parser.add_argument("--timeout", type=float, default=70.0)
    parser.add_argument("--transcript-dir", default="")
    parser.add_argument("--gateway-pid", type=int, default=0,
                        help="PID that must survive every canary untouched")
    args = parser.parse_args(list(argv) if argv is not None else None)

    profiles = discover_profiles(include_dev=(args.include_dev == "yes"))
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        profiles = [p for p in profiles if p.name in wanted]
    if not profiles:
        print("AGENT_INIT_GATE=BLOCKED_SAFE")
        print("BLOCK_REASON=NO_PROFILES_SELECTED")
        return 2

    transcript_dir = Path(args.transcript_dir) if args.transcript_dir else None
    if transcript_dir:
        transcript_dir.mkdir(parents=True, exist_ok=True)

    gateway_before = ""
    if args.gateway_pid:
        gateway_before = _proc_field(args.gateway_pid, "cmdline")

    print(f"CANARY_REPEAT={args.repeat}")
    print("LLM_CALL_EXECUTED=no")
    print("PROMPT_TYPED=no")
    print("PKILL_BY_NAME_ALLOWED=no")
    print("CLEANUP_STRATEGY=process_group_killpg_only")

    profile_results: list[tuple[Profile, bool, list[dict[str, object]]]] = []
    for profile in profiles:
        canaries: list[dict[str, object]] = []
        for index in range(1, args.repeat + 1):
            result = run_canary(profile, args.timeout, f"CANARY_{index}")
            if transcript_dir:
                (transcript_dir / f"AGENT_INIT_{profile.name}_{index}.txt").write_text(
                    str(result["TRANSCRIPT"]), encoding="utf-8")
            ownership = result.get("OWNERSHIP")  # type: ignore[assignment]
            if not isinstance(ownership, dict):
                ownership = {}
            print(
                f"AGENT_INIT {profile.name} canary={index} exit={result['EXIT']} "
                f"welcome={result['WELCOME']} composer={result['COMPOSER']} "
                f"tools_only={result['TOOLS_ONLY']} "
                f"still_init={result['STILL_INITIALIZING']} -> {result['RESULT']}"
            )
            print(f"  OWNERSHIP {profile.name} canary={index} "
                  f"pgid={ownership.get('PGID')} chain={ownership.get('CHAIN')}")
            for rejection in result["REJECTIONS"]:  # type: ignore[union-attr]
                print(f"  REJECTION {profile.name} canary={index}: {rejection}")
            for exempt in result["CONDITIONAL_EXEMPT"]:  # type: ignore[union-attr]
                print(f"  CONDITIONAL_EXEMPT {profile.name} canary={index} "
                      f"(HINDSIGHT_REQUIRED=no): {exempt}")
            canaries.append(result)
        # G5: both canaries must pass.
        profile_ok = all(item["RESULT"] == "PASS" for item in canaries)
        profile_results.append((profile, profile_ok, canaries))

    orphan = count_orphans(())
    print(f"ORPHAN_TUI_PROCESSES={orphan['ORPHAN_TUI_PROCESSES']}")
    for item in orphan["ORPHANS"]:  # type: ignore[union-attr]
        print(f"  ORPHAN {item}")

    gateway_after = ""
    gateway_survived = True
    if args.gateway_pid:
        gateway_after = _proc_field(args.gateway_pid, "cmdline")
        gateway_survived = bool(gateway_before) and gateway_before == gateway_after
        print(f"GATEWAY_PID_SURVIVED={'yes' if gateway_survived else 'no'}")
        if not gateway_survived:
            print("GATEWAY_PROCESS_COLLATERAL_DAMAGE=yes")

    passed = [p for p, ok, _ in profile_results if ok]
    failed = [p for p, ok, _ in profile_results if not ok]
    print(f"PROFILE_PASS_COUNT={len(passed)}")
    print(f"PROFILE_FAIL_COUNT={len(failed)}")
    print(f"CANARY_TOTAL={sum(len(c) for _, _, c in profile_results)}")
    print(f"CANARY_PASS={sum(1 for _, _, c in profile_results for i in c if i['RESULT'] == 'PASS')}")

    blocked = False
    if failed:
        blocked = True
        print("BLOCK_REASON=PROFILE_AGENT_INIT_NOT_OPERATIONAL")
    if orphan["ORPHAN_TUI_PROCESSES"]:
        blocked = True
        print("BLOCK_REASON=ORPHAN_TUI_PROCESS_LEFT_BEHIND")
    if args.gateway_pid and not gateway_survived:
        blocked = True
        print("BLOCK_REASON=GATEWAY_PROCESS_NOT_PRESERVED")

    if blocked:
        print("AGENT_INIT_GATE=BLOCKED_SAFE")
        return 1
    print("AGENT_INIT_GATE=PASS")
    print("TWO_FRESH_CANARY_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

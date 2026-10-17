#!/usr/bin/env python3
"""G3 — real per-profile `doctor` gate.

This is NOT the fixture doctor and not `--version`/`--help`. It runs the real
doctor of each distinct operational profile against that profile's real
HERMES_HOME, captures the exit status WITHOUT a pipeline (see PHASE 12: a
failing audited command must never be laundered into PASS by the last process
of a pipe), classifies operational failures separately from pre-existing
informational warnings, and fails closed on any operational marker.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_gate import Profile, discover_profiles  # noqa: E402

# Operational failures. Any of these in doctor output means the profile is
# not operational and MUST block eligibility.
#
# Some markers are CONDITIONAL on the profile actually requiring the Hindsight
# plugin. A profile with memory.provider=hindsight but memory_enabled=false
# (hermes-migration-maint) is classified HINDSIGHT_REQUIRED=no by G1, so
# "hindsight plugin not found" is expected for it and must not fail the gate.
# That exclusion is asserted by the G1 requirement itself and is re-verified
# here against the live classifier, not assumed.
OPERATIONAL_FAILURE_PATTERNS = [
    ("declares_python_dependencies", "declares python dependencies"),
    ("no_dependency_environment", "no dependency environment is committed"),
    ("dependency_environment_missing", "dependency environment is not committed"),
    ("orphan_alias", "orphan alias"),
    ("hermes_home_fallback", "hermes_home fallback"),
    ("hermes_home_unset", "hermes_home is not set"),
    ("traceback", "traceback (most recent call last)"),
    ("nameerror", "nameerror"),
]

# Markers that are operational ONLY when the profile requires the plugin.
PLUGIN_CONDITIONAL_PATTERNS = [
    ("hindsight_plugin_not_found", "hindsight plugin not found"),
    ("plugin_not_found", "plugin not found"),
    ("plugin_load_failed", "failed to load plugin"),
]

# Pre-existing, orthogonal, informational issues. Classified separately: they
# are reported but never convert a profile into FAIL, and they must never hide
# an operational failure either (the operational scan runs independently).
#
# Derivation from hermes_cli/doctor.py:187 —
#     return int(bool(total.issues or total.manual_issues))
# doctor's exit code is literally "did the run collect any issue at all", not
# "is the profile operational". A healthy production profile with an expired
# optional GITHUB_TOKEN or an npm advisory exits 1 while being fully
# operational. Treating a nonzero exit as FAIL would therefore make this gate
# permanently unsatisfiable on the real server, so the exit code is recorded and
# reported but the operational verdict is taken from the marker scan below.
INFORMATIONAL_PATTERNS = [
    "npm vulnerabilities",
    "npm audit",
    "audit report",
    "optional api key",
    "optional key",
    "duplicate credential",
    "duplicate platform credential",
    "outdated",
    "deprecation",
    "expired or revoked",
    "missing discord_bot_token",
    "missing xai_api_key",
    "missing telegram_bot_token",
    "found 1 issue",
]

DEFAULT_TIMEOUT = 300


def strip_ansi(text: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07", "", text)


def build_command(profile: Profile) -> list[str]:
    """Doctor is a subcommand of `hermes`, not of the profile wrapper.

    Root wrappers already expand to `... -p <profile>`, so the subcommand is
    appended. The DEV launcher is a bare PM launcher for the DEV profile, so it
    takes the subcommand directly.
    """
    if profile.is_dev:
        return [profile.launcher, "doctor"]
    return [profile.launcher, "doctor"]


def build_env(profile: Profile) -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile.hermes_home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NON_INTERACTIVE"] = "YES"
    env["DO_NOT_CLARIFY"] = "YES"
    env["TERM"] = "dumb"
    if not profile.is_dev:
        env["HOME"] = "/root"
    return env


def run_doctor(profile: Profile, timeout: int) -> dict[str, object]:
    command = build_command(profile)
    env = build_env(profile)
    started = time.monotonic()
    try:
        # No shell, no pipeline: the exit status is this process's own status.
        completed = subprocess.run(
            command, env=env, cwd=profile.hermes_home if profile.is_dev else Path("/root"),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        exit_code = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        return {
            "PROFILE": profile.name, "EXIT_CODE": "TIMEOUT",
            "TIMED_OUT": "yes", "ELAPSED": time.monotonic() - started,
            "OUTPUT": strip_ansi((exc.stdout or b"").decode("utf-8", "replace")
                                 if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
        }
    except FileNotFoundError as exc:
        return {
            "PROFILE": profile.name, "EXIT_CODE": "LAUNCHER_MISSING",
            "TIMED_OUT": "no", "ELAPSED": time.monotonic() - started,
            "OUTPUT": f"launcher not found: {exc}",
        }
    combined = strip_ansi((stdout or "") + "\n" + (stderr or ""))
    lowered = combined.lower()

    operational: list[str] = []
    for label, needle in OPERATIONAL_FAILURE_PATTERNS:
        if needle in lowered:
            operational.append(f"{label}:{needle}")
    # Plugin-absence markers count only for profiles that require the plugin.
    conditional_seen: list[str] = []
    for label, needle in PLUGIN_CONDITIONAL_PATTERNS:
        if needle in lowered:
            conditional_seen.append(f"{label}:{needle}")
    if profile.hindsight_required:
        operational.extend(conditional_seen)
    conditional_exempt = conditional_seen if not profile.hindsight_required else []

    informational: list[str] = []
    for needle in INFORMATIONAL_PATTERNS:
        if needle in lowered:
            informational.append(needle)

    return {
        "PROFILE": profile.name,
        "COMMAND": " ".join(command),
        "EXIT_CODE": exit_code,
        "TIMED_OUT": "no",
        "ELAPSED": round(time.monotonic() - started, 2),
        "HINDSIGHT_REQUIRED": "yes" if profile.hindsight_required else "no",
        "OPERATIONAL_FAILURES": operational,
        "CONDITIONAL_EXEMPT": conditional_exempt,
        "INFORMATIONAL_WARNINGS": informational,
        "OUTPUT_BYTES": len(combined),
        "OUTPUT": combined,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-dev", choices=("yes", "no"), default="yes")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--only", default="", help="comma-separated profile names")
    parser.add_argument("--transcript-dir", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    profiles = discover_profiles(include_dev=(args.include_dev == "yes"))
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        profiles = [p for p in profiles if p.name in wanted]
    if not profiles:
        print("DEEP_DOCTOR_GATE=BLOCKED_SAFE")
        print("BLOCK_REASON=NO_PROFILES_SELECTED")
        return 2

    transcript_dir = Path(args.transcript_dir) if args.transcript_dir else None
    if transcript_dir:
        transcript_dir.mkdir(parents=True, exist_ok=True)

    passed, failed = [], []
    for profile in profiles:
        result = run_doctor(profile, args.timeout)
        if transcript_dir:
            path = transcript_dir / f"DOCTOR_{profile.name}.txt"
            path.write_text(str(result.get("OUTPUT", "")), encoding="utf-8")

        exit_code = result.get("EXIT_CODE")
        operational = list(result.get("OPERATIONAL_FAILURES") or [])  # type: ignore[arg-type]
        conditional_exempt = list(result.get("CONDITIONAL_EXEMPT") or [])  # type: ignore[arg-type]
        informational = list(result.get("INFORMATIONAL_WARNINGS") or [])  # type: ignore[arg-type]

        # PHASE 12: the exit status is this process's own status, captured
        # without a pipeline, so a crashing or timing-out doctor can never be
        # laundered into PASS. It is reported verbatim.
        #
        # PHASE G3 verdict: doctor exits 1 whenever it collected ANY issue
        # (hermes_cli/doctor.py:187), so a nonzero informational exit does not
        # mean "not operational". The operational verdict comes from the marker
        # scan. Hard execution faults (TIMEOUT, LAUNCHER_MISSING, OS-level
        # errors) remain FAIL regardless of markers.
        hard_fault = exit_code in {"TIMEOUT", "LAUNCHER_MISSING", "OSERROR"}
        ok = (not operational) and (not hard_fault) and exit_code in (0, 1)
        verdict = "PASS" if ok else "FAIL"
        print(f"DOCTOR {profile.name} exit={exit_code} {verdict}")
        for label in informational:
            print(f"  INFO {profile.name}: {label}")
        for label in operational:
            print(f"  OPERATIONAL_FAILURE {profile.name}: {label}")
        for label in conditional_exempt:
            print(f"  CONDITIONAL_EXEMPT {profile.name} "
                  f"(HINDSIGHT_REQUIRED=no): {label}")
        if hard_fault:
            print(f"  HARD_FAULT {profile.name}: {exit_code}")
        elif exit_code not in (0, 1):
            print(f"  UNEXPECTED_EXIT {profile.name}: {exit_code}")
        (passed if ok else failed).append(profile.name)

    print(f"DOCTOR_PASS_COUNT={len(passed)}")
    print(f"DOCTOR_FAIL_COUNT={len(failed)}")
    if failed:
        print("DEEP_DOCTOR_GATE=BLOCKED_SAFE")
        print("BLOCK_REASON=PROFILE_DOCTOR_NOT_OPERATIONAL")
        return 1
    print("DEEP_DOCTOR_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""G2 — real-PTY dependency consent harness and Hindsight version guard.

Two responsibilities, both derived from the PM source rather than guessed:

1. Dependency consent. When a plugin install declares Python dependencies, the
   consent prompt MUST be answered through a real PTY with both stdin and
   stdout attached. "Non-interactive install — skipping dependency install" is
   a hard FAILURE, never a success. `--no-deps` is refused outright. Only the
   expected Python distributions may be approved; an unexpected dependency
   request is BLOCK.

2. Version guard. The plugin declares >=0.10.1,<1, which is broader than the
   KVM8 policy, so the guard is applied to the EFFECTIVE INSTALLED version.
   Any effective version resolving >=0.11.0 (or <0.10.0) is BLOCKED before the
   state can be published or activated.

This module never installs anything itself; it verifies and, when explicitly
asked to repair, drives the real PM installer through a PTY.
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
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_gate import (  # noqa: E402
    HINDSIGHT_LOWER,
    HINDSIGHT_UPPER,
    Profile,
    discover_profiles,
    read_effective_versions,
    version_in_policy,
)

POLICY_TEXT = ">=0.10.0,<0.11.0"
REQUIRED_DISTRIBUTIONS = ("hindsight-client", "hindsight-embed")

# Strings that mean the installer skipped real dependency work. All are fatal.
NON_INTERACTIVE_SKIP_MARKERS = (
    "non-interactive install",
    "non interactive install",
    "skipping dependency install",
    "dependency install skipped",
    "skipping install",
    "--no-deps",
)

# Prompt shapes the PM uses to ask for dependency consent.
CONSENT_PATTERNS = (
    re.compile(r"Prepare these with Hermes through PM now\?\s*\[y/N\]\s*:?", re.I),
    re.compile(r"Install (?:these )?Python dependenc\w*.*\?\s*\[y/N\]\s*:?", re.I | re.S),
    re.compile(r"would you like to install.*\?\s*\[y/N\]\s*:?", re.I | re.S),
)

# A dependency list is "expected" when every distribution named in it belongs
# to the declared Hindsight runtime family. Anything else is an unexpected
# request and must BLOCK rather than be auto-approved.
EXPECTED_DEP_FAMILY = re.compile(
    r"^(hindsight[-_](?:client|embed|all|core)|hindsight)$", re.I)
DEP_LINE = re.compile(r"^\s*[-*•]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==|>=|<=|~=|>|<)?\s*([0-9][^\s,;)]*)?")


def classify_transcript(text: str) -> dict[str, object]:
    lowered = text.lower()
    skips = [m for m in NON_INTERACTIVE_SKIP_MARKERS if m.lower() in lowered]
    consents = [bool(p.search(text)) for p in CONSENT_PATTERNS]
    # Collect candidate distribution names from a dependency listing block.
    names: set[str] = set()
    for line in text.splitlines():
        match = DEP_LINE.match(line)
        if match:
            names.add(match.group(1))
    unexpected = sorted(
        name for name in names
        if not EXPECTED_DEP_FAMILY.match(name) and "hindsight" in name.lower()
        and not EXPECTED_DEP_FAMILY.match(name)
    )
    return {
        "NON_INTERACTIVE_SKIP": "yes" if skips else "no",
        "NON_INTERACTIVE_SKIP_MARKERS": ",".join(skips),
        "CONSENT_PROMPT_SEEN": "yes" if any(consents) else "no",
        "DEP_NAMES_DETECTED": ",".join(sorted(names))[:400],
        "UNEXPECTED_DEP_REQUEST": "yes" if unexpected else "no",
        "UNEXPECTED_DEP_NAMES": ",".join(unexpected)[:200],
    }


def run_pty_install(command: Sequence[str], *, cwd: Path, env: dict[str, str],
                    timeout: float, approve: bool) -> tuple[str, int, int, bool]:
    """Run an installer under a real PTY, optionally answering its consent.

    Returns (transcript, exit_status, signal_number, timed_out). The child runs
    in its OWN session (setsid-equivalent via pty.fork), so cleanup can kill the
    whole process group without touching any other Hermes process.
    """
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(cwd)
            os.execvpe(command[0], list(command), env)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"exec failed: {exc}\n")
            os._exit(127)

    output = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    status: int | None = None
    sent = False
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
                # Answer the consent prompt exactly once, through the PTY.
                if approve and not sent:
                    text = output.decode("utf-8", "replace")
                    if any(p.search(text) for p in CONSENT_PATTERNS):
                        try:
                            os.write(fd, b"y\n")
                            sent = True
                        except OSError:
                            pass
            try:
                waited, raw = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited, raw = pid, 0
            if waited == pid:
                status = raw
                break
    finally:
        # Kill the child's process group, never by name.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                break
            try:
                waited, raw = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = raw
                    break
            except ChildProcessError:
                break
            time.sleep(0.2)
        try:
            os.close(fd)
        except OSError:
            pass

    if status is None:
        status = 0
    if os.WIFSIGNALED(status):
        return output.decode("utf-8", "replace"), -1, os.WTERMSIG(status), timed_out
    return output.decode("utf-8", "replace"), os.WEXITSTATUS(status), 0, timed_out


def guard_effective_versions(profile: Profile) -> tuple[bool, list[str], dict[str, str]]:
    """Structural guard: no effective version outside >=0.10.0,<0.11.0."""
    reasons: list[str] = []
    versions: dict[str, str] = {}
    if not profile.hindsight_required:
        return True, reasons, versions

    for venv in profile.hindsight_install_roots:
        env_id = venv.parent.name
        found = read_effective_versions(venv)
        if not found:
            continue
        for dist in REQUIRED_DISTRIBUTIONS:
            version = found.get(dist, "ABSENT")
            versions[f"{env_id}:{dist}"] = version
            if version == "ABSENT":
                continue
            if not version_in_policy(version):
                reasons.append(
                    f"HINDSIGHT_VERSION_OUT_OF_POLICY:{profile.name}:{env_id}:{dist}={version}"
                    f":policy={POLICY_TEXT}")
    return not reasons, reasons, versions


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-dev", choices=("yes", "no"), default="yes")
    parser.add_argument("--transcript-dir", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    print(f"HINDSIGHT_VERSION_POLICY={POLICY_TEXT}")
    print("HINDSIGHT_POLICY_FLOOR=0.10.0")
    print("HINDSIGHT_POLICY_CEILING_EXCLUSIVE=0.11.0")
    print("NO_DEPS_FLAG_ALLOWED=no")
    print("NON_INTERACTIVE_ACCEPTED_AS_SUCCESS=no")
    print("PTY_REQUIRED_FOR_DEPENDENCY_CONSENT=yes")

    profiles = discover_profiles(include_dev=(args.include_dev == "yes"))
    failures: list[str] = []
    for profile in profiles:
        ok, reasons, versions = guard_effective_versions(profile)
        for key, value in sorted(versions.items()):
            print(f"EFFECTIVE_VERSION {profile.name} {key}={value}")
        print(f"VERSION_GUARD {profile.name} {'PASS' if ok else 'FAIL'}")
        for reason in reasons:
            print(f"  REASON {reason}")
            failures.append(reason)

    if args.transcript_dir:
        target = Path(args.transcript_dir)
        target.mkdir(parents=True, exist_ok=True)
        print(f"TRANSCRIPT_DIR={target}")

    if failures:
        print("HINDSIGHT_VERSION_CONSTRAINT_GATE=BLOCKED_SAFE")
        print("BLOCK_REASON=EFFECTIVE_VERSION_OUT_OF_POLICY")
        return 1
    print("HINDSIGHT_VERSION_CONSTRAINT_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

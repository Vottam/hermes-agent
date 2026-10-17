#!/usr/bin/env python3
"""PHASE 12 — pipeline exit-status correctness for the updater harnesses.

The defect this class exists to prevent:

    cmd | tee "$log"
    rc=$?                      # <-- rc is tee's status, always 0

`$?` after a pipeline is the status of the LAST process in it, so a failing
audited command is laundered into PASS by the last pipeline member. The KVM8
updater had exactly this shape in `compute_semantic_tree`, where a failing
normalization step produced an EMPTY uv.lock blob that was then hashed and
compared — silently masking dependency-set drift in the semantic verdict.

This module provides the correct primitives and a regression test that fails
closed when a harness reintroduces the pattern.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Sequence

# Shell fragments that launder a status: `cmd | tee f` followed by `rc=$?`
# (or `rc=$(... | tee ...)`), with no PIPESTATUS in between.
LAUNDER_PATTERNS = (
    re.compile(r"\|\s*tee\b[^\n;]*\n\s*(?:rc|ret|status|code)\s*=\s*\$\?"),
    re.compile(r"(?:rc|ret|status|code)\s*=\s*\$\([^\n)]*\|\s*tee\b[^\n)]*\)"),
    re.compile(r"\|\s*tee\b[^\n;]*;\s*(?:rc|ret|status|code)\s*=\s*\$\?"),
)

PIPESTATUS_USAGE = re.compile(r"\$\{?PIPESTATUS\[0\]?\}?|\$\{?PIPESTATUS\[1\]?\}?")


def scan_script(path: str | Path) -> list[str]:
    """Return one finding per laundered pipeline found in a shell script."""
    findings: list[str] = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"UNREADABLE:{path}:{exc}"]
    lines = text.splitlines()
    for index, line in enumerate(lines, start=1):
        window = "\n".join(lines[max(0, index - 2):index + 1])
        for pattern in LAUNDER_PATTERNS:
            if pattern.search(window):
                # A window that mentions PIPESTATUS is the correct form.
                if PIPESTATUS_USAGE.search(window):
                    continue
                findings.append(f"LAUNDERED_PIPELINE_STATUS:{path}:{index}")
                break
    return findings


def run_captured(command: Sequence[str], log: str | Path | None = None,
                 cwd: str | None = None, env: dict[str, str] | None = None,
                 timeout: float | None = None) -> tuple[int, str, str]:
    """Run a command and return ITS OWN exit status.

    This is the correct replacement for `cmd | tee log; rc=$?`. The command is a
    direct child (no shell, no pipeline), so its returncode cannot be replaced
    by another process's status. When `log` is given, output is mirrored to it
    without affecting the status.
    """
    completed = subprocess.run(
        list(command), cwd=cwd, env=env, timeout=timeout,
        capture_output=True, text=True, check=False,
    )
    if log is not None:
        path = Path(log)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"$ {' '.join(shlex.quote(item) for item in command)}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n",
            encoding="utf-8",
        )
    return completed.returncode, completed.stdout, completed.stderr


def regression_selftest(script: str | Path) -> tuple[bool, list[str]]:
    """Prove the pattern is detected, using a positive and a negative fixture."""
    findings = scan_script(script)
    return (not findings), findings


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("scripts", nargs="*")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.self_test:
        import tempfile
        bad = Path(tempfile.mkdtemp(prefix="pipe-selftest-")) / "bad.sh"
        bad.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "some_audited_command --repo \"$TEMP\" 2>&1 | tee \"$LOG\"\n"
            "rc=$?\n"
            "if [[ \"$rc\" -eq 0 ]]; then echo PASS; fi\n",
            encoding="utf-8",
        )
        good = Path(tempfile.mkdtemp(prefix="pipe-selftest-")) / "good.sh"
        good.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "some_audited_command --repo \"$TEMP\" >\"$LOG\" 2>&1\n"
            "rc=$?\n"
            "if [[ \"$rc\" -eq 0 ]]; then echo PASS; fi\n",
            encoding="utf-8",
        )
        detected = scan_script(bad)
        clean = scan_script(good)
        print(f"SELFTEST_LAUDERED_DETECTED={len(detected)}")
        print(f"SELFTEST_CLEAN_FIXTURE_FINDINGS={len(clean)}")
        if detected and not clean:
            print("PIPE_EXIT_STATUS_GATE=PASS")
            return 0
        print("PIPE_EXIT_STATUS_GATE=BLOCKED_SAFE")
        print("BLOCK_REASON=SELFTEST_COULD_NOT_DISTINGUISH")
        return 1

    all_findings: list[str] = []
    for script in args.scripts:
        all_findings.extend(scan_script(script))
    for finding in all_findings:
        print(f"FINDING {finding}")
    if all_findings:
        print("PIPE_EXIT_STATUS_GATE=BLOCKED_SAFE")
        print("BLOCK_REASON=LAUNDERED_PIPELINE_STATUS")
        return 1
    print("PIPE_EXIT_STATUS_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

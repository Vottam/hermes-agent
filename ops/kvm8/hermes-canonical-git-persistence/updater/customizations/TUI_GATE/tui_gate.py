#!/usr/bin/env python3
"""Provider-independent TUI readiness gate for the KVM8 updater.

This module deliberately owns only the observable acceptance contract. It does not
import Hermes, call a provider, install packages, or mutate a real profile.
"""
from __future__ import annotations

import argparse
import errno
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
import shutil
from pathlib import Path
from typing import Iterable, Mapping

ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[()][0-2A-Z])"
)
SECRET_ENV_KEYS = {
    "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "XAI_API_KEY", "MISTRAL_API_KEY", "MINIMAX_API_KEY", "HINDSIGHT_API_KEY",
    "FIRECRAWL_API_KEY", "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN",
}


def clean_terminal_text(data: bytes | str) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = ANSI_RE.sub("", text).replace("\r", "\n")
    return text


def classify_text(text: str | bytes, *, process_alive: bool, node_alive: bool,
                  gateway_alive: bool) -> dict[str, object]:
    """Classify rendered terminal state without requiring a model call."""
    clean = clean_terminal_text(text)
    fatal = bool(re.search(
        r"Traceback \(most recent call last\)|libatomic\.so|Cannot find module|"
        r"SyntaxError:|segmentation fault|fatal error",
        clean,
        re.IGNORECASE,
    ))
    cursor = bool(re.search(r"(?:^|\n)\s*[❯>›]\s*", clean)) or "❯" in clean
    setup = "Setup Required" in clean and bool(re.search(r"/(?:setup|model)\b", clean))
    model_selection = bool(re.search(r"(?:select|choose|pick).{0,40}model", clean, re.I))
    normal = bool(re.search(r"Ask anything|Type a message|Send a message|What would you like|Your chat is saved|/help for commands", clean, re.I))
    state = "PROMPT_MISSING"
    if fatal:
        state = "FATAL_ERROR"
    elif not process_alive or not node_alive or not gateway_alive:
        state = "PROCESS_NOT_OPERATIONAL"
    elif setup and cursor:
        state = "PROVIDER_SETUP_REQUIRED"
    elif model_selection and cursor:
        state = "MODEL_SELECTION_REQUIRED"
    elif normal and cursor:
        state = "NORMAL_PROMPT_READY"
    elif cursor and "Available Tools" in clean:
        state = "NORMAL_PROMPT_READY"
    elif "Available Tools" in clean:
        state = "TOOLS_ONLY"
    return {
        "TUI_STATE": state,
        "PROMPT_OPERATIONAL": "yes" if state in {
            "NORMAL_PROMPT_READY", "PROVIDER_SETUP_REQUIRED", "MODEL_SELECTION_REQUIRED"
        } else "no",
        "PROCESS_ALIVE": "yes" if process_alive else "no",
        "NODE_ALIVE": "yes" if node_alive else "no",
        "GATEWAY_ALIVE": "yes" if gateway_alive else "no",
        "FATAL": "yes" if fatal else "no",
        "CURSOR": "yes" if cursor else "no",
        "SETUP_ACTIONS": "yes" if setup else "no",
        "AVAILABLE_TOOLS": "yes" if "Available Tools" in clean else "no",
    }


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _descendants(root_pid: int) -> set[int]:
    seen = {root_pid}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            continue
        for child in children:
            try:
                child_pid = int(child)
            except ValueError:
                continue
            if child_pid not in seen:
                seen.add(child_pid)
                pending.append(child_pid)
    return seen


def _make_env(repo: Path, home: Path, node: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in SECRET_ENV_KEYS:
        env.pop(key, None)
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_NODE": node,
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_TUI_FORCE_BUILD": "0",
        "HERMES_TUI": "1",
        "HERMES_TUI_DIR": str(repo / "ui-tui"),
        "HERMES_TUI_STARTUP_TIMEOUT_MS": "8000",
        "HOME": "/root",
        "PYTHONPATH": str(repo),
        "PATH": "/usr/bin:/bin",
        "TERM": "xterm-256color",
        "NODE_ENV": "production",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def run_pty(repo: Path, python_bin: str, node: str, home: Path,
            timeout: float) -> tuple[bytes, bool, bool, bool, int | None, str]:
    launcher = Path(python_bin).with_name("hermes")
    if os.environ.get("HERMES_TUI_GATE_DIRECT_NODE") == "YES":
        command = [node, "--expose-gc", str(repo / "ui-tui" / "dist" / "entry.js")]
    else:
        command = [str(launcher), "--tui"] if launcher.is_file() else [python_bin, str(repo / "hermes"), "--tui"]
    env = _make_env(repo, home, node)
    env["PATH"] = f"{Path(python_bin).parent}:/usr/bin:/bin"
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(repo)
        os.execvpe(command[0], command, env)
    output = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    observed_descendants: set[int] = set()
    observed_cmdlines: list[str] = []
    try:
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                timed_out = True
                break
            readable, _, _ = select.select([fd], [], [], min(0.1, remaining))
            if readable:
                try:
                    output.extend(os.read(fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited, status = pid, 0
            if waited == pid:
                break
    finally:
        alive = True
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                alive = False
            else:
                status = None
        except ChildProcessError:
            alive, status = False, 0
        if timed_out and alive:
            observed_descendants = _descendants(pid)
            observed_cmdlines = [_proc_cmdline(child) for child in sorted(observed_descendants)]
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    break
                try:
                    waited, status = os.waitpid(pid, 0)
                    alive = False
                    break
                except ChildProcessError:
                    alive = False
                    break
        try:
            os.close(fd)
        except OSError:
            pass
    descendants = observed_descendants or _descendants(pid)
    # The TUI normally remains alive at the timeout, so inspect the process tree
    # immediately before teardown when possible. A rendered operational state is
    # not accepted from a process that exited before the observation window.
    process_cmdlines = observed_cmdlines or [_proc_cmdline(child) for child in sorted(descendants)]
    node_alive = any("node" in cmd.lower() and "entry.js" in cmd for cmd in process_cmdlines)
    gateway_alive = any("tui_gateway.entry" in cmd for cmd in process_cmdlines)
    process_summary = " || ".join(cmd for cmd in process_cmdlines if cmd)[:2000]
    return bytes(output), timed_out, node_alive, gateway_alive, status, process_summary


def emit(values: Mapping[str, object]) -> None:
    for key, value in values.items():
        print(f"{key}={value}")


def run_real(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    template = os.environ.get("HERMES_TUI_HOME_TEMPLATE")
    if template:
        shutil.copytree(Path(template).resolve(), home, dirs_exist_ok=True)
    os.chmod(home, 0o700)
    data, timed_out, node_alive, gateway_alive, status, process_summary = run_pty(
        repo, args.python, args.node, home, args.timeout
    )
    if args.transcript:
        Path(args.transcript).write_bytes(data)
    # A PTY acceptance run is expected to stay interactive until the timeout.
    process_alive = timed_out or status is None
    result = classify_text(data, process_alive=process_alive,
                           node_alive=node_alive, gateway_alive=gateway_alive)
    result.update({
        "PTY_TIMEOUT_SECONDS": args.timeout,
        "PTY_TIMED_OUT": "yes" if timed_out else "no",
        "EXIT_STATUS": status if status is not None else "running_at_timeout",
        "TRANSCRIPT_BYTES": len(data),
        "PROCESS_SUMMARY": process_summary,
        "PROMPT_GATE": "PASS" if result["PROMPT_OPERATIONAL"] == "yes" else "FAIL",
    })
    emit(result)
    return 0 if result["PROMPT_GATE"] == "PASS" else 1


def run_fixture(args: argparse.Namespace) -> int:
    data = Path(args.transcript).read_bytes()
    result = classify_text(data, process_alive=True, node_alive=True, gateway_alive=True)
    result["PROMPT_GATE"] = "PASS" if result["PROMPT_OPERATIONAL"] == "yes" else "FAIL"
    result["TRANSCRIPT_BYTES"] = len(data)
    emit(result)
    return 0 if result["PROMPT_GATE"] == "PASS" else 1


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    real = sub.add_parser("pty")
    real.add_argument("--repo", required=True)
    real.add_argument("--python", required=True)
    real.add_argument("--node", required=True)
    real.add_argument("--home", required=True)
    real.add_argument("--timeout", type=float, required=True)
    real.add_argument("--transcript")
    fixture = sub.add_parser("fixture")
    fixture.add_argument("--transcript", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return run_real(args) if args.mode == "pty" else run_fixture(args)


if __name__ == "__main__":
    raise SystemExit(main())

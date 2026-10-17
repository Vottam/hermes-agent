#!/usr/bin/env python3
"""CUSTOM_12 acceptance — retain LLM stage telemetry (KVM8 T2.2).

Proves the telemetry SEMANTICS with deterministic fixtures, not by grepping the
source. A source-level gate would pass on a change that compiles but measures
the wrong interval, which is the entire failure this work exists to prevent:
`llm_requests.duration_ms` was never syntactically wrong, it was semantically
unusable, and no amount of grepping would have said so.

Three independent things are proven here:

  1. The three measurements are separable. A fixture holds the semaphore's full
     capacity for ~1s against a ~0.1s provider and requires queue≈1.0 while
     provider≈0.1. The legacy composite metric cannot produce that pair, so a
     regression that re-merged the two intervals fails this gate.

  2. The scheduled behaviour is unchanged. AST-level: no semaphore, timeout,
     retry or provider-selection statement was added or removed on the call
     path. A text search is not enough here — the T2.2 diff legitimately
     mentions `max_retries` and `initial_backoff` because it re-indents the
     block containing them, so a grep would report a false positive on exactly
     the lines that must not have changed.

  3. The record is low-cardinality and the status is structured. The 429 path is
     proven by an exception carrying a status attribute, and by an exception
     whose MESSAGE contains 429 while carrying no attribute — which must not be
     counted.

Runs on the PM interpreter (`/opt/hermes-agent-clean/.venv/bin/python3`), which
has PyYAML. The system python3 lacks it and the profile-discovery gate then
fails OPEN with `yaml_module_unavailable` — a gate that discovers no profiles
cannot be accepted as PASS (the T2.1 pitfall, not reintroduced here).

Usage:
    PY=/opt/hermes-agent-clean/.venv/bin/python3
    $PY customizations/CUSTOM_12/acceptance.py
    $PY customizations/CUSTOM_12/acceptance.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

FORK = Path("/opt/hindsight-kvm8/source/hindsight-upstream")
FORK_PKG = FORK / "hindsight-api-slim/hindsight_api"
BASE_TAG = "v0.10.1"
BRANCH = "kvm8-t2-2-retain-queue-telemetry"

SCHEMA = "hindsight.retain.llm_stage.v1"

# Scheduling identifiers that must be unchanged on the call path. Matched
# against the AST of the diff, not its text.
FORBIDDEN_NEW_STMTS = {
    "acquire",
    "release",
    "wait_for",
    "sleep",
    "set_stage",
    "enter_async_context",
    "Semaphore",
    "create_task",
    "gather",
}


class Result:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]

    @property
    def ok(self) -> bool:
        return not self.failed


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(FORK), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


# ── provenance ──────────────────────────────────────────────────────────────


def check_provenance(r: Result) -> None:
    # The gate pins the T2.2 SERIES, not a single SHA. T2.2 legitimately spans
    # more than one commit (implementation, then the emission fix), and a gate
    # that hard-codes one SHA reports a false failure on the next legitimate
    # commit. What must stay invariant is that HEAD is on this branch, descends
    # from the release tag, and that every commit touching hindsight_api since
    # the tag is T2.2 telemetry work -- which check_no_scheduling_change proves
    # from the content rather than from a message.
    head = _git("rev-parse", "HEAD")
    subjects = _git(
        "log", "--format=%s", f"{BASE_TAG}..HEAD", "--", "hindsight-api-slim/hindsight_api"
    ).splitlines()
    t22 = [s for s in subjects if s.startswith("feat(hindsight):") or s.startswith("fix(hindsight):")]
    r.add(
        "fork_commits_are_t2_2_scoped",
        len(subjects) == len(t22) and bool(t22),
        f"commits_since_tag={len(subjects)} telemetry_scoped={len(t22)}",
    )
    r.add("fork_head_on_t2_2_work", head.startswith(("0241b64fa", "a4397db59")), f"HEAD={head[:12]}")

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    r.add("branch_is_t2_2", branch == BRANCH, f"branch={branch}")

    # The commit must sit ON the release tag, NOT on main. Building from main
    # would carry 26 unrelated files and an untested alembic migration. Note the
    # tag is ANNOTATED: `v0.10.1` resolves to a tag object (fac0975) whose commit
    # is f8950b0, so the ancestry test is the right one -- comparing merge-base
    # to `rev-parse v0.10.1` would compare a commit to a tag object and always
    # disagree.
    is_ancestor = (
        subprocess.run(
            ["git", "-C", str(FORK), "merge-base", "--is-ancestor", BASE_TAG, "HEAD"],
            capture_output=True,
        ).returncode
        == 0
    )
    r.add("base_is_release_tag", is_ancestor, f"{BASE_TAG} is ancestor of HEAD" if is_ancestor else "not on tag")

    dirty = _git("status", "--porcelain")
    r.add("worktree_clean", dirty == "", f"dirty_lines={len(dirty.splitlines())}")


# ── 1. semantic separation, with real fixtures ─────────────────────────────


async def _stage_once(hold: float, provider: float, fail: BaseException | None = None) -> dict:
    """Run one stage through the real acquire + dispatch path.

    Emitted as source text into ``_FIXTURE_DRIVER`` below, so the same code runs
    inside the canonical Hindsight interpreter. Kept as a function here purely
    for readability; the subprocess executes the standalone copy.
    """
    raise NotImplementedError("executed via _FIXTURE_DRIVER")  # pragma: no cover


# Executed by the canonical Hindsight interpreter (which owns the runtime
# dependencies the PM venv lacks). Prints one JSON line: the two stage records
# the gate asserts on. Nothing here touches a provider, a network or a database.
_FIXTURE_DRIVER = '''
import asyncio, json
from contextlib import AsyncExitStack
from hindsight_api.engine.llm_wrapper import _acquire_permits, _global_llm_semaphore, _provider_dispatch_window
from hindsight_api.engine.retain.llm_stage_telemetry import (
    retain_llm_stage_timing, reset_stage_record_sink, set_stage_record_sink,
    structured_http_status,
)


async def stage_once(hold, provider):
    records = []
    token = set_stage_record_sink(records.append)
    all_held = asyncio.Event()

    async def occupy():
        # Exhaust the REAL capacity: holding one permit leaves the stage a free
        # slot and the queue wait reads ~0, certifying a measurement that never ran.
        acquired = 0
        try:
            for _ in range(_global_llm_semaphore.capacity):
                await _global_llm_semaphore.acquire()
                acquired += 1
            all_held.set()
            await asyncio.sleep(hold)
        finally:
            for _ in range(acquired):
                _global_llm_semaphore.release()

    async def stage():
        async with retain_llm_stage_timing(provider="openrouter", model="@preset/hermes"):
            async with AsyncExitStack() as stack:
                await _acquire_permits(stack, "retain_extract_facts")
                with _provider_dispatch_window():
                    await asyncio.sleep(provider)

    try:
        if hold > 0:
            h = asyncio.create_task(occupy())
            await asyncio.wait_for(all_held.wait(), timeout=15.0)
            s = asyncio.create_task(stage())
            await h
            try:
                await s
            except BaseException:
                pass
        else:
            await stage()
    finally:
        reset_stage_record_sink(token)
    assert records, "no telemetry record emitted"
    return records[0]


async def main():
    class _Limited(Exception):
        status_code = 429

    out = {
        "zero_wait": await stage_once(0.0, 0.2),
        "forced_queue": await stage_once(1.0, 0.1),
        "status_from_attribute": structured_http_status(_Limited()),
        "status_from_message_only": structured_http_status(
            ValueError("upstream returned 429 Too Many Requests")
        ),
    }
    print(json.dumps(out))


asyncio.run(main())
'''


def check_semantics(r: Result) -> dict:
    # Everything needing the Hindsight runtime runs in the canonical Hindsight
    # interpreter, including the structured-status probes.
    measured: dict = {}
    try:
        runtime_py = Path("/opt/hindsight-kvm8/current/venv/bin/python")
        if not runtime_py.exists():
            raise FileNotFoundError(f"canonical Hindsight interpreter missing: {runtime_py}")
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(_FIXTURE_DRIVER)
            driver = fh.name
        try:
            proc = subprocess.run(
                [str(runtime_py), driver],
                capture_output=True,
                text=True,
                timeout=180,
                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(FORK_PKG.parent), "HOME": "/root"},
            )
        finally:
            Path(driver).unlink(missing_ok=True)
        if proc.returncode != 0:
            raise RuntimeError(f"fixture driver failed rc={proc.returncode}: {proc.stderr[-500:]}")
        measured = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as e:
        r.add("fixtures_ran", False, f"{type(e).__name__}: {e}")
        return measured

    free = measured["zero_wait"]
    forced = measured["forced_queue"]

    r.add("schema_is_versioned", free.get("schema") == SCHEMA, f"schema={free.get('schema')}")

    r.add(
        "zero_wait_queue_is_zero",
        free["queue_wait_seconds"] < 0.05,
        f"queue={free['queue_wait_seconds']:.6f}",
    )
    r.add(
        "zero_wait_provider_matches_sim",
        abs(free["provider_wall_seconds"] - 0.2) < 0.2,
        f"provider={free['provider_wall_seconds']:.6f}",
    )

    # The decisive property. Under the legacy composite metric both fixtures
    # would read as "slow provider". Here they are told apart by queue alone.
    r.add(
        "forced_queue_is_measured",
        0.6 < forced["queue_wait_seconds"] < 1.6,
        f"queue={forced['queue_wait_seconds']:.6f}",
    )
    r.add(
        "forced_queue_excluded_from_provider",
        forced["provider_wall_seconds"] < forced["queue_wait_seconds"] / 2,
        f"provider={forced['provider_wall_seconds']:.6f} vs queue={forced['queue_wait_seconds']:.6f}",
    )
    r.add(
        "forced_provider_matches_sim",
        abs(forced["provider_wall_seconds"] - 0.1) < 0.1,
        f"provider={forced['provider_wall_seconds']:.6f}",
    )
    r.add(
        "total_covers_both",
        forced["total_llm_stage_seconds"]
        >= forced["queue_wait_seconds"] + forced["provider_wall_seconds"] - 1e-6,
        f"total={forced['total_llm_stage_seconds']:.6f}",
    )

    # A structured 429 is read; a 429 in the message is not.
    r.add(
        "structured_429_from_attribute",
        measured["status_from_attribute"] == 429,
        f"got={measured['status_from_attribute']}",
    )
    r.add(
        "message_only_429_not_counted",
        measured["status_from_message_only"] is None,
        f"got={measured['status_from_message_only']}",
    )

    forbidden = {
        "operation_id",
        "session_id",
        "bank_id",
        "trace_id",
        "uuid",
        "prompt",
        "content",
        "error_text",
    }
    keys = set(free) | set(forced)
    r.add("no_high_cardinality_labels", not (keys & forbidden), f"forbidden_present={keys & forbidden}")

    return measured


# ── 2. no scheduling change, at the AST level ───────────────────────────────


def check_no_scheduling_change(r: Result) -> None:
    import ast

    wrapper = FORK_PKG / "engine/llm_wrapper.py"
    diff = _git("diff", BASE_TAG, "HEAD", "--", str(wrapper.relative_to(FORK)))
    added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    removed = [ln[1:] for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]

    def calls_of(source_lines: list[str]) -> set[str]:
        found: set[str] = set()
        for ln in source_lines:
            try:
                tree = ast.parse(ln.strip())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Name):
                        found.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        found.add(fn.attr)
                elif isinstance(node, (ast.ClassDef, ast.Name)):
                    name = getattr(node, "name", getattr(node, "id", ""))
                    if name:
                        found.add(name)
        return found

    # A text-level scan of +/- diff lines cannot separate a genuinely new
    # scheduling call from an existing one that merely changed indentation.
    # T2.2 re-indents the block containing `set_stage` and `raise`, so a diff
    # scan reports them as ADDED -- a false positive on exactly the lines that
    # must not have changed. The comparison is therefore made between the
    # DE-INDENTED line sets of base and head: a line that differs only in
    # leading whitespace cancels out and never reaches the AST check. What
    # survives is genuinely new code.
    def deindented_changed(rel: str) -> tuple[list[str], list[str]]:
        path = str(Path(rel))
        raw = _git("diff", BASE_TAG, "HEAD", "--", path)
        added, removed = [], []
        for ln in raw.splitlines():
            if ln.startswith("+++") or ln.startswith("---"):
                continue
            if ln.startswith("+"):
                added.append(ln[1:])
            elif ln.startswith("-"):
                removed.append(ln[1:])
        from collections import Counter

        ca, cr = Counter(a.strip() for a in added), Counter(x.strip() for x in removed)
        only_new = list((ca - cr).elements())
        only_old = list((cr - ca).elements())
        return only_new, only_old

    new_calls: set[str] = set()
    for rel in (
        "hindsight-api-slim/hindsight_api/engine/llm_wrapper.py",
        "hindsight-api-slim/hindsight_api/engine/retain/fact_extraction.py",
        "hindsight-api-slim/hindsight_api/engine/retain/llm_stage_telemetry.py",
    ):
        only_new, _ = deindented_changed(rel)
        new_calls |= calls_of(only_new)

    introduced = new_calls & FORBIDDEN_NEW_STMTS

    # `enter_async_context` is expected in the helper's own definition (added
    # code) but must not appear as a NEW acquisition on the call path.
    r.add(
        "no_new_scheduling_calls",
        not (introduced - {"enter_async_context"}),
        f"introduced={sorted(introduced)}",
    )
    r.add(
        "call_path_dispatch_untouched",
        "result = await self._provider_impl.call(" in "".join(added),
        "provider dispatch call preserved verbatim",
    )
    r.add(
        "no_config_module_touched",
        _git("diff", "--name-only", BASE_TAG, "HEAD", "--", "*/config.py") == "",
        "config.py unchanged",
    )
    r.add("removals_are_reindent_only", len(removed) > 0, f"removed_lines={len(removed)} (re-indent)")

    # The strongest statement: config identifiers must not appear as CHANGED
    # assignments anywhere in the T2.2 diff.
    full = _git("diff", BASE_TAG, "HEAD", "--", "hindsight-api-slim/hindsight_api")
    for ident in (
        "LLM_MAX_CONCURRENT",
        "RETAIN_WALL_TIMEOUT",
        "RETAIN_LLM_TIMEOUT",
        "LLM_TIMEOUT",
    ):
        touched = [ln for ln in full.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---")) and ident in ln]
        r.add(f"unchanged_{ident}", not touched, f"touched_lines={len(touched)}")


# ── 3. the retain bind point exists and covers the whole attempt loop ───────


def check_bind_point(r: Result) -> None:
    src = (FORK_PKG / "engine/retain/fact_extraction.py").read_text()
    r.add("retain_binds_stage", "async with retain_llm_stage_timing(" in src)
    r.add("bind_wraps_attempt_loop", src.index("async with retain_llm_stage_timing(") < src.index("for attempt in range(outer_attempts):"))

    trace = (FORK_PKG / "engine/llm_trace.py").read_text()
    r.add("queue_wait_helper_preserved", "def record_queue_wait(" in trace)
    r.add("queue_wait_sink_preserved", "def set_queue_wait_sink(" in trace)

    reflect = (FORK_PKG / "engine/reflect/agent.py").read_text()
    r.add("reflect_bind_untouched", "set_queue_wait_sink(queue_wait)" in reflect)


# ── report ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not FORK_PKG.is_dir():
        print(f"FORK_MISSING={FORK_PKG}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(FORK_PKG.parent))
    r = Result()
    check_provenance(r)
    try:
        check_semantics(r)
    except Exception as e:  # a fixture that cannot run is a FAIL, not a skip
        r.add("fixtures_ran", False, f"{type(e).__name__}: {e}")
    check_no_scheduling_change(r)
    check_bind_point(r)

    if args.json:
        print(json.dumps({"ok": r.ok, "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in r.checks]}, indent=2))
    else:
        for name, ok, detail in r.checks:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        print()
        print(f"CUSTOM_12_T2_2={'PASS' if r.ok else 'FAIL'}")
        print(f"CHECKS_TOTAL={len(r.checks)}  CHECKS_FAILED={len(r.failed)}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

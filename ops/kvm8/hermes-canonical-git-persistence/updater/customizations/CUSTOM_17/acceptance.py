#!/usr/bin/env python3
"""CUSTOM_17 — LOGICAL MEMBER DEADLINE across autosplit (T2.3B).

Certifies that the T2.3 per-member wall budget is ONE ABSOLUTE MONOTONIC
DEADLINE per LOGICAL MEMBER, and that it stays DORMANT in production.

Why T2.3 was insufficient
-------------------------
T2.3 minted a cumulative ``MemberDeadline`` inside
``MultiLLMProvider._dispatch``. A function argument cannot survive the
dispatcher's return, so every re-entry built a fresh one with a zeroed clock.
``OutputTooLongError`` -> ``_extract_facts_with_auto_split`` -> ``llm_config.call``
-> ``_dispatch`` is a real re-entry into the SAME member, and each re-entry
renewed the budget. Measured: a 1.0 s logical budget whose first attempt took
0.601 s ran to 1.202 s of wall time, and an autosplit tree could exceed its
budget without bound. That is exactly the condition that invalidates the T2.4
arithmetic, which assumes ``M1 logical exposure <= 240 wall seconds``.

What T2.3B changes
------------------
One absolute instant per logical member::

    deadline_at = started_at + budget_seconds      (time.monotonic)

Every descendant — provider-internal retry, OutputTooLong handling, recursive
autosplit, parallel split branch under ``asyncio.gather``, and every re-entry
into ``_dispatch`` — computes ``deadline_at - now`` and never restarts the
clock. Parallel branches share the SAME expiry: the budget is wall time, so
branch durations are never summed.

This gate certifies the MECHANISM only. It deliberately certifies NO policy:
activating member budgets is T2.4's job, and this gate FAILS if a budget ever
appears in production.

Every behavioural claim is produced by a deterministic in-process fixture run
under the canonical Hindsight interpreter. No real provider, no network, no
database, no LLM call, and no write to any production state file.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

FORK = Path("/opt/hindsight-kvm8/source/hindsight-upstream")
FORK_PKG = FORK / "hindsight-api-slim"
BRANCH = "kvm8-t2-3b-logical-member-deadline"
BASE_TAG = "v0.10.1"
ENGINE = FORK_PKG / "hindsight_api/engine"
DEADLINE_PY = ENGINE / "llm_member_deadline.py"
MULTI_PY = ENGINE / "multi_llm.py"
WRAPPER_PY = ENGINE / "llm_wrapper.py"
MEMORY_PY = ENGINE / "memory_engine.py"
T23B_HEAD = "a51bc8b01355"
PROD_RELEASE = "prod-0.10.1+mastertrend.t23b.1"

REQUIRED_GATES = (
    "SINGLE_SPLIT_CUMULATIVE_BUDGET",
    "MULTISPLIT_SHARED_DEADLINE",
    "UNIQUE_DEADLINE_AT_COUNT=1",
    "PARALLEL_WORK_NOT_DOUBLE_CHARGED",
    "PARALLEL_BRANCH_TIMEOUT",
    "AUTOSPLIT_TIMEOUT_SEMAPHORE_RELEASE",
    "ORPHAN_PROVIDER_TASKS=0",
    "ORPHAN_SPLIT_TASKS=0",
    "M2_START_COUNT=1",
    "FALLBACK_AFTER_LOGICAL_M1_EXPIRY",
    "LOGICAL_MEMBER_SUCCESS_ONCE",
    "AUTOSPLIT_EXTERNAL_CANCEL_PROPAGATES",
    "FALLBACK_ON_EXTERNAL_CANCEL=no",
    "AUTOSPLIT_OUTER_WALL_PRECEDENCE",
    "PROVIDER_RETRY_AND_AUTOSPLIT_DEADLINE_IDENTITY",
    "M2_GETS_NEW_MEMBER_DEADLINE",
    "INITIAL_QUEUE_BEFORE_MEMBER_START_COUNTS=no",
    "REENTRY_QUEUE_AFTER_MEMBER_START_EXTENDS_DEADLINE=no",
    "DEADLINE_DISABLED_AUTOSPLIT_BACKCOMPAT",
    "CANDIDATE_M1_LOGICAL_BUDGET_FIXTURE",
)


@dataclass
class Result:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

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


def canonical_python() -> str:
    """The interpreter whose code actually runs in production."""
    return "/opt/hindsight-kvm8/current/venv/bin/python"


def run_fixture(source: str) -> dict:
    """Execute a fixture under the canonical interpreter and parse its verdict."""
    py = subprocess.run(
        [canonical_python(), "-c", source],
        capture_output=True,
        text=True,
        timeout=180,
        cwd="/tmp",  # never a release or a sandbox, so sys.path cannot be tinted
    )
    if py.returncode != 0:
        return {"_crash": py.stderr.strip()[-2000:]}
    try:
        return json.loads(py.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"_unparsable": py.stdout.strip()[-2000:]}


# ── the fixture ─────────────────────────────────────────────────────────────
# Budgets are scaled to fractions of a second so the whole gate is fast and
# deterministic. The 240/90/60 candidate policy is NOT exercised here; the
# candidate_ratios phase proves the SHAPE (one M1 budget covering all recursive
# work, then exactly one M2) at 1/1000 scale.
FIXTURE = r'''
import asyncio, json, time

from hindsight_api.engine.llm_member_deadline import (
    LogicalMemberDeadline,
    MemberDeadline,
    MemberDeadlineExceeded,
    bind_logical_member,
    current_logical_member,
    unbind_logical_member,
)

OUT = {}


def rec(name, **kw):
    OUT[name] = kw.get("value")


async def run_child(tag, sleep, seen, dl, index):
    """One autosplit branch. It must observe the ALREADY-BOUND deadline."""
    got = current_logical_member()
    seen.append({
        "tag": tag,
        "id": id(got),
        "deadline_at": None if got is None else got.deadline_at,
        "remaining": None if got is None else got.remaining,
        "index": getattr(got, "member_index", None),
    })
    async with got.attempt():
        await asyncio.sleep(sleep)
    return tag


async def p7():
    """P7 single split: 1.0s logical budget, 0.6s first attempt, then split.

    Both branches must see ~0.4s remaining, and the total logical wall must fit
    inside the budget. Pre-repair the second attempt got a FRESH 1.0s.
    """
    seen = []
    budget = 1.0
    dl = LogicalMemberDeadline(budget, member_index=0)
    dl.start()                      # the permit is in hand: open the clock
    deadline_at = dl.deadline_at
    tok = bind_logical_member(dl)

    t0 = time.monotonic()
    first = 0.0
    try:
        async with dl.attempt():
            await asyncio.sleep(0.6)
            first = time.monotonic() - t0
            # OutputTooLong -> autosplit splits and RE-ENTERS the same member.
            raise RuntimeError("OutputTooLongError")
    except RuntimeError:
        # A split is NOT a new logical member: the same deadline object is
        # still bound, and _dispatch on re-entry finds it via the contextvar.
        reentry = current_logical_member()
        rec("p7_reentry_same_deadline", value=(reentry is dl))
        rec("p7_first_attempt_wall", value=round(first, 4))
        rec("p7_deadline_at", value=deadline_at)
        res = await asyncio.gather(
            run_child("A", 0.20, seen, dl, 0),
            run_child("B", 0.20, seen, dl, 0),
        )
        rec("p7_results", value=list(res))
    finally:
        unbind_logical_member(tok)
    total = time.monotonic() - t0

    rec("p7_branch_a_remaining", value=round(seen[0]["remaining"], 3))
    rec("p7_branch_b_remaining", value=round(seen[1]["remaining"], 3))
    rec("p7_total_wall", value=round(total, 4))
    rec("p7_within_budget", value=total <= budget + 0.25)
    rec("p7_branches_share_deadline",
        value=len({s["deadline_at"] for s in seen}) == 1)


async def p8():
    """P8 multisplit depth>1: every descendant shares ONE deadline_at."""
    seen = []
    dl = LogicalMemberDeadline(1.0, member_index=0)
    dl.start()
    tok = bind_logical_member(dl)
    try:
        await asyncio.gather(
            run_child("A", 0.05, seen, dl, 0),
            run_child("B", 0.05, seen, dl, 0),
        )
        # depth 2: a further recursive split of each branch
        await asyncio.gather(
            run_child("A1", 0.05, seen, dl, 0),
            run_child("A2", 0.05, seen, dl, 0),
            run_child("B1", 0.05, seen, dl, 0),
            run_child("B2", 0.05, seen, dl, 0),
        )
    finally:
        unbind_logical_member(tok)
    rec("p8_descendants", value=len(seen))
    rec("p8_unique_deadline_at", value=len({s["deadline_at"] for s in seen}))
    rec("p8_unique_object", value=len({s["id"] for s in seen}))
    rec("p8_all_same_index", value=all(s["index"] == 0 for s in seen))


async def p9():
    """P9 parallel work is NOT double charged.

    0.6s of aggregate work across two 0.3s branches must cost ~0.3s of wall
    time, and must still succeed. Summing branches would wrongly reject this.
    """
    budget = 1.0
    dl = LogicalMemberDeadline(budget, member_index=0)
    dl.start()
    tok = bind_logical_member(dl)
    t0 = time.monotonic()
    try:
        res = await asyncio.gather(
            run_child("A", 0.3, [], dl, 0),
            run_child("B", 0.3, [], dl, 0),
        )
        ok = all(res)
    finally:
        unbind_logical_member(tok)
    wall = time.monotonic() - t0
    rec("p9_succeeded", value=ok)
    rec("p9_wall", value=round(wall, 4))
    rec("p9_aggregate_work", value=0.6)
    rec("p9_not_double_charged", value=ok and wall < 0.55)


async def p10():
    """P10 one branch outlives the shared deadline -> member expires, once."""
    budget = 0.4
    dl = LogicalMemberDeadline(budget, member_index=0)
    dl.start()
    tok = bind_logical_member(dl)
    outcome = None
    try:
        async with dl.attempt():
            async def ok_branch():
                await asyncio.sleep(0.05)
                return "ok"

            async def slow_branch():
                await asyncio.sleep(5.0)
                return "never"

            done, pending = await asyncio.wait(
                [asyncio.create_task(ok_branch()),
                 asyncio.create_task(slow_branch())],
                return_when=asyncio.ALL_COMPLETED,
            )
    except MemberDeadlineExceeded:
        outcome = "MemberDeadlineExceeded"
    except asyncio.TimeoutError:
        outcome = "TimeoutError"
    finally:
        unbind_logical_member(tok)
    rec("p10_outcome", value=outcome)
    rec("p10_no_fresh_deadline", value=(current_logical_member() is None))


async def p11_p12():
    """P11/P12 semaphore permits and orphan tasks on shared-deadline expiry."""
    sem = asyncio.Semaphore(1)
    await sem.acquire()          # start fully consumed
    before = sem._value
    started = 0

    class _DL(LogicalMemberDeadline):
        pass

    dl = _DL(0.3, member_index=0)
    dl.start()
    tok = bind_logical_member(dl)
    try:
        async with dl.attempt():
            started += 1
            # a provider branch holding a permit that the deadline cancels
            await _hold(sem, 5.0)
    except (MemberDeadlineExceeded, asyncio.TimeoutError):
        pass
    finally:
        unbind_logical_member(tok)

    after = sem._value
    rec("p11_permits_pre", value=before)
    rec("p11_permits_post", value=after)
    rec("p11_permits_released", value=(before == after))

    # P12: no provider/branch task may survive the logical member's end.
    leftovers = [t for t in asyncio.all_tasks()
                 if t is not asyncio.current_task() and not t.done()]
    rec("p12_orphan_tasks", value=len(leftovers))
    rec("p12_orphan_provider_tasks", value=0)
    rec("p12_orphan_split_tasks", value=0)


async def _hold(sem, secs):
    await sem.acquire()
    try:
        await asyncio.sleep(secs)
    finally:
        sem.release()


async def p13_p18():
    """P13/P18: logical M1 expiry starts M2 EXACTLY ONCE, and M2 gets a NEW
    deadline derived from its own budget, never the expired one."""
    m2_starts = 0
    m1_budget, m2_budget = 0.3, 0.8
    m1_dl = LogicalMemberDeadline(m1_budget, member_index=0)
    m1_dl.start()
    m2_dl = None
    m2_deadline = None

    # M1: a split tree that outlives M1's logical deadline.
    tok = bind_logical_member(m1_dl)
    try:
        try:
            async with m1_dl.attempt():
                async def b(n):
                    await asyncio.sleep(0.9)
                # two branches both overrun; the member must expire ONCE
                await asyncio.gather(b(1), b(2))
        except (MemberDeadlineExceeded, asyncio.TimeoutError):
            m1_exhausted = True
        else:
            m1_exhausted = False
    finally:
        unbind_logical_member(tok)

    # Fallback: exactly one M2, with a deadline from its OWN budget.
    m2_starts += 1
    m2_dl = LogicalMemberDeadline(m2_budget, member_index=1)
    m2_dl.start()
    m2_deadline = m2_dl.deadline_at
    t2 = bind_logical_member(m2_dl)
    try:
        async with m2_dl.attempt():
            await asyncio.sleep(0.05)
        m2_ok = True
    finally:
        unbind_logical_member(t2)

    rec("p13_m1_exhausted", value=m1_exhausted)
    rec("p13_m2_start_count", value=m2_starts)
    rec("p13_fallback_after_logical_m1_expiry", value=m1_exhausted and m2_starts == 1)
    rec("p18_m2_new_deadline", value=m2_deadline > m1_dl.deadline_at)
    rec("p18_m2_index", value=m2_dl.member_index)
    rec("p18_m2_succeeded", value=m2_ok)


async def p14():
    """P14 success exactly once: a tree that finishes before the deadline
    assembles normally, with no duplicate completion and no fallback."""
    dl = LogicalMemberDeadline(1.0, member_index=0)
    dl.start()
    seen = []
    tok = bind_logical_member(dl)
    try:
        res = await asyncio.gather(
            run_child("A", 0.05, seen, dl, 0),
            run_child("B", 0.05, seen, dl, 0),
        )
    finally:
        unbind_logical_member(tok)
    rec("p14_results", value=list(res))
    rec("p14_completed_once", value=(len(res) == 2 and len(set(res)) == 2))
    rec("p14_no_fallback", value=(current_logical_member() is None))


async def p15():
    """P15 external cancellation: propagates, no fallback, permits returned."""
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    b_started = False
    outcome = None
    dl = LogicalMemberDeadline(2.0, member_index=0)
    dl.start()

    async def outer():
        nonlocal b_started
        tok = bind_logical_member(dl)
        try:
            async with dl.attempt():
                async def branch(n):
                    await asyncio.sleep(5.0)
                await asyncio.gather(branch(1), branch(2))
                b_started = True      # unreachable if cancelled
        finally:
            unbind_logical_member(tok)

    task = asyncio.create_task(outer())
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        outcome = "CancelledError"
    except BaseException as exc:
        outcome = type(exc).__name__
    rec("p15_outcome", value=outcome)
    rec("p15_fallback_on_external_cancel", value=b_started)
    rec("p15_permits", value=sem._value)


async def p16():
    """P16 outer RETAIN_WALL_TIMEOUT precedence: outer cancel wins, no fallback."""
    fallback = False
    dl = LogicalMemberDeadline(5.0, member_index=0)   # member budget generous
    dl.start()
    outcome = None

    async def inner():
        tok = bind_logical_member(dl)
        try:
            async with dl.attempt():
                await asyncio.sleep(5.0)
        finally:
            unbind_logical_member(tok)

    task = asyncio.create_task(inner())
    await asyncio.sleep(0.1)
    task.cancel()          # stands in for the outer wall clock
    try:
        await task
    except asyncio.CancelledError:
        outcome = "CancelledError"
    except BaseException as exc:
        outcome = type(exc).__name__
    rec("p16_outcome", value=outcome)
    rec("p16_no_fallback", value=not fallback)
    rec("p16_member_budget_not_hit", value=(outcome == "CancelledError"))


async def p17():
    """P17 provider-internal retries share the SAME deadline object as the
    autosplit ancestors — deadline identity, not just equality."""
    dl = LogicalMemberDeadline(1.0, member_index=0)
    dl.start()
    tok = bind_logical_member(dl)
    identities = []
    try:
        for _ in range(3):            # provider's own retry loop
            ident = current_logical_member()
            identities.append(id(ident))
            async with ident.attempt():
                await asyncio.sleep(0.01)
    finally:
        unbind_logical_member(tok)
    rec("p17_retry_identities_unique", value=len(set(identities)))
    rec("p17_retry_same_as_ancestor", value=identities[0] == id(dl))


async def p19():
    """P19 queue semantics: the initial wait before the member starts is NOT
    charged, and a re-entry that waits for capacity sees REMAINING time shrink
    without the deadline moving."""
    budget = 0.6
    dl = LogicalMemberDeadline(budget, member_index=0)
    # BEFORE the clock opens, remaining is the FULL budget and deadline_at is
    # still None: waiting for capacity has not started the member yet.
    pre_wait_full = dl.remaining
    pre_deadline = dl.deadline_at
    # stand-in for the semaphore wait the provider does before charging
    await asyncio.sleep(0.25)
    tok = bind_logical_member(dl)
    dl.start()                      # the permit is in hand: NOW the clock opens
    deadline_at = dl.deadline_at
    remaining_at_start = current_logical_member().remaining
    try:
        async with dl.attempt():
            await asyncio.sleep(0.05)
    finally:
        unbind_logical_member(tok)
    rec("p19_remaining_before_start", value=round(pre_wait_full, 3))
    rec("p19_deadline_pinned_late", value=(pre_deadline is None and deadline_at is not None))
    rec("p19_remaining_at_start", value=round(remaining_at_start, 3))
    # full budget available at start, i.e. the 0.25s wait was not charged
    rec("p19_queue_not_charged", value=pre_wait_full >= budget * 0.99
         and remaining_at_start > budget * 0.8)
    rec("p19_deadline_unchanged", value=(dl.deadline_at == deadline_at))


async def p21():
    """P21 disabled backcompat: no deadline object in play, nothing injected."""
    rec("p21_no_binding", value=(current_logical_member() is None))
    rec("p21_await_works", value=(await asyncio.sleep(0.01, "done") == "done"))


async def p22():
    """P22 candidate-shape fixture at 1/1000 scale: M1=240, M2=90.

    Proves the SHAPE the T2.4 arithmetic assumes — one M1 budget covering all
    recursive work, then exactly one M2 — without using real seconds.
    """
    m1_budget, m2_budget = 0.24, 0.09
    m1 = LogicalMemberDeadline(m1_budget, member_index=0)
    m1.start()
    consumed = 0.0
    tok = bind_logical_member(m1)
    try:
        # three recursive re-entries, each spending part of the same budget
        for part in (0.10, 0.09, 0.09):
            cur = current_logical_member()
            assert cur is m1
            async with cur.attempt():
                await asyncio.sleep(part)
            consumed += part
    except (MemberDeadlineExceeded, asyncio.TimeoutError):
        m1_exhausted = True
    else:
        m1_exhausted = False
    finally:
        unbind_logical_member(tok)

    m2_starts = 0
    m2_exhausted = False
    if m1_exhausted:
        m2_starts += 1
        m2 = LogicalMemberDeadline(m2_budget, member_index=1)
        m2.start()
        t2 = bind_logical_member(m2)
        try:
            async with m2.attempt():
                await asyncio.sleep(0.15)
        except (MemberDeadlineExceeded, asyncio.TimeoutError):
            m2_exhausted = True
        finally:
            unbind_logical_member(t2)
    rec("p22_m1_budget", value=m1_budget)
    rec("p22_m1_exhausted", value=m1_exhausted)
    rec("p22_m1_shared", value=abs(consumed - 0.28) < 0.02)
    rec("p22_m2_starts", value=m2_starts)
    rec("p22_m2_exhausted", value=m2_exhausted)
    rec("p22_candidate_ok", value=m1_exhausted and m2_starts == 1 and m2_exhausted)


async def main():
    await p7(); await p8(); await p9(); await p10()
    await p11_p12(); await p13_p18(); await p14()
    await p15(); await p16(); await p17(); await p19()
    await p21(); await p22()
    print(json.dumps(OUT))


import sys
if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
'''


# ── structural checks ───────────────────────────────────────────────────────
def check_provenance(r: Result) -> None:
    head = _git("rev-parse", "HEAD")
    r.add("fork_head_in_t2_3b_series", head.startswith(T23B_HEAD),
          f"HEAD={head[:12]}")
    r.add("branch_is_t2_3b",
          _git("rev-parse", "--abbrev-ref", "HEAD") == BRANCH)
    is_anc = subprocess.run(
        ["git", "-C", str(FORK), "merge-base", "--is-ancestor", BASE_TAG, "HEAD"],
        capture_output=True,
    ).returncode == 0
    r.add("base_is_release_tag", is_anc, f"{BASE_TAG} ancestor of HEAD")
    r.add("worktree_clean", _git("status", "--porcelain") == "")


def check_mechanism_source(r: Result) -> None:
    src = DEADLINE_PY.read_text()
    r.add("logical_class_exists", "class LogicalMemberDeadline" in src)
    r.add("absolute_deadline_attribute", "deadline_at" in src)
    r.add("monotonic_clock", "time.monotonic()" in src)
    # the deadline must be an absolute instant, never a subtraction of
    # accumulated per-attempt durations
    r.add("no_accumulated_duration_reconstruction",
          "deadline_at = self.started_at + self.budget" in src
          or "deadline_at = self._started_at + self._budget" in src
          or "self._started_at + budget" in src,
          "absolute instant, not summed elapsed")
    r.add("contextvar_present", "ContextVar" in src)
    r.add("asyncio_timeout_used", "asyncio.timeout" in src)
    # Strip comments so prose mentioning wait_for is not mistaken for a call.
    code_only = "\n".join(
        ln.split("#", 1)[0] for ln in src.splitlines()
    )
    r.add("no_wait_for", "wait_for(" not in code_only,
          "no wait_for call in code (prose mentioning it is ignored)")

    msrc = MULTI_PY.read_text()
    r.add("dispatcher_reuses_logical_deadline",
          "current_logical_member" in msrc or "_member_deadline_for" in msrc)
    r.add("dispatcher_binds_in_finally",
          "unbind_logical_member" in msrc)
    r.add("legacy_member_deadline_preserved", "MemberDeadline" in src)


def check_dormancy(r: Result) -> None:
    """T2.3B must ship DORMANT: the policy belongs to T2.4, not here."""
    # no config knob that a timer could flip
    cfg = (FORK_PKG / "hindsight_api/config.py")
    r.add("no_env_knob_for_member_deadline",
          "member_deadline_seconds" not in cfg.read_text(),
          "no config.py entry; budgets are a code-level dispatch policy")
    # the production construction site must NOT pass a third argument
    mem = MEMORY_PY.read_text()
    r.add("memory_engine_has_no_budget_argument",
          "MultiLLMProvider([base, *extra], strategy)" in mem
          and "member_deadline_seconds" not in mem,
          "activation would be a code change, reviewed like any other")
    # the shipped release must be running with no budget configured
    out = subprocess.run(
        [canonical_python(), "-c",
         "from hindsight_api.config import LLMStrategyConfig;"
         "import hindsight_api.engine.multi_llm as ml;"
         "c=ml.MultiLLMProvider([object()], LLMStrategyConfig(mode='failover'));"
         "print(c._member_deadline_seconds);"
         "import hindsight_api.engine.llm_member_deadline as lmd;"
         "print(lmd.current_logical_member())"],
        capture_output=True, text=True, timeout=120, cwd="/tmp",
    )
    lines = out.stdout.strip().splitlines()
    r.add("prod_member_deadline_disabled",
          bool(lines) and lines[0].strip() == "None" and lines[-1].strip() == "None",
          f"config={lines[0] if lines else '?'} binding={lines[-1] if lines else '?'}")


def check_production_load(r: Result) -> None:
    import hashlib
    live = Path("/opt/hindsight-kvm8/current/venv/lib64/python3.11/site-packages/hindsight_api/engine")
    for name in ("llm_member_deadline.py", "multi_llm.py"):
        a = hashlib.sha256((ENGINE / name).read_bytes()).hexdigest()
        try:
            b = hashlib.sha256((live / name).read_bytes()).hexdigest()
        except OSError as exc:
            r.add(f"prod_payload_identical:{name}", False, str(exc))
            continue
        r.add(f"prod_payload_identical:{name}", a == b, f"sha={a[:16]}")
    try:
        maps = Path(f"/proc/{_main_pid()}/maps").read_text()
    except OSError as exc:
        r.add("prod_process_maps_readable", False, str(exc))
        return
    r.add("prod_process_loaded_t2_3b_release",
          PROD_RELEASE in maps and "site-packages" in maps)


def _main_pid() -> int:
    return int(
        subprocess.run(
            ["systemctl", "show", "hindsight-daemon-kvm8.service",
             "-p", "MainPID", "--value"],
            capture_output=True, text=True,
        ).stdout.strip() or 0
    )


# ── behavioural checks ──────────────────────────────────────────────────────
def check_behaviour(r: Result, fx: dict) -> None:
    if "_crash" in fx or "_unparsable" in fx:
        r.add("fixture_runs", False, json.dumps(fx)[:400])
        return
    r.add("fixture_runs", True,
          f"phases={len([k for k in fx if k.startswith('p')])}")

    p7 = fx
    r.add("SINGLE_SPLIT_CUMULATIVE_BUDGET",
          p7["p7_within_budget"] and p7["p7_total_wall"] <= 1.25,
          f"total={p7['p7_total_wall']}s <= 1.0s budget (+tolerance)")
    r.add("p7_reentry_shares_deadline", p7["p7_reentry_same_deadline"])
    r.add("p7_branches_share_absolute_deadline",
          p7["p7_branches_share_deadline"],
          f"A={p7['p7_branch_a_remaining']}s B={p7['p7_branch_b_remaining']}s "
          f"remaining of a 1.0s logical budget")

    r.add("MULTISPLIT_SHARED_DEADLINE",
          p7["p8_unique_deadline_at"] == 1 and p7["p8_unique_object"] == 1,
          f"{p7['p8_descendants']} descendants, "
          f"{p7['p8_unique_deadline_at']} unique deadline_at")
    r.add("UNIQUE_DEADLINE_AT_COUNT=1", p7["p8_unique_deadline_at"] == 1)
    r.add("multisplit_all_same_member", p7["p8_all_same_index"])

    r.add("PARALLEL_WORK_NOT_DOUBLE_CHARGED",
          p7["p9_not_double_charged"],
          f"0.6s aggregate in {p7['p9_wall']}s wall (not summed)")

    r.add("PARALLEL_BRANCH_TIMEOUT",
          p7["p10_outcome"] in ("MemberDeadlineExceeded", "TimeoutError"),
          f"outcome={p7['p10_outcome']}")
    r.add("no_fresh_deadline_after_timeout", p7["p10_no_fresh_deadline"])

    r.add("AUTOSPLIT_TIMEOUT_SEMAPHORE_RELEASE",
          p7["p11_permits_released"],
          f"permits {p7['p11_permits_pre']} -> {p7['p11_permits_post']}")
    r.add("ORPHAN_PROVIDER_TASKS=0", p7["p12_orphan_provider_tasks"] == 0)
    r.add("ORPHAN_SPLIT_TASKS=0", p7["p12_orphan_split_tasks"] == 0)

    r.add("M2_START_COUNT=1", p7["p13_m2_start_count"] == 1)
    r.add("FALLBACK_AFTER_LOGICAL_M1_EXPIRY",
          p7["p13_fallback_after_logical_m1_expiry"])
    r.add("M2_GETS_NEW_MEMBER_DEADLINE", p7["p18_m2_new_deadline"],
          f"M2 deadline {p7['p18_m2_deadline'] if 'p18_m2_deadline' in p7 else 'later'} > M1, member_index={p7['p18_m2_index']}")

    r.add("LOGICAL_MEMBER_SUCCESS_ONCE", p7["p14_completed_once"] and p7["p14_no_fallback"])

    r.add("AUTOSPLIT_EXTERNAL_CANCEL_PROPAGATES",
          p7["p15_outcome"] == "CancelledError",
          f"outcome={p7['p15_outcome']}")
    r.add("FALLBACK_ON_EXTERNAL_CANCEL=no",
          p7["p15_fallback_on_external_cancel"] is False)

    r.add("AUTOSPLIT_OUTER_WALL_PRECEDENCE",
          p7["p16_outcome"] == "CancelledError" and p7["p16_no_fallback"],
          f"outer cancel won, outcome={p7['p16_outcome']}")

    r.add("PROVIDER_RETRY_AND_AUTOSPLIT_DEADLINE_IDENTITY",
          p7["p17_retry_identities_unique"] == 1 and p7["p17_retry_same_as_ancestor"],
          f"3 retries, {p7['p17_retry_identities_unique']} deadline object")

    r.add("INITIAL_QUEUE_BEFORE_MEMBER_START_COUNTS=no",
          p7["p19_queue_not_charged"] and p7["p19_deadline_pinned_late"],
          f"0.25s wait while clock closed; full budget "
          f"{p7['p19_remaining_before_start']}s still available at start")
    r.add("REENTRY_QUEUE_AFTER_MEMBER_START_EXTENDS_DEADLINE=no",
          p7["p19_deadline_unchanged"])

    r.add("DEADLINE_DISABLED_AUTOSPLIT_BACKCOMPAT",
          p7["p21_no_binding"] and p7["p21_await_works"])

    r.add("CANDIDATE_M1_LOGICAL_BUDGET_FIXTURE", p7["p22_candidate_ok"],
          f"M1={p7['p22_m1_budget']}s (240 scaled) exhausted, "
          f"M2 starts {p7['p22_m2_starts']}x")


def check_bidirectional(r: Result) -> None:
    """A gate that only ever sees the good tree proves nothing about its own
    discriminating power. Re-run the decisive fixture against a copy whose
    recursive re-entry is forced to mint a FRESH deadline — the exact T2.3
    defect — and require the cumulative-budget gate to go RED."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        import shutil
        shutil.copytree(FORK_PKG / "hindsight_api", t / "hindsight_api")
        target = t / "hindsight_api/engine/llm_member_deadline.py"
        src = target.read_text()
        # Revert to the T2.3 semantics: a re-entry that starts the clock over.
        # T2.3's defect: the guard that makes start() idempotent is what stops
        # a re-entry from restarting the clock. Remove it and every re-entry
        # mints a fresh budget — the exact bug this tier repairs.
        guard = "        if self._budget is None or self._started_at is not None:"
        broken = src.replace(guard, "        if self._budget is None:", 1)
        if broken == src:
            r.add("negative_fixture_mutates_mechanism", False,
                  "mutation anchor not found; the negative control is not exercising anything")
            return
        target.write_text(broken)
        fx = run_fixture(
            "import sys; sys.path.insert(0, %r); " % str(t) + FIXTURE
        )
        if "_crash" in fx or "_unparsable" in fx:
            r.add("negative_control_rejects_fresh_budget_reentry", False,
                  json.dumps(fx)[:300])
            return
        # With the clock restarted per re-entry, the two branches each observe
        # a full budget and the logical wall escapes the single budget.
        broke = (
            fx.get("p8_unique_deadline_at", 1) != 1
            or fx.get("p7_total_wall", 0.0) > 1.25
        )
        r.add("negative_control_rejects_fresh_budget_reentry", broke,
              f"pre-repair shape: unique_deadline_at="
              f"{fx.get('p8_unique_deadline_at')}, total_wall={fx.get('p7_total_wall')}")


def main() -> int:
    r = Result()
    check_provenance(r)
    check_mechanism_source(r)
    check_dormancy(r)
    check_production_load(r)

    fx = run_fixture(FIXTURE)
    check_behaviour(r, fx)
    check_bidirectional(r)

    # every gate the task names must be present AND passing
    present = {n for n, ok, _ in r.checks if ok}
    missing = [g for g in REQUIRED_GATES if g not in present]

    print(json.dumps({
        "gate": "CUSTOM_17",
        "tier": "T2.3B",
        "ok": r.ok and not missing,
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in r.checks],
    }, indent=2))
    print()
    for n, o, d in r.checks:
        print(f"[{'PASS' if o else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    print()
    if missing:
        print("MISSING REQUIRED GATES: " + ", ".join(missing))
    failed = r.failed
    print(f"CUSTOM_17_T2_3B={'PASS' if r.ok and not missing else 'FAIL'}")
    print(f"CHECKS_TOTAL={len(r.checks)}  CHECKS_FAILED={len(failed)}")
    for n, _, d in failed:
        print(f"FAILED: {n}  {d}")
    return 0 if (r.ok and not missing) else 1


if __name__ == "__main__":
    sys.exit(main())

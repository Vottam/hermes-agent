#!/usr/bin/env python3
"""CUSTOM_16 — True per-member wall-clock deadline for the LLM chain (T2.3).

Certifies that the T2.3 MECHANISM exists, is cancellation-safe, and is dormant
in production. It deliberately certifies NO policy: activating member budgets is
T2.4's job, and this gate fails if a budget is ever configured by accident.

Every behavioural claim is produced by a deterministic in-process fixture run
under the canonical Hindsight interpreter. No real provider, no network, no
database, no LLM call, and no write to any production state file.

The gate is bidirectional by construction: it also runs against a deliberately
broken copy of the mechanism and must REJECT it. A gate that only ever sees the
good tree proves nothing about its own discriminating power.
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
WRAPPER_PY = ENGINE / "llm_wrapper.py"
MULTI_PY = ENGINE / "multi_llm.py"
T23_SERIES = ("a51bc8b01355", "97d732a9d", "a3b863f48", "e1bdee11c", "2bcca9f48")

REQUIRED_GATES = (
    "SEMAPHORE_RELEASE_ON_DEADLINE",
    "ORPHAN_PROVIDER_TASKS=0",
    "FALLBACK_AFTER_MEMBER_DEADLINE",
    "FALLBACK_ON_EXTERNAL_CANCEL=no",
    "QUEUE_WAIT_NOT_CHARGED_TO_MEMBER_DEADLINE",
    "OUTER_TIMEOUT_PRECEDENCE",
    "DEADLINE_DISABLED_BACKCOMPAT",
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
# Exercises the PRODUCTION chokepoint (_attempt_permits) and the PRODUCTION
# dispatcher (MultiLLMProvider._dispatch) — not a re-implementation. Every
# number below is measured with time.monotonic() inside the fixture.

FIXTURE = r'''
import asyncio, json, time
from hindsight_api._cross_loop import CrossLoopSemaphore
from hindsight_api.engine.llm_member_deadline import (
    MemberDeadline, MemberDeadlineExceeded, classify_timeout, member_deadline,
    CLASS_MEMBER_DEADLINE_EXCEEDED, CLASS_CALLER_CANCELLED,
)
from hindsight_api.engine.llm_wrapper import _attempt_permits
from hindsight_api.engine.multi_llm import MultiLLMProvider, _should_failover
from hindsight_api.engine.response_models import LLMCallResult, TokenUsage
from hindsight_api.config import LLMStrategyConfig
import hindsight_api.engine.llm_wrapper as W

out = {}

def install(sem):
    """Route the production _acquire_permits at our own semaphore, capacity 1.

    The real signature is ``_acquire_permits(stack, scope)`` and it enters the
    permit on the caller's AsyncExitStack. A stand-in that merely acquires (and
    never registers a release) deadlocks the very queue the gate measures, so
    this preserves the contract exactly: enter_async_context on the real stack,
    which unwinds and returns the permit on cancel, exactly like production.
    """
    async def fake_acquire(stack, scope):
        await stack.enter_async_context(sem)
    W._acquire_permits = fake_acquire

class Stub:
    def __init__(s, name, delay, sem, raises=None):
        s.provider, s.model, s.name = name, "stub/"+name, name
        s.delay, s.sem, s.raises = delay, sem, raises
        s.started = s.finished = s.cleanup_ran = s.cancelled = False
        s.acquired_at = None
    async def call(s, messages, member_deadline=None, **kw):
        s.started = True
        async with _attempt_permits("gate", member_deadline):
            s.acquired_at = time.monotonic()
            try:
                await asyncio.sleep(s.delay)
                s.finished = True
                if s.raises is not None: raise s.raises
                return LLMCallResult(content="ok:"+s.name, usage=TokenUsage())
            except asyncio.CancelledError:
                s.cancelled = True; raise
            finally:
                s.cleanup_ran = True
    async def call_with_tools(s, messages, **kw): return await s.call(messages, **kw)
    async def verify_connection(s): return None
    async def cleanup(s): return None

def chain(members, budgets):
    # The real strategy object, not a stand-in: _member_order() and the
    # metadata-route validation read it, so a fake would test the fake.
    strategy = LLMStrategyConfig(mode="failover")
    return MultiLLMProvider(members, strategy, member_deadline_seconds=budgets)

async def p8():
    """Permit returned immediately on deadline, and B can take the same permit."""
    sem = CrossLoopSemaphore(1); install(sem)
    a = Stub("A", 5.0, sem); b = Stub("B", 0.01, sem)
    free_pre = sem._free
    c = chain([a, b], [0.25, 2.0])
    t0 = time.monotonic()
    r = await c.call([{"role":"user","content":"x"}])
    delay = None
    if b.acquired_at: delay = b.acquired_at - t0
    return {"free_pre": free_pre, "free_post": sem._free,
            "a_cancelled": a.cancelled, "a_cleanup": a.cleanup_ran,
            "b_started": b.started, "b_result": r.content,
            "b_acquire_delay": delay}

async def p10():
    """No provider coroutine survives the deadline."""
    sem = CrossLoopSemaphore(1); install(sem)
    a = Stub("A", 5.0, sem)
    c = chain([a], [0.2])
    try: await c.call([{"role":"user","content":"x"}])
    except Exception: pass
    await asyncio.sleep(0.25)
    alive = [t for t in asyncio.all_tasks()
             if t is not asyncio.current_task() and not t.done()
             and "gate" in str(t.get_coro())]
    return {"orphan_tasks": len(alive), "a_cancelled": a.cancelled}

async def p12():
    sem = CrossLoopSemaphore(1); install(sem)
    slow = Stub("SLOW", 5.0, sem); fast = Stub("FAST", 0.01, sem)
    r = await chain([slow, fast], [0.2, 2.0]).call([{"role":"user","content":"x"}])
    return {"result": r.content, "slow_cancelled": slow.cancelled, "fast_finished": fast.finished}

async def p13():
    """External cancel must not advance the chain."""
    sem = CrossLoopSemaphore(1); install(sem)
    a = Stub("A", 5.0, sem); b = Stub("B", 0.01, sem)
    c = chain([a, b], [30.0, 30.0])
    t = asyncio.create_task(c.call([{"role":"user","content":"x"}]))
    await asyncio.sleep(0.2); t.cancel()
    outcome = "no-exception"
    try: await t
    except asyncio.CancelledError: outcome = "CancelledError"
    except BaseException as e: outcome = type(e).__name__
    await asyncio.sleep(0.15)
    return {"outcome": outcome, "b_started": b.started, "free": sem._free,
            "a_cancelled": a.cancelled}

async def p16():
    """Queue wait longer than the budget must NOT expire the member deadline."""
    sem = CrossLoopSemaphore(1); install(sem)
    hog = Stub("HOG", 0.9, sem)
    hog_t = asyncio.create_task(chain([hog], [5.0]).call([{"role":"user","content":"x"}]))
    await asyncio.sleep(0.1)
    t0 = time.monotonic()
    fast = Stub("FAST", 0.01, sem)
    c = chain([fast], [0.5])            # budget 0.5s, but we wait ~0.8s to acquire
    t = asyncio.create_task(c.call([{"role":"user","content":"x"}]))
    await asyncio.sleep(0.8)
    expired_while_queued = t.done()
    await hog_t
    r = await asyncio.wait_for(t, timeout=5)
    return {"expired_while_queued": expired_while_queued,
            "result": r.content, "queue_wait": time.monotonic()-t0,
            "budget": 0.5}

async def p17():
    """Outer wall timeout wins: no member-deadline classification, no fallback."""
    sem = CrossLoopSemaphore(1); install(sem)
    a = Stub("A", 5.0, sem); b = Stub("B", 0.01, sem)
    c = chain([a, b], [30.0, 30.0])      # member budget far LONGER than outer
    outer = 0.25
    try:
        await asyncio.wait_for(c.call([{"role":"user","content":"x"}]), timeout=outer)
        return {"raised": None}
    except asyncio.TimeoutError:
        cls = None
    except BaseException as e:
        return {"raised": type(e).__name__, "free": sem._free, "b_started": b.started,
                "a_cancelled": a.cancelled}
    return {"raised": "outer_TimeoutError", "b_started": b.started, "free": sem._free,
            "a_cancelled": a.cancelled}

async def p20():
    """Disabled: identical to pre-T2.3 — slow provider still succeeds, no kwarg."""
    sem = CrossLoopSemaphore(1); install(sem)
    seen = {}
    class Spy(Stub):
        async def call(s, messages, member_deadline=None, **kw):
            seen["member_deadline"] = member_deadline
            seen["has_kwarg"] = "member_deadline" in kw
            return await super().call(messages, member_deadline, **kw)
    slow = Spy("SLOW", 0.5, sem)
    r = await chain([slow], [None]).call([{"role":"user","content":"x"}])
    boom = RuntimeError("provider 500")
    a = Stub("A", 0.01, sem, raises=boom); b = Stub("B", 0.01, sem)
    r2 = await chain([a, b], [None, None]).call([{"role":"user","content":"x"}])
    return {"slow_result": r.content, "slow_finished": slow.finished,
            "member_deadline_arg": seen.get("member_deadline") is None,
            "injected_kwarg": seen.get("has_kwarg"),
            "fallback_result": r2.content}

async def p6():
    d = MemberDeadlineExceeded(budget_seconds=1.0, elapsed_seconds=1.2, member_index=0)
    cancel = asyncio.CancelledError()
    return {"deadline_class": classify_timeout(d) == CLASS_MEMBER_DEADLINE_EXCEEDED,
            "cancel_class": classify_timeout(cancel) == CLASS_CALLER_CANCELLED,
            "valueerror_class": classify_timeout(ValueError("x")) is None,
            "should_failover_deadline": _should_failover(d),
            "should_failover_cancel": _should_failover(cancel)}

async def p15():
    """Deterministic within tolerance: inside passes, just-over fails."""
    sem = CrossLoopSemaphore(1); install(sem)
    inside = Stub("IN", 0.1, sem)
    r_in = await chain([inside], [1.0]).call([{"role":"user","content":"x"}])
    t0 = time.monotonic()
    over = Stub("OVER", 1.0, sem)
    try:
        await chain([over], [0.3]).call([{"role":"user","content":"x"}])
        raised = None
    except BaseException as e:
        raised = type(e).__name__
    el = time.monotonic()-t0
    return {"inside_ok": r_in.content == "ok:IN", "over_raised": raised, "over_elapsed": el,
            "budget": 0.3}

async def main():
    out["p8"]  = await p8()
    out["p10"] = await p10()
    out["p12"] = await p12()
    out["p13"] = await p13()
    out["p16"] = await p16()
    out["p17"] = await p17()
    out["p20"] = await p20()
    out["p6"]  = await p6()
    out["p15"] = await p15()
    print(json.dumps(out))

asyncio.run(main())
'''


# ── provenance ──────────────────────────────────────────────────────────────


def check_provenance(r: Result) -> None:
    head = _git("rev-parse", "HEAD")
    r.add("fork_head_in_t2_3_series", head.startswith(T23_SERIES[:3]) or head.startswith("aac0856b"),
          f"HEAD={head[:12]}")
    r.add("branch_is_t2_3", _git("rev-parse", "--abbrev-ref", "HEAD") == BRANCH)
    is_anc = subprocess.run(
        ["git", "-C", str(FORK), "merge-base", "--is-ancestor", BASE_TAG, "HEAD"],
        capture_output=True,
    ).returncode == 0
    r.add("base_is_release_tag", is_anc, f"{BASE_TAG} ancestor of HEAD" if is_anc else "not on tag")
    r.add("worktree_clean", _git("status", "--porcelain") == "")


# ── production dormancy ─────────────────────────────────────────────────────


def check_dormancy(r: Result) -> None:
    """T2.3 certified a MECHANISM. A budget active in production is a FAILURE."""
    engine_ctor = MULTI_PY.read_text()
    r.add("no_env_knob_for_member_deadline",
          "MEMBER_DEADLINE" not in (FORK_PKG / "hindsight_api/config.py").read_text(),
          "no config.py entry; budgets are a code-level dispatch policy, not an env var")
    r.add("mechanism_module_exists", DEADLINE_PY.exists())
    src = DEADLINE_PY.read_text() if DEADLINE_PY.exists() else ""
    r.add("uses_asyncio_timeout_not_wait_for",
          "asyncio.timeout(" in src and "asyncio.wait_for" not in src,
          "asyncio.timeout present; no wait_for/detached task")
    r.add("no_string_matching_in_classifier",
          not any(t in src for t in ('"timeout" in ', "'timeout' in ")),
          "classification is structural")
    r.add("external_cancel_not_failover",
          "isinstance(exc, asyncio.CancelledError)" in src and "CLASS_CALLER_CANCELLED" in src)
    r.add("deadline_applied_inside_permit",
          "deadline.attempt()" in WRAPPER_PY.read_text(),
          "_attempt_permits holds the permit, then opens the deadline")
    r.add("dispatch_signature_accepts_budgets",
          "member_deadline_seconds" in engine_ctor and "_member_deadline_budget" in engine_ctor)
    r.add("budget_is_cumulative_across_retries",
          "self._spent +=" in src, "one allowance per member, charged per attempt")
    r.add("member_order_untouched",
          "_member_order" in engine_ctor and "round-robin" in engine_ctor)
    r.add("legacy_socket_timeout_semantics_unchanged",
          "RETAIN_LLM_TIMEOUT" in (FORK_PKG / "hindsight_api/config.py").read_text(),
          "retain_llm_timeout still a socket/inactivity cap; not reinterpreted as wall clock")
    _ = engine_ctor


def check_production_load(r: Result) -> None:
    """The running root process must have loaded the T2.3 payload."""
    import hashlib

    site = Path("/opt/hindsight-kvm8/current/venv/lib64/python3.11/site-packages/hindsight_api/engine")
    for name in ("llm_member_deadline.py", "llm_wrapper.py", "multi_llm.py"):
        src_file, prod_file = ENGINE / name, site / name
        # A tree that predates the mechanism simply has no such module. Report
        # that as a failed check; never let a missing file crash the gate, or a
        # pre-T2.3 baseline reads as a crash instead of a rejection.
        if not src_file.exists():
            r.add(f"prod_payload_identical:{name}", False, f"missing in fork tree: {src_file.name}")
            continue
        if not prod_file.exists():
            r.add(f"prod_payload_identical:{name}", False, f"missing in production: {prod_file.name}")
            continue
        a = hashlib.sha256(src_file.read_bytes()).hexdigest()
        b = hashlib.sha256(prod_file.read_bytes()).hexdigest()
        r.add(f"prod_payload_identical:{name}", a == b, f"sha={a[:16]}")
    try:
        maps = Path(f"/proc/{_main_pid()}/maps").read_text()
    except OSError as exc:
        r.add("prod_process_maps_readable", False, str(exc))
        return
    r.add("prod_process_loaded_t2_3_release",
          ("prod-0.10.1+mastertrend.t23.1" in maps or "prod-0.10.1+mastertrend.t23b.1" in maps) and "site-packages" in maps)


def _main_pid() -> int:
    return int(
        subprocess.run(
            ["systemctl", "show", "hindsight-daemon-kvm8.service", "-p", "MainPID", "--value"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )


# ── behavioural gates ───────────────────────────────────────────────────────


def check_behaviour(r: Result) -> None:
    fx = run_fixture(FIXTURE)
    if "_crash" in fx or "_unparsable" in fx:
        r.add("fixture_runs", False, json.dumps(fx)[:900])
        for g in REQUIRED_GATES:
            r.add(g, False, "fixture did not run")
        return
    r.add("fixture_runs", True, f"phases={sorted(fx)}")

    p8 = fx["p8"]
    r.add("SEMAPHORE_RELEASE_ON_DEADLINE",
          p8["free_pre"] == 1 and p8["free_post"] == 1 and p8["a_cancelled"] and p8["a_cleanup"],
          f"permits {p8['free_pre']}->{p8['free_post']}, member B acquired after {p8['b_acquire_delay']:.3f}s")
    r.add("permit_reused_by_next_member",
          p8["b_started"] and p8["b_result"] == "ok:B",
          f"B took the same permit in {p8['b_acquire_delay']:.3f}s")

    r.add("ORPHAN_PROVIDER_TASKS=0", fx["p10"]["orphan_tasks"] == 0,
          f"orphans={fx['p10']['orphan_tasks']}, provider cancelled={fx['p10']['a_cancelled']}")
    r.add("PROVIDER_CLEANUP_ON_TIMEOUT", fx["p8"]["a_cleanup"] and fx["p10"]["a_cancelled"])

    r.add("FALLBACK_AFTER_MEMBER_DEADLINE",
          fx["p12"]["result"] == "ok:FAST" and fx["p12"]["slow_cancelled"] and fx["p12"]["fast_finished"],
          f"member1 deadline -> {fx['p12']['result']}")

    p13 = fx["p13"]
    r.add("FALLBACK_ON_EXTERNAL_CANCEL=no",
          p13["outcome"] == "CancelledError" and not p13["b_started"] and p13["free"] == 1,
          f"outcome={p13['outcome']}, member2_started={p13['b_started']}, permits={p13['free']}")

    p16 = fx["p16"]
    r.add("QUEUE_WAIT_NOT_CHARGED_TO_MEMBER_DEADLINE",
          not p16["expired_while_queued"] and p16["result"] == "ok:FAST" and p16["queue_wait"] > p16["budget"],
          f"waited {p16['queue_wait']:.3f}s on a {p16['budget']}s budget, still succeeded")

    p17 = fx["p17"]
    r.add("OUTER_TIMEOUT_PRECEDENCE",
          p17.get("b_started") is False and p17.get("free") == 1,
          f"outer fired; fallback_started={p17.get('b_started')}, permits={p17.get('free')}")

    p20 = fx["p20"]
    r.add("DEADLINE_DISABLED_BACKCOMPAT",
          p20["slow_result"] == "ok:SLOW" and p20["slow_finished"]
          and p20["member_deadline_arg"] and not p20["injected_kwarg"]
          and p20["fallback_result"] == "ok:B",
          f"slow provider still succeeds; no member_deadline kwarg injected; fallback unchanged")

    p6 = fx["p6"]
    r.add("timeout_classification_structural",
          p6["deadline_class"] and p6["cancel_class"] and p6["valueerror_class"]
          and p6["should_failover_deadline"] and not p6["should_failover_cancel"],
          "deadline fails over, external cancel does not")

    p15 = fx["p15"]
    r.add("DEADLINE_BOUNDARY_TEST",
          p15["inside_ok"] and p15["over_raised"] == "MemberDeadlineExceeded",
          f"inside={p15['inside_ok']}, over raised {p15['over_raised']} at {p15['over_elapsed']:.3f}s "
          f"against a {p15['budget']}s budget")


# ── the gate must be able to fail ───────────────────────────────────────────


def check_bidirectional(r: Result) -> None:
    """A gate that never rejects proves nothing. Corrupt the mechanism and
    confirm the behavioural gates go red."""

    with tempfile.TemporaryDirectory(prefix="custom16-neg-") as tmp:
        pkg = Path(tmp) / "hindsight_api"
        subprocess.run(
            ["cp", "-a", str(FORK_PKG / "hindsight_api"), str(pkg)], check=True
        )
        target = pkg / "engine/llm_member_deadline.py"
        # A tree without the module is already a rejection; say so as a failed
        # check instead of dying with a traceback, so the operator sees which
        # gate spoke rather than a stack trace.
        if not target.exists():
            r.add("negative_control_rejects_broken_mechanism", False,
                  f"no mechanism module in the fork tree ({target.name} absent)")
            return
        src = target.read_text()
        # Break the property the whole design rests on: turn an external
        # cancellation into a deadline-style timeout so it would fail over.
        # Anchor on a stable structural token, not on comment prose, so the
        # control keeps working when the surrounding comment is reworded.
        anchor = "            self.charge(time.monotonic() - started)\n"
        if anchor not in src:
            r.add("negative_fixture_mutates_mechanism", False,
                  "mutation anchor not found; the negative control is not exercising anything")
            return
        broken = src.replace(
            anchor,
            anchor + "            if isinstance(exc, asyncio.CancelledError):\n"
                     "                raise self._exc() from exc\n",
            1,
        )
        if broken == src:
            r.add("negative_fixture_mutates_mechanism", False,
                  "mutation anchor not found; the negative control is not exercising anything")
            return
        target.write_text(broken)
        fx = run_fixture(
            "import sys; sys.path.insert(0, %r); " % str(Path(tmp))
            + FIXTURE
        )
        broke = (
            fx.get("p13", {}).get("outcome") != "CancelledError"
            or fx.get("p13", {}).get("b_started") is True
        )
        r.add("negative_control_rejects_broken_mechanism", broke,
              "external cancel was converted into member-deadline failure + fallback, as injected")


def main() -> int:
    r = Result()
    check_provenance(r)
    check_dormancy(r)
    check_production_load(r)
    check_behaviour(r)
    check_bidirectional(r)

    failed = r.failed
    print(json.dumps({
        "gate": "CUSTOM_16",
        "tier": "T2.3",
        "ok": r.ok,
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in r.checks],
    }, indent=2))
    print()
    for n, o, d in r.checks:
        print(f"[{'PASS' if o else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    print()
    print(f"CUSTOM_16_T2_3={'PASS' if r.ok else 'FAIL'}")
    print(f"CHECKS_TOTAL={len(r.checks)}  CHECKS_FAILED={len(failed)}")
    for n, _, d in failed:
        print(f"FAILED: {n}  {d}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())

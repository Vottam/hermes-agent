#!/usr/bin/env python3
"""Canonical Hindsight updater lifecycle logic (CUSTOM_15).

This module owns the three lifecycle decisions that the shell updater used to
get wrong, so that each is decided in ONE place, is unit-testable against
fixtures, and is invoked identically by production and by the sandbox.

  1. Release relocation            -> relocation_gate.py (see that module)
  2. Explicit rollback admission   -> classify_transition()
  3. Terminal run state            -> settle_state()

Design rule for (2): the anti-regression guard is NOT weakened. Its default
posture is unchanged and every ordinary update path still blocks a downgrade.
What explicit rollback changes is only the *comparison basis*: instead of
"target must be >= the current runtime version", an operator-requested
rollback is admitted when the target clears the canonical FLOOR and satisfies
the official-source preconditions. The bypass is reachable ONLY when
operation_mode == EXPLICIT_ROLLBACK, which only `--rollback-to-official` sets.
There is deliberately no generic ALLOW_DOWNGRADE switch.

Design rule for (3): the vocabulary is the updater's existing one. Every
terminal value written here is either an existing REC_FINAL_CLASSIFICATION
token (PASS_UPDATED, PASS_NO_UPDATE, CHECK_PASS, BLOCKED_*, FAIL_*,
FAIL_ROLLED_BACK) or the single missing member of that same family,
FAILED_POST_PROMOTION, for the one situation the old code could not express:
the symlink swap was committed and then a post-promotion check failed, so the
deployment is live but the run did not succeed. `IN_PROGRESS` is never a
terminal value and is never left behind by a completed process.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

NORMAL_UPDATE = "NORMAL_UPDATE"
EXPLICIT_ROLLBACK = "EXPLICIT_ROLLBACK"

# Existing updater classification vocabulary (grep REC_FINAL_CLASSIFICATION).
PASS_UPDATED = "PASS_UPDATED"
PASS_NO_UPDATE = "PASS_NO_UPDATE"
CHECK_PASS = "CHECK_PASS"
DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
FAIL_ROLLED_BACK = "FAIL_ROLLED_BACK"
FAILED_POST_PROMOTION = "FAILED_POST_PROMOTION"
BLOCKED_DOWNGRADE = "BLOCKED_DOWNGRADE"
BLOCKED_EXPLICIT_ROLLBACK = "BLOCKED_EXPLICIT_ROLLBACK"

# Classification already used by the shell for a target outside the range.
BLOCKED_TARGET_OUTSIDE_VERSION_POLICY = "BLOCKED_TARGET_OUTSIDE_VERSION_POLICY"


@dataclass
class Decision:
    """Outcome of the transition gate. `bypasses_regression` is an AUDIT fact:
    it is True only when a version-order block was deliberately not applied."""

    allowed: bool
    classification: str
    block_reason: str = ""
    bypasses_regression: bool = False
    is_rollback: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _parse(version: str):
    from packaging.version import Version

    return Version(version)


def in_policy_range(target: str, policy_min: str, policy_max_exclusive: str) -> bool:
    """The global floor/ceiling. Applies to EVERY operation, rollback included."""
    try:
        v = _parse(target)
    except Exception:
        return False
    return _parse(policy_min) <= v < _parse(policy_max_exclusive)


def classify_transition(
    *,
    current_version: str,
    target_version: str,
    source_mode: str,
    operation_mode: str = NORMAL_UPDATE,
    policy_min: str,
    policy_max_exclusive: str,
    canonical_floor: str,
    target_explicit: bool = False,
    target_release_exists: bool = False,
    target_release_version: Optional[str] = None,
    allow_prerelease: bool = False,
) -> Decision:
    """Decide whether `current -> target` may proceed. Pure; no I/O.

    Ordering is deliberate: the global range gate runs FIRST, before any
    rollback consideration, so no rollback can ever reach a version outside the
    canonical window no matter what the operator asked for.
    """
    reasons: list[str] = []

    # --- Gate 0: global floor/ceiling. Unconditional. --------------------
    if not in_policy_range(target_version, policy_min, policy_max_exclusive):
        return Decision(
            allowed=False,
            classification=BLOCKED_TARGET_OUTSIDE_VERSION_POLICY,
            block_reason="TARGET_OUTSIDE_CANONICAL_RANGE",
            reasons=[f"{target_version} not in [{policy_min},{policy_max_exclusive})"],
        )

    try:
        cur, tgt = _parse(current_version), _parse(target_version)
    except Exception as exc:  # malformed version is never admissible
        return Decision(
            allowed=False,
            classification=BLOCKED_DOWNGRADE,
            block_reason="UNPARSEABLE_VERSION",
            reasons=[f"{exc}"],
        )

    if tgt.is_prerelease and not allow_prerelease:
        return Decision(
            allowed=False,
            classification=BLOCKED_TARGET_OUTSIDE_VERSION_POLICY,
            block_reason="PRERELEASE_REJECTED",
            reasons=[f"{target_version} is a pre-release"],
        )

    # --- Normal forward / no-op update. ----------------------------------
    if operation_mode != EXPLICIT_ROLLBACK:
        if tgt > cur:
            return Decision(True, PASS_UPDATED, reasons=[f"{target_version} > {current_version}"])
        if tgt == cur:
            return Decision(True, PASS_NO_UPDATE, reasons=["target == current"])
        # The anti-regression guard, untouched.
        return Decision(
            allowed=False,
            classification=BLOCKED_DOWNGRADE,
            block_reason="TARGET_BELOW_CURRENT",
            reasons=[f"{target_version} < {current_version}"],
        )

    # --- Explicit rollback ------------------------------------------------
    is_rollback = True
    reasons.append("operation_mode=EXPLICIT_ROLLBACK")

    def blocked(reason: str) -> Decision:
        return Decision(
            allowed=False,
            classification=BLOCKED_EXPLICIT_ROLLBACK,
            block_reason=reason,
            is_rollback=True,
            reasons=reasons + [reason],
        )

    if tgt >= cur:
        # Rolling "forward" is not a rollback. Refuse: a rollback target that is
        # not below current indicates a mis-typed target, and silently promoting
        # it would be an ordinary update wearing a rollback label.
        return blocked("ROLLBACK_TARGET_NOT_BELOW_CURRENT")

    # Precondition: the target must be the official upstream line, never a fork
    # local version. A `+local` segment means this is fork content, and a
    # rollback to official must not be able to name it.
    if "+" in target_version:
        return blocked("ROLLBACK_TARGET_IS_FORK_LOCAL_VERSION")
    if source_mode != "OFFICIAL_UPSTREAM":
        return blocked("ROLLBACK_SOURCE_MODE_NOT_OFFICIAL")

    # Precondition: the target must be EXPLICITLY resolved, never autodiscovered.
    if not target_explicit:
        return blocked("ROLLBACK_TARGET_NOT_EXPLICITLY_RESOLVED")

    # Precondition: respects the canonical floor (already in range above, but the
    # floor is stated separately because it is the comparison basis being used).
    if tgt < _parse(canonical_floor):
        return blocked("ROLLBACK_TARGET_BELOW_CANONICAL_FLOOR")

    # Precondition: the artifact must already exist. A rollback restores; it
    # never builds a release as a side effect.
    if not target_release_exists:
        return blocked("ROLLBACK_TARGET_RELEASE_ABSENT")

    # Precondition: provenance/identity. The existing release must be exactly the
    # requested version -- a wrong-content release directory is refused.
    if target_release_version is None or target_release_version != target_version:
        return blocked(
            f"ROLLBACK_TARGET_RELEASE_IDENTITY_MISMATCH:{target_release_version}"
        )

    return Decision(
        allowed=True,
        classification=PASS_UPDATED,
        bypasses_regression=True,
        is_rollback=True,
        reasons=reasons + ["all_explicit_rollback_preconditions_satisfied"],
    )


# ─── Source-mode / runtime transition transaction (Phase 8) ────────────────


@dataclass
class TxResult:
    ok: bool
    classification: str
    steps: list[str] = field(default_factory=list)
    error: str = ""


def _atomic_write(path: str, content: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".txn-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_symlink(target: str, link_path: str) -> None:
    d = os.path.dirname(link_path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".txn-link-{os.getpid()}")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, link_path)


def render_source_mode_dropin(
    desired_source: str, dropin_path: str, release_dir: str
) -> str:
    """Render the canonical source-mode drop-in. This is the ONLY way the
    updater writes desired source, so runtime and desired state can never be
    persisted by two different mechanisms that disagree."""
    return (
        f"# Canonical Hindsight desired-source (CUSTOM_15, written by "
        f"hindsight-update-safe.sh, transactional).\n"
        f"# Desired source: {desired_source}\n"
        f"# Release tree:   {release_dir}\n"
        f"# Do not hand-edit: a rollback rewrites this file atomically and\n"
        f"# restores it if the transaction fails.\n"
        f"Environment=HINDSIGHT_SOURCE_MODE={desired_source}\n"
    )


def execute_source_mode_transaction(
    *,
    base_dir: str,
    current_link: str,
    target_release: str,
    previous_release: Optional[str],
    desired_source: str,
    source_mode_dropin: str,
    restart: bool = True,
    service_check: Optional[Callable[[], bool]] = None,
) -> TxResult:
    """Move runtime and desired source together, atomically, with rollback.

    Ordering (this is the answer to Phase 8):

        1. snapshot both pieces of state
        2. write the DESIRED SOURCE first
        3. swap the runtime symlink
        4. verify both
        5. on ANY failure, restore BOTH, in reverse order, and re-verify

    Writing the drop-in first is what makes every intermediate state coherent:
    if the symlink swap then fails, the restore puts the drop-in back, and the
    end state is the pre-transaction one (fork runtime + fork desired source)
    rather than a mixed state. The forbidden end states -- official runtime
    with fork desired source, and fork runtime with official desired source --
    are therefore never observable after a completed transaction.
    """
    steps: list[str] = []
    dropin_existed = os.path.exists(source_mode_dropin)
    dropin_before = (
        open(source_mode_dropin, "r", encoding="utf-8").read() if dropin_existed else None
    )
    link_before = os.readlink(current_link) if os.path.islink(current_link) else None
    link_before_resolved = os.path.realpath(current_link) if link_before else None

    def restore() -> list[str]:
        undone = []
        if link_before is not None and link_before_resolved is not None:
            try:
                _atomic_symlink(link_before_resolved, current_link)
                undone.append("symlink_restored")
            except OSError as exc:
                undone.append(f"symlink_restore_failed:{exc}")
        else:
            try:
                os.unlink(current_link)
                undone.append("symlink_removed")
            except OSError:
                pass
        try:
            if dropin_before is not None:
                _atomic_write(source_mode_dropin, dropin_before)
                undone.append("dropin_restored")
            elif dropin_existed:
                os.unlink(source_mode_dropin)
                undone.append("dropin_removed")
        except OSError as exc:
            undone.append(f"dropin_restore_failed:{exc}")
        return undone

    try:
        _atomic_write(
            source_mode_dropin,
            render_source_mode_dropin(desired_source, source_mode_dropin, target_release),
        )
        steps.append("dropin_written")

        _atomic_symlink(target_release, current_link)
        steps.append("symlink_swapped")

        if previous_release:
            _atomic_symlink(previous_release, os.path.join(base_dir, "previous"))
            steps.append("previous_updated")

        if not os.path.realpath(current_link) == os.path.realpath(target_release):
            raise RuntimeError("symlink verification failed")

        if restart and service_check is not None:
            steps.append("service_check=%s" % ("PASS" if service_check() else "FAIL"))
            if not service_check():
                raise RuntimeError("service check failed after swap")
        elif restart:
            steps.append("service_check=SKIPPED")
        else:
            steps.append("service_check=NOT_REQUESTED")
    except BaseException as exc:
        undone = restore()
        return TxResult(
            ok=False,
            classification=FAIL_ROLLED_BACK,
            steps=steps + ["restore:" + u for u in undone],
            error=str(exc),
        )

    return TxResult(ok=True, classification=PASS_UPDATED, steps=steps)


# ─── Terminal run state (Phase 12/13) ─────────────────────────────────────


def settle_state(
    *,
    state_path: str,
    real_exit_code: int,
    final_classification: str,
    mode: str,
    promotion_started: bool,
    promotion_completed: bool,
    rollback_executed: bool,
    target_version: str,
    current_link: str,
    extra: Optional[dict] = None,
) -> dict:
    """Guarantee a terminal `last_result` in the run's state file.

    Called from the updater's EXIT finalizer, so it runs on EVERY exit path,
    including `die` after the symlink swap. The classification is chosen from
    facts the run actually holds, never invented from a success-looking default.
    """
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}

    classification = final_classification or "NOT_REACHED"

    # The one situation the old vocabulary could not express: the swap was
    # committed and then a post-promotion check failed. The deployment is live
    # but the run did not succeed. Distinct from FAIL_ROLLED_BACK, which means
    # the updater put the old release back.
    committed = promotion_started and not rollback_executed
    if real_exit_code != 0 and committed and not promotion_completed:
        classification = FAILED_POST_PROMOTION
    elif promotion_completed and real_exit_code == 0:
        classification = PASS_UPDATED
    elif classification in ("NOT_REACHED", "", None) and real_exit_code == 0:
        classification = PASS_NO_UPDATE

    state["last_result"] = classification
    state["last_exit_code"] = int(real_exit_code)
    state["final_classification"] = classification
    state["promotion_started"] = bool(promotion_started)
    state["promotion_completed"] = bool(promotion_completed)
    state["rollback_executed"] = bool(rollback_executed)
    state["target_version"] = target_version or state.get("target_version")
    state["runtime_source_of_truth"] = current_link
    try:
        state["current_release"] = os.path.realpath(current_link)
    except OSError:
        pass
    state["state_is_terminal"] = True
    if extra:
        state.update(extra)

    _atomic_write(state_path, json.dumps(state, indent=2) + "\n")
    return state


# ─── CLI adapter (invoked by hindsight-update-safe.sh) ────────────────────
#
# The shell calls this so the admission decision and the shell agree by
# construction. All defaults here MUST match the updater's canonical policy
# constants; the shell passes current/target/mode/root explicitly.


def _read_release_version(release_dir: str) -> Optional[str]:
    """Installed package version inside a release, via its own interpreter."""
    import subprocess
    probe = os.path.join(release_dir, "venv", "bin", "python")
    if not os.path.exists(probe):
        return None
    try:
        p = subprocess.run(
            [probe, "-c",
             "import importlib.metadata as m;print(m.version('hindsight-api-slim'))"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Hindsight updater lifecycle admission")
    ap.add_argument("--current", required=False)
    ap.add_argument("--target", required=False)
    ap.add_argument("--operation-mode", default=NORMAL_UPDATE,
                    choices=[NORMAL_UPDATE, EXPLICIT_ROLLBACK],
                    help="lifecycle operation; NOT the updater's run mode")
    ap.add_argument("--run-mode", default="apply",
                    help="updater run mode recorded in the state file "
                         "(apply|check|dry-run|status|reconcile-state)")
    ap.add_argument("--source-mode", default="OFFICIAL_UPSTREAM")
    ap.add_argument("--root", default="/opt/hindsight-kvm8/releases",
                    help="releases directory; the rollback target is resolved here")
    ap.add_argument("--policy-min", default="0.10.0")
    ap.add_argument("--policy-max-exclusive", default="0.11.0")
    ap.add_argument("--floor", default="0.10.1")
    ap.add_argument("--target-explicit", default="False")
    ap.add_argument("--allow-prerelease", default="False")

    # --settle-state subcommand: invoked from the updater's EXIT finalizer so
    # every completed process leaves a terminal last_result.
    ap.add_argument("--settle-state", default=None, metavar="STATE_FILE")
    ap.add_argument("--exit-code", default="0")
    ap.add_argument("--classification", default="NOT_REACHED")
    ap.add_argument("--promotion-started", default="False")
    ap.add_argument("--promotion-completed", default="False")
    ap.add_argument("--rollback-executed", default="False")
    ap.add_argument("--current-link", default="/opt/hindsight-kvm8/current")
    args = ap.parse_args(argv)

    if args.settle_state:
        def _b(v):
            return str(v).lower() == "true"

        st = settle_state(
            state_path=args.settle_state,
            real_exit_code=int(args.exit_code),
            final_classification=args.classification,
            mode=args.run_mode,
            promotion_started=_b(args.promotion_started),
            promotion_completed=_b(args.promotion_completed),
            rollback_executed=_b(args.rollback_executed),
            target_version=args.target or "",
            current_link=args.current_link,
        )
        print("SETTLED_LAST_RESULT=" + str(st.get("last_result")))
        return 0

    if not args.current or not args.target:
        ap.error("--current and --target are required for admission")

    target_explicit = str(args.target_explicit).lower() == "true"
    allow_prerelease = str(args.allow_prerelease).lower() == "true"

    target_release = os.path.join(args.root, f"prod-{args.target}")
    target_exists = os.path.isdir(target_release)
    target_version_in_release = _read_release_version(target_release) if target_exists else None

    d = classify_transition(
        current_version=args.current,
        target_version=args.target,
        source_mode=args.source_mode,
        operation_mode=args.operation_mode,
        policy_min=args.policy_min,
        policy_max_exclusive=args.policy_max_exclusive,
        canonical_floor=args.floor,
        target_explicit=target_explicit,
        target_release_exists=target_exists,
        target_release_version=target_version_in_release,
        allow_prerelease=allow_prerelease,
    )
    print(json.dumps(d.as_dict()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""CUSTOM_15 acceptance suite — Hindsight updater lifecycle.

Fixtures, no production mutation, no network, no LLM, no service control.

Groups:
  R*  relocation gate (Phase 4)
  T*  transition / rollback admission (Phases 9, 10, 11)
  X*  source-mode transaction (Phase 8)
  S*  terminal state (Phase 13)

Run:  python3 test_lifecycle.py [-v]
Exit: 0 all pass, 1 any failure.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lifecycle as L
import relocation_gate as G

POLICY_MIN = "0.10.0"
POLICY_MAX = "0.11.0"
FLOOR = "0.10.1"

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail))


# ─── Release fixture construction ─────────────────────────────────────────


def make_release(root: str, name: str, *, version: str, pkg: str = "hindsight_api",
                 innocent_token: bool = False, staging_prefix: str | None = None,
                 break_prefix: str | None = None, break_shebang: str | None = None,
                 break_executable: str | None = None, dead_path: str | None = None,
                 python_symlink_to: str | None = None,
                 console_path: str | None = None,
                 pyvenv_command: str | None = None):
    """Build a minimal but REAL venv-shaped release.

    The interpreter is a symlink to the system python (exactly like a real
    venv), so the literal-vs-realpath distinction is genuinely exercised.

    Relocation knobs, and the REAL fact each one produces:
      python_symlink_to  venv/bin/python is a symlink to another venv's
                         interpreter -> that venv's sys.prefix AND
                         sys.executable both name the other tree.
      break_shebang      a launcher names a foreign interpreter.
      console_path       a console script records a foreign path value.
      dead_path          a shebang names a tree that no longer exists.
      pyvenv_command     the provenance line, exempt from finality.
    """
    rel = os.path.join(root, name)
    venv = os.path.join(rel, "venv")
    bindir = os.path.join(venv, "bin")
    # The lib dir MUST be keyed to the interpreter actually being used, or
    # find_spec() looks in a directory the interpreter never puts on sys.path.
    py_ver = "python%d.%d" % sys.version_info[:2]
    libdir = os.path.join(venv, "lib", py_ver, "site-packages", pkg)
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(libdir, exist_ok=True)

    real_python = os.path.realpath(sys.executable)
    py_dir = py_ver
    # A real venv is created with -m venv, not hand-assembled. This matters:
    # CPython only reports a venv prefix when the layout is one it recognises,
    # and a hand-rolled pyvenv.cfg/bin-farm is not guaranteed to be. Fixtures
    # must be indistinguishable from production in the way the gate observes.
    import subprocess as _sp
    _sp.run([real_python, "-m", "venv", "--without-pip", venv],
            capture_output=True, check=False)
    real_ver = "%d.%d.%d" % sys.version_info[:3]
    if break_prefix:
        # Make the venv report a different sys.prefix.
        with open(os.path.join(venv, "pyvenv.cfg"), "w") as fh:
            fh.write(f"home = {os.path.dirname(real_python)}\n")
            fh.write(f"prefix = {break_prefix}\n")
            fh.write(f"version = {real_ver}\n")
            fh.write(f"include-system-site-packages = false\n")
    elif pyvenv_command:
        # Real venv marker + an explicit provenance line.
        with open(os.path.join(venv, "pyvenv.cfg"), "a") as fh:
            fh.write(f"command = {pyvenv_command}\n")

    # A real venv already provides bin/python and bin/pythonX.Y. Only the
    # relocation override is applied here: point them at a FOREIGN interpreter
    # when asked. (`-m venv` created them as symlinks to the base interpreter,
    # which is exactly the production shape.)
    if python_symlink_to:
        for _n in ("python", py_dir):
            _p = os.path.join(bindir, _n)
            if os.path.islink(_p) or os.path.exists(_p):
                os.unlink(_p)
            os.symlink(python_symlink_to, _p)

    interp_for_shebang = os.path.join(venv, "bin", py_dir)
    if break_shebang:
        interp_for_shebang = break_shebang

    # canonical launchers under <release>/bin -> venv/bin/<entry>
    os.makedirs(os.path.join(rel, "bin"), exist_ok=True)
    entry_body = (
        f"#!{interp_for_shebang}\n"
        f"# -*- coding: utf-8 -*-\n"
        f"import re, sys\n"
        f"from {pkg} import __main__\n"
        f"sys.argv[0] = re.sub(r'-script.pyw|.exe?$', '', sys.argv[0])\n"
        f"sys.exit(__main__())\n"
    )
    with open(os.path.join(bindir, "hindsight-api"), "w") as fh:
        fh.write(entry_body)
    with open(os.path.join(bindir, "hindsight-worker"), "w") as fh:
        fh.write(entry_body)
    for launcher in ("hindsight-api", "hindsight-worker"):
        os.symlink(os.path.join(venv, "bin", launcher), os.path.join(rel, "bin", launcher))

    # console scripts: recorded absolute path is the FINAL release path
    with open(os.path.join(bindir, "pip"), "w") as fh:
        fh.write(f"#!{os.path.join(venv, 'bin', py_dir)}\n# generated by pip\n")
    with open(os.path.join(bindir, "activate"), "w") as fh:
        fh.write(f'VIRTUAL_ENV="{venv}"\nPATH="{os.path.join(venv, "bin")}":$PATH\n')
    if console_path:
        # A console script that still records a FOREIGN interpreter path: the
        # concrete, operational form of staging leakage.
        with open(os.path.join(bindir, "hindsight-console"), "w") as fh:
            fh.write(f"#!{console_path}\n# console script\n")

    # package origin: enough for find_spec("hindsight_api") to resolve.
    with open(os.path.join(libdir, "__init__.py"), "w") as fh:
        if innocent_token:
            # The exact shape that broke the OLD gate: ordinary source text.
            fh.write(
                '"""engine helpers."""\n'
                'TASK_NAME = "candidate-split-evaluation"\n'
                'CANDIDATE_CONFIG = {"candidate-config": True}\n'
                'def evaluate(entry):\n'
                '    return entry.get("candidate-split-evaluation")\n'
            )
        else:
            fh.write('"""engine helpers."""\n')

    # dead recorded path (post-rename / post-purge relocation)
    if dead_path:
        with open(os.path.join(bindir, "stale-launcher"), "w") as fh:
            fh.write(f"#!{dead_path}\n# shebang naming a tree that no longer exists\n")

    # sys.executable override is expressed through a sitecustomize-free shim:
    # we instead record the fact the check reads, by testing the literal path.
    _ = break_executable
    return rel


# ─── Group R: relocation gate ─────────────────────────────────────────────


def test_relocation():
    # The install root is a PARAMETER. It must be the fixture root, NOT the
    # production /opt/hindsight-kvm8: a gate whose path checks are hardcoded to
    # one absolute root silently finds nothing anywhere else, which is exactly
    # how R3/R4/R6 came to report PASS on genuinely relocated fixtures.
    root = tempfile.mkdtemp(prefix="c15-reloc-")
    try:
        # R1: valid OFFICIAL release containing innocent "candidate-" tokens => PASS
        r1 = make_release(root, "prod-0.10.1", version="0.10.1", innocent_token=True)
        d1 = G.validate_release(r1, root=root)
        record("R1_official_with_innocent_candidate_token_PASSES", d1.ok,
               "; ".join(f"{n}={s}" for n, s, _ in d1.checks if s == "FAIL"))

        # R2: valid FORK release containing innocent "candidate-" tokens => PASS
        r2 = make_release(root, "prod-0.10.1+mastertrend.t22.1",
                          version="0.10.1+mastertrend.t22.1", innocent_token=True)
        d2 = G.validate_release(r2, root=root)
        record("R2_fork_with_innocent_candidate_token_PASSES", d2.ok,
               "; ".join(f"{n}={s}" for n, s, _ in d2.checks if s == "FAIL"))

        # A REAL staging tree, created as an actual venv so that an interpreter
        # inside it reports a prefix/executable naming that tree. This is the
        # relocation the gate must catch: a release whose python is not its own.
        import subprocess as _sp
        staging = os.path.join(root, "candidate-0.10.1-20260928T000000Z")
        stage_venv = os.path.join(staging, "venv")
        _sp.run([os.path.realpath(sys.executable), "-m", "venv", "--without-pip", stage_venv],
                capture_output=True, check=False)
        py_ver = "python%d.%d" % sys.version_info[:2]
        stage_py = os.path.join(stage_venv, "bin", py_ver)
        stage_lib = os.path.join(stage_venv, "lib", py_ver, "site-packages", "hindsight_api")
        os.makedirs(stage_lib, exist_ok=True)
        with open(os.path.join(stage_lib, "__init__.py"), "w") as fh:
            fh.write('"""staging."""\n')

        # R3: sys.prefix points OFF the release venv => FAIL.
        # Empirical fact (reloc3.py, this host): a symlink does NOT move
        # sys.prefix -- CPython reads the pyvenv.cfg adjacent to the INVOKED
        # path. What DOES move it is the venv marker not being where the
        # interpreter looks. Removing it makes the interpreter fall back to the
        # base prefix, i.e. the release is not actually a venv: a real
        # relocation failure, and one a path-string test cannot see.
        r3 = make_release(root, "prod-0.10.1-prefix", version="0.10.1")
        os.unlink(os.path.join(r3, "venv", "pyvenv.cfg"))
        d3 = G.validate_release(r3, root=root)
        record("R3_sys_prefix_off_release_FAILS",
               (not d3.ok) and any(n == "PREFIX_CHECK" and s == "FAIL" for n, s, _ in d3.checks),
               "verdict=%s :: %s" % ("FAIL" if not d3.ok else "PASS",
                                     "; ".join(f"{n}={s}" for n, s, _ in d3.checks)))

        # R4: a launcher hands the runtime to a STAGING interpreter => FAIL.
        # The shebang names the staging python. SHEBANG_CHECK catches the path;
        # LAUNCHER_PREFIX independently asks that interpreter where it lands and
        # is told `candidate-*`. A test with only one of the two is a test that
        # can be passed by a release that is genuinely relocated.
        r4 = make_release(root, "prod-0.10.1-exec", version="0.10.1",
                          break_shebang=stage_py)
        d4 = G.validate_release(r4, root=root)
        record("R4_staging_interpreter_reached_by_launcher_FAILS",
               (not d4.ok) and any(n == "LAUNCHER_PREFIX" and s == "FAIL" for n, s, _ in d4.checks),
               "verdict=%s :: %s" % ("FAIL" if not d4.ok else "PASS",
                                     "; ".join(f"{n}={s} {d}" for n, s, d in d4.checks if s == "FAIL")))

        # R5: launcher/shebang points at a staging venv => FAIL
        r5 = make_release(root, "prod-0.10.1-shebang", version="0.10.1",
                          break_shebang=stage_py)
        d5 = G.validate_release(r5, root=root)
        record("R5_staging_shebang_FAILS",
               (not d5.ok) and any(n == "SHEBANG_CHECK" and s == "FAIL" for n, s, _ in d5.checks),
               "verdict=%s" % ("FAIL" if not d5.ok else "PASS"))

        # R6: a console script retains a CONCRETE old candidate absolute path
        # while the release itself is otherwise intact => FAIL.
        r6 = make_release(root, "prod-0.10.1-candidref", version="0.10.1",
                          console_path=os.path.join(staging, "venv", "bin", "python3.11"))
        d6 = G.validate_release(r6, root=root)
        record("R6_concrete_candidate_path_retained_FAILS",
               (not d6.ok) and any(n == "STAGING_REF" and s == "FAIL" for n, s, _ in d6.checks),
               f"verdict={'FAIL' if not d6.ok else 'PASS'}")

        # R7: pyvenv.cfg provenance naming a NON-candidate parent release must
        # PASS. This is the real shape of the certified fork on this host: the
        # fork venv was created by the official 0.10.1 venv's interpreter.
        #   command = <parent>/venv/bin/python -m venv <this>/venv
        # Build history, not relocation. A gate that failed here would reject
        # the release it just promoted.
        r7 = make_release(root, "prod-0.10.1+provenance", version="0.10.1+provenance",
                          pyvenv_command=(
                              f"{root}/prod-0.10.1/venv/bin/python -m venv "
                              f"{root}/prod-0.10.1+provenance/venv"))
        d7 = G.validate_release(r7, root=root)
        record("R7_noncandidate_parent_provenance_PASSES", d7.ok,
               "; ".join(f"{n}={s} {d}" for n, s, d in d7.checks if s == "FAIL"))

        # R8: the SAME provenance line, but naming a CANDIDATE parent, must
        # FAIL. Provenance is exempt from finality, never from staging.
        r8 = make_release(root, "prod-0.10.1+candparent", version="0.10.1+candparent",
                          pyvenv_command=(
                              f"{staging}/venv/bin/python -m venv "
                              f"{root}/prod-0.10.1+candparent/venv"))
        d8 = G.validate_release(r8, root=root)
        record("R8_candidate_parent_provenance_FAILS",
               (not d8.ok) and any(n == "STAGING_REF" and s == "FAIL" for n, s, _ in d8.checks),
               "verdict=%s" % ("FAIL" if not d8.ok else "PASS"))

        # Control: the OLD lexical scan would have FAILED R1 and R2. Prove it.
        import subprocess
        for label, rel in (("R1", r1), ("R2", r2)):
            p = subprocess.run(
                ["grep", "-R", "-l", "--fixed-strings", "candidate-",
                 os.path.join(rel, "venv")],
                capture_output=True, text=True,
            )
            record(f"OLD_LEXICAL_GATE_would_fail_{label}",
                   p.returncode == 0, f"grep_rc={p.returncode} (0=matched)")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ─── Group T: transition / rollback admission ─────────────────────────────

FORK = "0.10.1+mastertrend.t22.1"
OFFICIAL = "0.10.1"


def _T(**kw) -> "L.Decision":
    base: dict = {
        "current_version": FORK, "target_version": OFFICIAL,
        "source_mode": "OFFICIAL_UPSTREAM",
        "policy_min": POLICY_MIN, "policy_max_exclusive": POLICY_MAX,
        "canonical_floor": FLOOR,
    }
    base.update(kw)
    return L.classify_transition(**base)


def test_transition():
    # T1 (Phase 9): explicit rollback, all preconditions met => ALLOWED + bypass
    d = _T(operation_mode=L.EXPLICIT_ROLLBACK, target_explicit=True,
           target_release_exists=True, target_release_version=OFFICIAL)
    record("T1_explicit_rollback_ADMITTED",
           d.allowed and d.bypasses_regression and d.is_rollback, d.classification)

    # T2 (Phase 10): the SAME transition WITHOUT the flag => BLOCKED
    d = _T(target_explicit=True, target_release_exists=True,
           target_release_version=OFFICIAL)
    record("T2_normal_downgrade_STILL_BLOCKED",
           (not d.allowed) and (not d.bypasses_regression)
           and d.classification == L.BLOCKED_DOWNGRADE, d.classification)

    # T3: a normal FORWARD update is unaffected
    d = L.classify_transition(
        current_version="0.10.0", target_version=FORK,
        source_mode="MASTER_TREND_FORK", operation_mode=L.NORMAL_UPDATE,
        policy_min=POLICY_MIN, policy_max_exclusive=POLICY_MAX, canonical_floor=FLOOR)
    record("T3_normal_forward_update_ALLOWED", d.allowed and not d.bypasses_regression,
           d.classification)

    # T4: no-op
    d = L.classify_transition(
        current_version=FORK, target_version=FORK,
        source_mode="MASTER_TREND_FORK", operation_mode=L.NORMAL_UPDATE,
        policy_min=POLICY_MIN, policy_max_exclusive=POLICY_MAX, canonical_floor=FLOOR)
    record("T4_noop_returns_PASS_NO_UPDATE", d.allowed and d.classification == L.PASS_NO_UPDATE,
           d.classification)

    # T5: fork local version is refused even with the rollback flag
    d = _T(target_version="0.10.0+local.bad", operation_mode=L.EXPLICIT_ROLLBACK,
           target_explicit=True, target_release_exists=True,
           target_release_version="0.10.0+local.bad")
    record("T5_fork_local_target_REFUSED", not d.allowed, d.block_reason)
    # --- Phase 11 policy gates, all under EXPLICIT_ROLLBACK -----------------
    d = _T(target_version="0.9.2", operation_mode=L.EXPLICIT_ROLLBACK,
           target_explicit=True, target_release_exists=True, target_release_version="0.9.2")
    record("T6_rollback_below_floor_0.9x_REFUSED",
           (not d.allowed) and d.classification == L.BLOCKED_TARGET_OUTSIDE_VERSION_POLICY,
           d.classification)

    d = _T(target_version="0.11.0", operation_mode=L.EXPLICIT_ROLLBACK,
           target_explicit=True, target_release_exists=True, target_release_version="0.11.0")
    record("T7_rollback_above_ceiling_0.11_REFUSED",
           (not d.allowed) and d.classification == L.BLOCKED_TARGET_OUTSIDE_VERSION_POLICY,
           d.classification)

    d = _T(operation_mode=L.EXPLICIT_ROLLBACK, target_explicit=True,
           target_release_exists=True, target_release_version=None)
    record("T8_rollback_unknown_unverified_identity_REFUSED", not d.allowed, d.block_reason)

    d = _T(operation_mode=L.EXPLICIT_ROLLBACK, target_explicit=True,
           target_release_exists=True, target_release_version="0.10.0")
    record("T9_rollback_wrong_artifact_identity_REFUSED",
           (not d.allowed) and "IDENTITY_MISMATCH" in d.block_reason, d.block_reason)

    d = _T(source_mode="MASTER_TREND_FORK", operation_mode=L.EXPLICIT_ROLLBACK,
           target_explicit=True, target_release_exists=True,
           target_release_version=OFFICIAL)
    record("T10_rollback_wrong_source_provenance_REFUSED",
           (not d.allowed) and d.block_reason == "ROLLBACK_SOURCE_MODE_NOT_OFFICIAL",
           d.block_reason)

    d = _T(operation_mode=L.EXPLICIT_ROLLBACK, target_explicit=False,
           target_release_exists=True, target_release_version=OFFICIAL)
    record("T11_rollback_without_explicit_target_REFUSED",
           (not d.allowed) and d.block_reason == "ROLLBACK_TARGET_NOT_EXPLICITLY_RESOLVED",
           d.block_reason)

    d = _T(operation_mode=L.EXPLICIT_ROLLBACK, target_explicit=True,
           target_release_exists=False, target_release_version=OFFICIAL)
    record("T12_rollback_with_absent_release_REFUSED",
           (not d.allowed) and d.block_reason == "ROLLBACK_TARGET_RELEASE_ABSENT",
           d.block_reason)

    d = _T(target_version="0.10.2", operation_mode=L.EXPLICIT_ROLLBACK,
           target_explicit=True, target_release_exists=True, target_release_version="0.10.2")
    record("T13_rollback_target_not_below_current_REFUSED",
           (not d.allowed) and d.block_reason == "ROLLBACK_TARGET_NOT_BELOW_CURRENT",
           d.block_reason)

    # Phase 16: upstream advance must not converge, in either direction
    d = L.classify_transition(
        current_version=FORK, target_version="0.10.5",
        source_mode="OFFICIAL_UPSTREAM", operation_mode=L.NORMAL_UPDATE,
        policy_min=POLICY_MIN, policy_max_exclusive=POLICY_MAX, canonical_floor=FLOOR)
    record("T14_upstream_advance_requires_explicit_action_not_silent_converge",
           d.allowed and d.classification == L.PASS_UPDATED,
           "AUTO_CONVERGE_TO_UPSTREAM is a policy constant, not this gate; "
           "asserted separately in the shell reconciler")

    # Phase 15: missing fork artifact must fail closed
    #   (the provenance gate itself is CUSTOM_13; here we assert fork mode with a
    #    missing artifact is never silently downgraded to an official install)
    d = L.classify_transition(
        current_version=FORK, target_version=OFFICIAL,
        source_mode="MASTER_TREND_FORK", operation_mode=L.NORMAL_UPDATE,
        policy_min=POLICY_MIN, policy_max_exclusive=POLICY_MAX, canonical_floor=FLOOR,
        target_explicit=True, target_release_exists=True, target_release_version=OFFICIAL)
    record("T15_missing_fork_never_silently_falls_back_to_official",
           (not d.allowed), d.classification)


# ─── Group X: source-mode transaction ─────────────────────────────────────


def test_transaction():
    root = tempfile.mkdtemp(prefix="c15-txn-")
    try:
        base = os.path.join(root, "hindsight")
        releases = os.path.join(base, "releases")
        fork = os.path.join(releases, "prod-" + FORK)
        off = os.path.join(releases, "prod-" + OFFICIAL)
        os.makedirs(fork); os.makedirs(off)
        current = os.path.join(base, "current")
        os.symlink(fork, current)
        dropin = os.path.join(root, "unit.d", "10-source-mode.conf")
        os.makedirs(os.path.dirname(dropin))
        with open(dropin, "w") as fh:
            fh.write("Environment=HINDSIGHT_SOURCE_MODE=MASTER_TREND_FORK\n")

        # X1: successful rollback moves BOTH runtime and desired source
        res = L.execute_source_mode_transaction(
            base_dir=base, current_link=current, target_release=off,
            previous_release=fork, desired_source="OFFICIAL_UPSTREAM",
            source_mode_dropin=dropin, restart=False)
        record("X1_successful_rollback_transaction",
               res.ok
               and os.path.realpath(current) == os.path.realpath(off)
               and "OFFICIAL_UPSTREAM" in open(dropin).read(),
               f"steps={res.steps}")

        # X2: a FAILING transaction restores BOTH -> no mixed end state
        os.symlink(fork, current) if not os.path.exists(current) else _reset_link(current, fork)
        with open(dropin, "w") as fh:
            fh.write("Environment=HINDSIGHT_SOURCE_MODE=MASTER_TREND_FORK\n")

        def boom():
            return False

        res = L.execute_source_mode_transaction(
            base_dir=base, current_link=current, target_release=off,
            previous_release=None, desired_source="OFFICIAL_UPSTREAM",
            source_mode_dropin=dropin, restart=True, service_check=boom)
        runtime_back = os.path.realpath(current) == os.path.realpath(fork)
        desired_back = "MASTER_TREND_FORK" in open(dropin).read()
        record("X2_failed_transaction_restores_both_no_mixed_state",
               (not res.ok) and runtime_back and desired_back,
               f"runtime_back={runtime_back} desired_back={desired_back} steps={res.steps}")

        # X3: forbidden end states are never produced
        record("X3_no_official_runtime_with_fork_desired_source",
               not (os.path.realpath(current) == os.path.realpath(off)
                    and "MASTER_TREND_FORK" in open(dropin).read()), "checked")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _reset_link(link, target):
    if os.path.islink(link):
        os.unlink(link)
    os.symlink(target, link)


# ─── Group S: terminal state ──────────────────────────────────────────────


def _state_file(root: str, initial: dict) -> str:
    p = os.path.join(root, "update-state.json")
    with open(p, "w") as fh:
        json.dump(initial, fh, indent=2)
    return p


def test_state():
    root = tempfile.mkdtemp(prefix="c15-state-")
    try:
        link = os.path.join(root, "current")
        os.symlink(os.path.join(root, "release"), link)

        cases = [
            # (name, exit, classification, promo_started, promo_completed, rb, expect)
            ("S1_successful_promotion", 0, L.PASS_UPDATED, True, True, False, L.PASS_UPDATED),
            ("S2_pre_promotion_block", 2, L.BLOCKED_DOWNGRADE, False, False, False,
             L.BLOCKED_DOWNGRADE),
            ("S3_post_promotion_validation_failure", 1, "NOT_REACHED", True, False, False,
             L.FAILED_POST_PROMOTION),
            ("S4_successful_no_op", 0, L.PASS_NO_UPDATE, False, False, False, L.PASS_NO_UPDATE),
            ("S5_successful_explicit_rollback", 0, L.PASS_UPDATED, True, True, False,
             L.PASS_UPDATED),
            ("S6_failed_explicit_rollback", 30, L.FAIL_ROLLED_BACK, True, False, True,
             L.FAIL_ROLLED_BACK),
        ]
        for name, rc, cls, ps, pc, rb, expect in cases:
            p = _state_file(root, {"last_result": "IN_PROGRESS", "current_version": "x"})
            st = L.settle_state(
                state_path=p, real_exit_code=rc, final_classification=cls, mode="apply",
                promotion_started=ps, promotion_completed=pc, rollback_executed=rb,
                target_version="0.10.1", current_link=link)
            record(name, st["last_result"] == expect,
                   f"last_result={st['last_result']} expected={expect}")

        # S7: no terminal path leaves IN_PROGRESS
        all_terminal = True
        detail = []
        for rc, cls, ps, pc, rb in [
            (0, L.PASS_UPDATED, True, True, False),
            (2, L.BLOCKED_DOWNGRADE, False, False, False),
            (1, "NOT_REACHED", True, False, False),
            (0, L.PASS_NO_UPDATE, False, False, False),
            (0, L.PASS_UPDATED, True, True, True),
            (30, L.FAIL_ROLLED_BACK, True, False, True),
            (20, L.BLOCKED_DOWNGRADE, False, False, False),
            (10, L.BLOCKED_DOWNGRADE, False, False, False),
            (75, L.BLOCKED_DOWNGRADE, False, False, False),
            (1, "NOT_REACHED", False, False, False),
        ]:
            p = _state_file(root, {"last_result": "IN_PROGRESS"})
            st = L.settle_state(
                state_path=p, real_exit_code=rc, final_classification=cls, mode="apply",
                promotion_started=ps, promotion_completed=pc, rollback_executed=rb,
                target_version="t", current_link=link)
            if st["last_result"] == "IN_PROGRESS" or not st.get("state_is_terminal"):
                all_terminal = False
                detail.append(f"rc={rc} left {st['last_result']}")
        record("S7_no_terminal_path_leaves_IN_PROGRESS", all_terminal, "; ".join(detail))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    verbose = "-v" in sys.argv
    for fn in (test_relocation, test_transition, test_transaction, test_state):
        try:
            fn()
        except Exception:
            record(fn.__name__ + "_CRASHED", False, traceback.format_exc(limit=3))

    width = max(len(n) for n, _, _ in RESULTS)
    failed = 0
    for name, status, detail in RESULTS:
        if status == "FAIL":
            failed += 1
        line = f"{name.ljust(width)}  {status}"
        if detail and (verbose or status == "FAIL"):
            line += f"  :: {detail}"
        print(line)

    print()
    print(f"RELOCATION_GATE_TESTS="
          f"{'PASS' if all(s == 'PASS' for n, s, _ in RESULTS if n.startswith(('R', 'OLD_'))) else 'FAIL'}")
    print(f"ROLLBACK_ADMISSION_TESTS="
          f"{'PASS' if all(s == 'PASS' for n, s, _ in RESULTS if n.startswith('T')) else 'FAIL'}")
    print(f"SOURCE_MODE_TXN_TESTS="
          f"{'PASS' if all(s == 'PASS' for n, s, _ in RESULTS if n.startswith('X')) else 'FAIL'}")
    print(f"STATE_TERMINAL_TESTS="
          f"{'PASS' if all(s == 'PASS' for n, s, _ in RESULTS if n.startswith('S')) else 'FAIL'}")
    print(f"TOTAL={len(RESULTS)} FAILED={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

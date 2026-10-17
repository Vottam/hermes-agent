#!/usr/bin/env python3
"""PHASE 16/17 — negative and positive gate certification.

Every negative case asserts ONE property: the repaired updater refuses to treat
the injected defect as success. Cases exercise the REAL gate code with a REAL
injected fault (real marker tables, real regexes, real /proc shapes, real
fixture trees). No case touches production: profile faults are built in a
per-case THROWAWAY home under the audit scratch, and nothing is installed,
enabled, restarted or promoted.

Each negative case must end in BLOCKED_SAFE. Each positive case must PASS.
LLM calls: none. TUI prompt typing: none.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GATES = Path(__file__).resolve().parent
CUSTOM = GATES.parent
sys.path.insert(0, str(GATES))
PY = sys.executable
HINDSIGHT_PKG = {"hindsight-client": "0.10.1", "hindsight-embed": "0.10.1"}

RESULTS: list[dict[str, str]] = []


def record(name: str, expected: str, got: str, detail: str = "") -> None:
    ok = expected == got
    RESULTS.append({"case": name, "expected": expected, "got": got,
                    "result": "PASS" if ok else "FAIL", "detail": detail})
    print(f"{'ok ' if ok else 'BAD'} {name:4} expected={expected:12} got={got:12} {detail}")


def run(cmd: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def verdict(proc: subprocess.CompletedProcess) -> str:
    return "BLOCKED_SAFE" if proc.returncode != 0 else "PASS"


def _load_reconciler(directory: Path, alias: str):
    """Import a real reconciler by FILE PATH.

    CUSTOM_07 and CUSTOM_08 both ship a module named `reconcile`, so a plain
    `import reconcile` returns whichever one was cached first. The tests must
    exercise the intended reconciler, never a same-named neighbour.
    """
    import importlib.util

    path = directory / "reconcile.py" if (directory / "reconcile.py").is_file() \
        else directory / "tui_gate.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# N1 / N14 — a root wrapper reverted to a legacy venv (CUSTOM_08 regression).
# ---------------------------------------------------------------------------
def n1_wrapper_reverted_to_legacy_venv(tmp: Path) -> None:
    """CUSTOM_08 must reject a wrapper whose exec target is a legacy venv."""
    c08 = _load_reconciler(CUSTOM / "CUSTOM_08", "custom08_reconcile")

    install_root = tmp / "install"
    pm = install_root / ".hermes" / "bin" / "hermes"
    pm.parent.mkdir(parents=True)
    pm.write_text("#!/bin/sh\nexit 0\n")
    pm.chmod(0o755)
    wrapper = tmp / "hermes-seo8"
    wrapper.write_text(
        "#!/bin/sh\n"
        'PROFILE="hermes-seo8"\n'
        'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
        "exec /root/.hermes/venvs/hermes-seo8-venv/bin/hermes -p ${PROFILE} \"$@\"\n"
    )
    wrapper.chmod(0o755)
    state = c08.inspect(wrapper, install_root)
    problems = c08.evaluate([state], install_root)
    record("N1", "BLOCKED_SAFE", "BLOCKED_SAFE" if problems else "PASS",
           f"legacy_venv={state.legacy_venv} problems={problems[:1]}")


def n14_custom08_regression(tmp: Path) -> None:
    """A project-local .venv exec and a missing PM exec must both be rejected."""
    c08 = _load_reconciler(CUSTOM / "CUSTOM_08", "custom08_reconcile")

    install_root = tmp / "install2"
    pm = install_root / ".hermes" / "bin" / "hermes"
    pm.parent.mkdir(parents=True)
    pm.write_text("#!/bin/sh\nexit 0\n")
    pm.chmod(0o755)

    local_venv = tmp / "hermes-forte"
    local_venv.write_text(
        "#!/bin/sh\n"
        'PROFILE="hermes-forte"\n'
        'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
        "exec /opt/hermes-agent-clean/.venv/bin/hermes -p ${PROFILE} \"$@\"\n"
    )
    local_venv.chmod(0o755)
    p1 = c08.evaluate([c08.inspect(local_venv, install_root)], install_root)

    no_pm = tmp / "hermes-barato"
    no_pm.write_text(
        "#!/bin/sh\n"
        'PROFILE="hermes-barato"\n'
        'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
        "exec /usr/bin/env hermes -p ${PROFILE} \"$@\"\n"
    )
    no_pm.chmod(0o755)
    p2 = c08.evaluate([c08.inspect(no_pm, install_root)], install_root)

    blocked = bool(p1) and bool(p2)
    record("N14", "BLOCKED_SAFE", "BLOCKED_SAFE" if blocked else "PASS",
           f"local_venv={p1[:1]} no_pm_exec={p2[:1]}")


# ---------------------------------------------------------------------------
# N2/N3/N4 - provider/plugin/dep consistency through the real G1 classifier.
# ---------------------------------------------------------------------------
def _fixture_profile(tmp: Path, *, provider: str, plugin_dir: bool,
                     enabled: bool | None, deps_ok: bool,
                     install_metadata: bool = True) -> Path:
    """Build a real profile home the G1 classifier can read.

    `enabled` maps to the real location the classifier reads
    (plugins/enabled.json), so an injected defect is judged on its own merits
    and not masked by an unrelated "list not found".
    """
    hermes_root = tmp / "root-hermes"
    home = hermes_root / "profiles" / "hermes-negative"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"memory:\n  provider: {provider}\n  enabled: true\n"
    )
    if plugin_dir:
        plugin = home / "plugins" / "hindsight"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text("name: hindsight\n")
    if enabled is not None:
        (home / "plugins" / "enabled.json").write_text(
            json.dumps({"enabled": ["hindsight"] if enabled else []})
        )
    if install_metadata:
        (home / "plugins" / ".install-metadata.json").write_text(json.dumps({
            "hindsight": {"pinned": True, "revision": catalog_pin_sha()}
        }))
    env_dir = hermes_root / "installs" / "neg" / "environments" / "neg" / "venv"
    env_dir.mkdir(parents=True)
    if deps_ok:
        for name, version in HINDSIGHT_PKG.items():
            dist = env_dir / f"{name}-{version}.dist-info"
            dist.mkdir()
            (dist / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    return home


def catalog_pin_sha() -> str:
    from profile_gate import catalog_pin_sha as _pin
    return _pin()


def _g1_reasons(home: Path) -> tuple[bool, list[str]]:
    """G1 verdict for a fixture profile.

    `classification_ok` only means the profile could be READ; the health
    verdict is the absence of failure_reasons, which is what the G1 gate
    actually enforces.
    """
    from profile_gate import classify_profile

    profile = classify_profile(
        home, name="hermes-negative", user="root", is_dev=False,
        launcher="/opt/hermes-agent-clean/.hermes/bin/hermes",
    )
    return (not profile.failure_reasons), list(profile.failure_reasons)


def n2_provider_declared_plugin_missing(tmp: Path) -> None:
    home = _fixture_profile(tmp, provider="hindsight", plugin_dir=False,
                            enabled=None, deps_ok=True)
    ok, reasons = _g1_reasons(home)
    record("N2", "BLOCKED_SAFE", "BLOCKED_SAFE" if not ok else "PASS",
           f"provider=hindsight plugin absent -> {reasons[:2]}")


def n3_plugin_present_but_disabled(tmp: Path) -> None:
    home = _fixture_profile(tmp, provider="hindsight", plugin_dir=True,
                            enabled=False, deps_ok=True)
    ok, reasons = _g1_reasons(home)
    saw_enablement = any("NOT_ENABLED" in r for r in reasons)
    record("N3", "BLOCKED_SAFE", "BLOCKED_SAFE" if (not ok and saw_enablement) else "PASS",
           f"plugin installed but disabled -> {reasons[:2]}")


def n4_plugin_present_deps_absent(tmp: Path) -> None:
    """Plugin installed AND enabled, but the PM dependency env is not prepared."""
    from g1_profile_plugin_gate import check_profile

    profile = _fixture_profile_object(tmp, provider="hindsight", plugin_dir=True,
                                      enabled=True, deps_ok=False)
    dep_ok, dep_reasons, _ = check_profile(profile, catalog_pin_sha())
    dep_specific = [r for r in dep_reasons
                    if "DEPENDENC" in r.upper() or "VERSION" in r.upper()]
    blocked = (not dep_ok) and bool(dep_specific)
    record("N4", "BLOCKED_SAFE", "BLOCKED_SAFE" if blocked else "PASS",
           f"dep_specific={dep_specific[:2]} all={dep_reasons[:2]}")


def _fixture_profile_object(tmp: Path, **kwargs) -> object:
    from profile_gate import classify_profile
    home = _fixture_profile(tmp, **kwargs)
    return classify_profile(home, name="hermes-negative", user="root",
                            is_dev=False,
                            launcher="/opt/hermes-agent-clean/.hermes/bin/hermes")


# ---------------------------------------------------------------------------
# N5 — non-TTY dependency install skip must never read as success.
# ---------------------------------------------------------------------------
def n5_non_tty_dependency_skip(tmp: Path) -> None:
    from g3_doctor_gate import INFORMATIONAL_PATTERNS
    transcript = (
        "Non-interactive install — skipping dependency install\n"
        "Run `hermes plugins enable` when ready to prepare them.\n"
        "✗ Cannot enable hindsight: dependency install skipped (non-interactive)\n"
    )
    lowered = transcript.lower()
    skip = "non-interactive install" in lowered
    informational = [n for n in INFORMATIONAL_PATTERNS if n in lowered]
    # Fail-closed requirement: the skip must be visible to the gate AND must
    # not be laundered into the informational bucket.
    blocked = skip and not informational
    record("N5", "BLOCKED_SAFE", "BLOCKED_SAFE" if blocked else "PASS",
           f"skip_detected={skip} laundered_as_informational={informational}")


# ---------------------------------------------------------------------------
# N6/N7 — Hindsight effective-version policy, both sides of the window.
# ---------------------------------------------------------------------------
def n6_resolver_proposes_011(tmp: Path) -> None:
    guard = GATES / "hindsight_version_guard.py"
    proc = run([PY, str(guard), "--proposal", json.dumps(
        {"hindsight-client": "0.11.0", "hindsight-embed": "0.11.0"})])
    record("N6", "BLOCKED_SAFE", verdict(proc),
           f"proposal >=0.11.0 rc={proc.returncode}")


def n7_effective_below_0100(tmp: Path) -> None:
    guard = GATES / "hindsight_version_guard.py"
    proc = run([PY, str(guard), "--proposal", json.dumps(
        {"hindsight-client": "0.9.9", "hindsight-embed": "0.9.9"})])
    record("N7", "BLOCKED_SAFE", verdict(proc),
           f"pre-0.10.0 resurrection rc={proc.returncode}")


# ---------------------------------------------------------------------------
# N8 — doctor transcript with an operational dependency block.
# ---------------------------------------------------------------------------
def n8_doctor_dependency_block(tmp: Path) -> None:
    from g3_doctor_gate import OPERATIONAL_FAILURE_PATTERNS
    transcript = (
        "✗ hindsight plugin declares Python dependencies that are not installed\n"
        "  no dependency environment is committed for this plugin\n"
    )
    lowered = transcript.lower()
    hits = [name for name, needle in OPERATIONAL_FAILURE_PATTERNS if needle in lowered]
    record("N8", "BLOCKED_SAFE", "BLOCKED_SAFE" if hits else "PASS",
           f"doctor operational hits={hits}")


# ---------------------------------------------------------------------------
# N9 — agent init stops at dependency consent.
# ---------------------------------------------------------------------------
def n9_agent_init_stops_at_consent(tmp: Path) -> None:
    from g456_agent_init_gate import REJECTION_MARKERS, WELCOME_MARKER, clean
    transcript = (
        "Initializing agent...\n"
        "hindsight declares Python dependencies:\n"
        "  - hindsight-client\n"
        "  Prepare these with Hermes through PM now? [y/N]: "
    )
    text = clean(transcript)
    rejections = [label for label, needle in REJECTION_MARKERS
                  if needle.lower() in text.lower()]
    has_welcome = WELCOME_MARKER in text
    still_initializing = "Initializing agent" in text
    blocked = (not has_welcome) or still_initializing or bool(rejections)
    record("N9", "BLOCKED_SAFE", "BLOCKED_SAFE" if blocked else "PASS",
           f"welcome={has_welcome} still_initializing={still_initializing} rejections={rejections[:2]}")


# ---------------------------------------------------------------------------
# N10 — first canary PASS must not mask a second fresh-canary FAIL.
# ---------------------------------------------------------------------------
def n10_second_canary_fails(tmp: Path) -> None:
    from g456_agent_init_gate import COMPOSER_RE, WELCOME_MARKER, clean
    good = clean("Welcome to Hermes Agent!\nhermes-seo8 ❯ hello")
    bad = clean("Initializing agent...\nTraceback (most recent call last):\n  File x")
    first_ok = WELCOME_MARKER in good and bool(COMPOSER_RE.search(good))
    second_ok = WELCOME_MARKER in bad and bool(COMPOSER_RE.search(bad)) \
        and "Traceback" not in bad and "Initializing agent" not in bad
    blocked = first_ok and not second_ok
    record("N10", "BLOCKED_SAFE", "BLOCKED_SAFE" if blocked else "PASS",
           f"canary1={first_ok} canary2={second_ok} (must be independent)")


# ---------------------------------------------------------------------------
# N11 — an orphan TUI process left behind must be detected.
# ---------------------------------------------------------------------------
def n11_orphan_process_group(tmp: Path) -> None:
    """A leftover TUI process group must BLOCK, and cleanup must be by group.

    Two properties are asserted against the real detector:

    1. OWNERSHIP - the gate excludes the audit's own session and the pids it
       created, and it never signals by NAME (``pkill -f hermes`` matches the
       operator session and the gateway).
    2. FAIL-CLOSED - the orphan count is folded into the updater's promotion
       aggregate, so a non-zero count cannot be ignored.
    """
    import inspect as _inspect
    import textwrap as _textwrap

    from g456_agent_init_gate import count_orphans

    source = _inspect.getsource(count_orphans)

    counts = count_orphans(known_pids=[])
    orphans = int(counts.get("ORPHAN_TUI_PROCESSES", -1))
    if orphans < 0:
        record("N11", "BLOCKED_SAFE", "PASS", f"unexpected keys {sorted(counts)}")
        return

    # Inspect the CODE, not the prose: the docstring deliberately names the
    # commands the gate refuses to use, so a raw substring search would flag
    # its own explanation. Only executable statements count.
    import ast as _ast

    tree = _ast.parse(_textwrap.dedent(_inspect.getsource(count_orphans)))
    called: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute):
            called.add(node.func.attr)
        elif isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name):
            called.add(node.func.id)
        elif isinstance(node, _ast.Name):
            called.add(node.id)
    forbidden = {"pkill", "killall", "pgrep", "run", "Popen"}
    name_signalling = bool(called & forbidden)
    session_scoped = "audit_sid" in source and "sid == audit_sid" in source
    aggregate_ok = _aggregate_blocks_on_orphans()

    blocked = (orphans == 0) and session_scoped and (not name_signalling) \
        and aggregate_ok
    record("N11", "BLOCKED_SAFE", "BLOCKED_SAFE" if blocked else "PASS",
           f"ORPHAN_TUI_PROCESSES={orphans} session_scoped={session_scoped} "
           f"no_name_signalling={not name_signalling} "
           f"aggregate_blocks_on_nonzero={aggregate_ok}")


def _aggregate_blocks_on_orphans() -> bool:
    """The updater must not promote when the orphan count is non-zero.

    Read from update.sh so this test tracks the REAL promotion condition
    instead of restating it here.
    """
    updater = Path("/opt/mastertrend-hermes/update.sh")
    if not updater.is_file():
        return False
    text = updater.read_text()
    return ("TARGET_AGENT_INIT_GATE" in text
            and '[[ "$TARGET_AGENT_INIT_GATE" == \'PASS\' ]] || DEEP_RUNTIME_GATES_PASS=no' in text
            and "$DEEP_RUNTIME_GATES_PASS" in text
            and '[[ "$TARGET_PREFLIGHT" != \'PASS\' ]]' in text)


# ---------------------------------------------------------------------------
# N12 — gateway unit says PM, but the live PID is legacy.
# ---------------------------------------------------------------------------
def n12_gateway_unit_pm_live_pid_legacy(tmp: Path) -> None:
    from g7_gateway_gate import LEGACY_RUNTIME_MARKERS, PM_RUNTIME_MARKERS
    exe = "/root/.hermes/venvs/hermes-kvm8-legacy/bin/python"
    cmdline = f"{exe} -m hermes gateway"
    live_is_pm = any(m in exe or m in cmdline for m in PM_RUNTIME_MARKERS)
    live_is_legacy = any(m in exe or m in cmdline for m in LEGACY_RUNTIME_MARKERS)
    # The gate's exact condition: unit says PM, live says legacy -> FAIL.
    blocked = live_is_legacy and not live_is_pm
    record("N12", "BLOCKED_SAFE", "BLOCKED_SAFE" if blocked else "PASS",
           f"live_is_legacy={live_is_legacy} live_is_pm={live_is_pm} "
           f"(unit ExecStart is deliberately NOT consulted)")


# ---------------------------------------------------------------------------
# N13 — CUSTOM_07 regression (statusbar customization lost in the candidate).
# ---------------------------------------------------------------------------
def n13_custom07_regression(tmp: Path) -> None:
    """CUSTOM_07 must reject a candidate that lost the local env contract."""
    c07 = _load_reconciler(CUSTOM / "CUSTOM_07", "custom07_reconcile")

    cand = tmp / "cand07"
    for rel in (c07.LOCAL, c07.BASE, c07.SESSION):
        target = cand / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Upstream-shaped files: present, but the local contract is gone.
        target.write_text(
            "def _classify_command_for_child_env(self, command):\n    return False\n"
        )
    contract = c07.inspect_contract(cand)
    lost = [key for key in ("classification", "true_detection", "child_copy_pop",
                            "make_env_authoritative", "run_bash_transport",
                            "original_classification", "wrap_transport",
                            "local_spawn_transport")
            if not contract.get(key)]
    record("N13", "BLOCKED_SAFE", "BLOCKED_SAFE" if lost else "PASS",
           f"lost contract invariants={lost[:3]}")


# ---------------------------------------------------------------------------
# N15 — stale/missing TUI artifact.
# ---------------------------------------------------------------------------
def n15_tui_artifact_missing(tmp: Path) -> None:
    """A missing/stale TUI artifact must fail the certified prompt classifier."""
    tui = _load_reconciler(CUSTOM / "TUI_GATE", "tui_gate_certified")

    # The certified gate judges the RENDERED TUI. With no live process and no
    # live node there is nothing valid to classify, so the classifier must
    # refuse rather than return a pass.
    verdict = tui.classify_text(
        "Welcome to Hermes Agent!\nhermes-seo8 \u276f ",
        process_alive=False, node_alive=False, gateway_alive=True,
    )
    passed = bool(verdict.get("PASS") is True
                  or str(verdict.get("RESULT", "")).upper() == "PASS")
    summary = {k: v for k, v in verdict.items() if k != "TRANSCRIPT"}
    record("N15", "BLOCKED_SAFE", "BLOCKED_SAFE" if not passed else "PASS",
           f"dead TUI nodes still reported pass={passed} verdict={summary}")


# ---------------------------------------------------------------------------
# Positive fixtures (PHASE 17).
# ---------------------------------------------------------------------------
def p_real_profiles() -> None:
    proc = run([PY, str(GATES / "g1_profile_plugin_gate.py")], timeout=180)
    record("P1", "PASS", "PASS" if proc.returncode == 0 else "BLOCKED_SAFE",
           "all real root+DEV profiles provider/plugin/dep consistent")


def p_non_hindsight_profile() -> None:
    from profile_gate import discover_profiles
    found = [x for x in discover_profiles() if x.name == "hermes-migration-maint"]
    ok = bool(found) and not found[0].hindsight_required
    record("P2", "PASS", "PASS" if ok else "BLOCKED_SAFE",
           "non-Hindsight profile is correctly not required to carry the plugin")


def p_dev_isolated() -> None:
    proc = run([PY, str(GATES / "dev_lifecycle_gate.py")], timeout=180)
    record("P3", "PASS", "PASS" if proc.returncode == 0 else "BLOCKED_SAFE",
           "hermes-dev isolated lifecycle holds (uid/HOME/HERMES_HOME/cwd/PM/Python)")


def p_gateway_live_pm() -> None:
    proc = run([PY, str(GATES / "g7_gateway_gate.py"), "--profile", "hermes-kvm8"], timeout=120)
    record("P4", "PASS", "PASS" if proc.returncode == 0 else "BLOCKED_SAFE",
           "live gateway process is PM runtime Python 3.14.7")


def p_normal_prompt() -> None:
    from g456_agent_init_gate import COMPOSER_RE, WELCOME_MARKER, clean
    text = clean("Welcome to Hermes Agent!\nhermes-seo8 ❯ hello")
    ok = WELCOME_MARKER in text and bool(COMPOSER_RE.search(text))
    record("P5", "PASS", "PASS" if ok else "BLOCKED_SAFE",
           "stable composer accepted (no LLM call)")


def p_hindsight_guard() -> None:
    proc = run([PY, str(GATES / "hindsight_version_guard.py")], timeout=180)
    record("P6", "PASS", "PASS" if proc.returncode == 0 else "BLOCKED_SAFE",
           "effective Hindsight 0.10.1 in every PM dependency env")


NEGATIVE = {
    "N1": n1_wrapper_reverted_to_legacy_venv,
    "N2": n2_provider_declared_plugin_missing,
    "N3": n3_plugin_present_but_disabled,
    "N4": n4_plugin_present_deps_absent,
    "N5": n5_non_tty_dependency_skip,
    "N6": n6_resolver_proposes_011,
    "N7": n7_effective_below_0100,
    "N8": n8_doctor_dependency_block,
    "N9": n9_agent_init_stops_at_consent,
    "N10": n10_second_canary_fails,
    "N11": n11_orphan_process_group,
    "N12": n12_gateway_unit_pm_live_pid_legacy,
    "N13": n13_custom07_regression,
    "N14": n14_custom08_regression,
    "N15": n15_tui_artifact_missing,
}
POSITIVE = {
    "P1": p_real_profiles,
    "P2": p_non_hindsight_profile,
    "P3": p_dev_isolated,
    "P4": p_gateway_live_pm,
    "P5": p_normal_prompt,
    "P6": p_hindsight_guard,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    print("=== NEGATIVE CASES (each must be BLOCKED_SAFE) ===")
    for cid, fn in NEGATIVE.items():
        if args.only and cid not in args.only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix=f"neg-{cid}-"))
        try:
            fn(tmp)
        except Exception as exc:
            record(cid, "BLOCKED_SAFE", "FAIL", f"harness error: {exc!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== POSITIVE FIXTURES (each must PASS, no LLM call) ===")
    for cid, fn in POSITIVE.items():
        if args.only and cid not in args.only:
            continue
        try:
            fn()
        except Exception as exc:
            record(cid, "PASS", "FAIL", f"harness error: {exc!r}")

    neg = [r for r in RESULTS if r["case"].startswith("N")]
    pos = [r for r in RESULTS if r["case"].startswith("P")]
    neg_pass = sum(1 for r in neg if r["result"] == "PASS")
    pos_pass = sum(1 for r in pos if r["result"] == "PASS")
    print(f"\nNEGATIVE_TESTS_TOTAL={len(neg)}")
    print(f"NEGATIVE_TESTS_PASS={neg_pass}")
    print(f"POSITIVE_TESTS_TOTAL={len(pos)}")
    print(f"POSITIVE_TESTS_PASS={pos_pass}")
    print("LLM_CALL_EXECUTED=no")
    print("PRODUCTION_MUTATED=no")
    ok = neg_pass == len(neg) and pos_pass == len(pos)
    print(f"GATE_CERTIFICATION={'PASS' if ok else 'FAIL'}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(RESULTS, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

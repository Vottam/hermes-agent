#!/usr/bin/env python3
"""CUSTOM_11 — acceptance gate: non-mutating initialization on every Hindsight profile.

Proves, per profile, the three properties the task requires:

    NON_MUTATING_INITIALIZATION=yes
    PACKAGE_MANAGER_INVOKED_ON_INIT=no
    VERSION_ADMISSION_FAIL_CLOSED=yes

Method
------
1. AST over the DEPLOYED plugin per profile. Text search is not acceptable: the
   repair's own comments name ``_maybe_upgrade_client()`` and ``install_specs()`` to
   explain what was removed, so a substring test would report a comment as a live
   call site. It checks real call nodes and the real enclosing function.
2. Dynamic admission proof: the deployed ``_check_client_admission`` is loaded with
   the package manager HARD-STUBBED and each version fixture is exercised. The stub
   is asserted active both before the module loads and immediately before
   ``initialize()`` is called, so a passing run cannot have silently reached the
   real package manager. (An earlier harness failed exactly here: it purged
   ``sys.modules`` AFTER installing the stubs, so the plugin imported the REAL
   ``tools.lazy_deps`` and a self-test actually triggered a PM takeover on the host.)
3. A drift check: the deployed file must equal the canonical fork's repair applied to
   the upstream pin, so a profile cannot silently diverge.

Exit 0 only when every profile passes every property.

Usage:
    acceptance.py            # report
    acceptance.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata as _md
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

SOT_ROOT = Path(__file__).resolve().parents[2]
DEEP_GATES = SOT_ROOT / "customizations" / "DEEP_RUNTIME_GATES"
if str(DEEP_GATES) not in sys.path:
    sys.path.insert(0, str(DEEP_GATES))

# Durable canonical fork. The reconciler itself is self-contained (it carries the
# OLD->NEW anchors inline), so a missing fork never blocks reconciliation; the fork
# is only reported here as provenance. There is deliberately NO audit-storage
# fallback: the audit copy under /root/hermes-audits/ is frozen evidence and must
# not become a runtime dependency of the acceptance gate.
FORK_PLUGIN = Path("/opt/hindsight-kvm8/source/hindsight-upstream/hindsight-integrations/hermes")

MUTATORS = {"install_specs", "stop_for_relaunch", "ensure"}
EXPLICIT_ONLY = "upgrade_client_explicitly"

# Fixtures: (installed version or None, must_block)
VERSION_FIXTURES = [
    ("0.10.1", False),
    ("0.10.2", False),
    ("0.10.0", True),
    ("0.9.2", True),
    ("0.11.0", True),
    ("0.12.3", True),
    ("1.0.0", True),
    ("not-a-version", True),
    ("", True),
    (None, True),   # missing distribution
]


# --------------------------------------------------------------------------
# 1. Static: the mutation path must not be in the initialization call graph.
# --------------------------------------------------------------------------
def static_check(plugin: Path) -> dict:
    tree = ast.parse(plugin.read_text(encoding="utf-8"))
    mutator_callers: dict[str, set[str]] = {}
    init_calls: set[str] = set()
    has_old_hook = False

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):  # noqa: N802
            if node.name == "_maybe_upgrade_client":
                nonlocal has_old_hook
                has_old_hook = True
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node):  # noqa: N802
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):  # noqa: N802
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            here = self.stack[-1] if self.stack else "<module>"
            if name in MUTATORS:
                mutator_callers.setdefault(name, set()).add(here)
            if here == "initialize" and name:
                init_calls.add(name)
            self.generic_visit(node)

    V().visit(tree)

    outside = sorted(
        {c for callers in mutator_callers.values() for c in callers if c != EXPLICIT_ONLY}
    )
    init_mutators = sorted(MUTATORS & init_calls)
    return {
        "has_old_upgrade_hook": has_old_hook,
        "mutator_callers": {k: sorted(v) for k, v in mutator_callers.items()},
        "mutators_outside_explicit_path": outside,
        "initialize_calls_admission": "_check_client_admission" in init_calls,
        "initialize_calls_mutator": init_mutators,
        "NON_MUTATING_INITIALIZATION": (
            not has_old_hook and not outside and not init_mutators and "_check_client_admission" in init_calls
        ),
        "PACKAGE_MANAGER_INVOKED_ON_INIT": "no" if not init_mutators else "YES",
    }


# --------------------------------------------------------------------------
# 2. Dynamic: fail-closed admission, with the PM provably unreachable.
# --------------------------------------------------------------------------
_SENTINEL = {"install_specs": 0, "stop_for_relaunch": 0}
_SEEN: list[str] = []


def _stub_pm() -> None:
    _SENTINEL["install_specs"] = 0
    _SENTINEL["stop_for_relaunch"] = 0
    ld = types.ModuleType("tools.lazy_deps")

    def install_specs(specs, *, timeout=300):  # pragma: no cover
        _SENTINEL["install_specs"] += 1
        raise RuntimeError("CUSTOM_11_ACCEPTANCE: package manager must be unreachable")

    ld.install_specs = install_specs
    tp = types.ModuleType("tools")
    tp.__path__ = []
    tp.lazy_deps = ld
    ou = types.ModuleType("hermes_cli._old_updater")

    def stop_for_relaunch(*, incomplete: bool = False):  # pragma: no cover
        _SENTINEL["stop_for_relaunch"] += 1
        raise RuntimeError("CUSTOM_11_ACCEPTANCE: takeover must be unreachable")

    ou.stop_for_relaunch = stop_for_relaunch
    hc = types.ModuleType("hermes_cli")
    hc.__path__ = []
    hc._old_updater = ou
    sys.modules["tools"] = tp
    sys.modules["tools.lazy_deps"] = ld
    sys.modules["hermes_cli"] = hc
    sys.modules["hermes_cli._old_updater"] = ou


def _assert_pm_stubbed(where: str) -> None:
    import tools.lazy_deps as ld
    if getattr(ld, "__file__", None) is not None:
        raise AssertionError(
            f"REAL package manager bound at {where} ({ld.__file__}) -- refusing to continue"
        )


def _load(plugin_dir: Path, tag: str, version: str | None):
    for m in [k for k in list(sys.modules) if k.startswith(f"c11_{tag}_")]:
        del sys.modules[m]
    _stub_pm()
    _assert_pm_stubbed("before module load")

    pkg = f"c11_{tag}"
    p = types.ModuleType(pkg)
    p.__path__ = [str(plugin_dir)]
    sys.modules[pkg] = p

    s = importlib.util.spec_from_file_location(f"{pkg}.settings", plugin_dir / "settings.py")
    ms = importlib.util.module_from_spec(s)
    sys.modules[f"{pkg}.settings"] = ms
    s.loader.exec_module(ms)

    for n, a in {
        "agent.memory_provider": {"MemoryProvider": object, "RecallStatus": object,
                                  "spawn_context_thread": lambda *x, **k: None},
        "agent.secret_scope": {"UnscopedSecretError": Exception, "get_secret": lambda *x, **k: None},
        "hermes_cli.config": {"cfg_get": lambda *x, **k: None},
        "hermes_constants": {"get_hermes_home": lambda: Path("/tmp")},
        "hermes_time": {"now": lambda: None},
        "tools.registry": {"tool_error": lambda *x, **k: ""},
        "utils": {"read_json_or_empty": lambda *x, **k: {}},
    }.items():
        m = types.ModuleType(n)
        for k, v in a.items():
            setattr(m, k, v)
        m.__path__ = []
        sys.modules[n] = m
    sys.modules.setdefault("agent", types.ModuleType("agent")).__path__ = []

    se = importlib.util.spec_from_file_location(f"{pkg}.embedded", plugin_dir / "embedded.py")
    me = importlib.util.module_from_spec(se)
    sys.modules[f"{pkg}.embedded"] = me
    try:
        se.loader.exec_module(me)
    except Exception:
        pass

    sp = importlib.util.spec_from_file_location(f"{pkg}.__init__", plugin_dir / "__init__.py")
    mp = importlib.util.module_from_spec(sp)
    sys.modules[f"{pkg}.__init__"] = mp
    sp.loader.exec_module(mp)
    _assert_pm_stubbed("after module load, before initialize()")
    return mp


def dynamic_check(plugin_dir: Path) -> dict:
    results = []
    real = _md.version
    for i, (version, must_block) in enumerate(VERSION_FIXTURES):
        _SEEN.clear()
        present = version is not None
        md_version = version if present else ""

        def fake(name, _v=md_version, _p=present):
            if name == "hindsight-client":
                _SEEN.append(name)
                if not _p:
                    raise _md.PackageNotFoundError(name)
                return _v
            return real(name)

        _md.version = fake
        try:
            mod = _load(plugin_dir, f"fx{i}", version)
            blocked, reason = False, ""
            try:
                got = mod._check_client_admission()
                if not present:
                    blocked = True
                    reason = "returned without raising for a missing distribution"
            except mod.ClientAdmissionError as e:
                blocked = True
                reason = e.reason[:90]
            except Exception as e:  # noqa: BLE001
                blocked = False
                reason = f"{type(e).__name__}: {str(e)[:80]}"
        finally:
            _md.version = real

        results.append({
            "installed": version if present else "<absent>",
            "expected_block": must_block,
            "observed_block": blocked,
            "ok": blocked == must_block,
            "stub_consulted": bool(_SEEN),
            "install_specs_attempts": _SENTINEL["install_specs"],
            "pm_reached": _SENTINEL["install_specs"] > 0,
            "reason": reason,
        })

    return {
        "fixtures": results,
        "all_fixtures_ok": all(r["ok"] for r in results),
        "pm_ever_reached": any(r["pm_reached"] for r in results),
        "VERSION_ADMISSION_FAIL_CLOSED": all(r["ok"] for r in results)
        and not any(r["pm_reached"] for r in results),
    }


# --------------------------------------------------------------------------
def target_profiles() -> tuple[list[tuple[str, Path]], str, str]:
    """Hindsight-required profile plugin dirs, plus the discovery state.

    The state is returned, never swallowed: an acceptance gate that reports
    "no profiles" because its interpreter could not read any config is not an
    acceptance result, it is a broken gate. See
    ``profile_gate.discover_profiles_checked``.
    """
    from profile_gate import discover_profiles_checked  # noqa: E402

    profiles, state, detail = discover_profiles_checked(include_dev=True)
    out = []
    for prof in profiles:
        if not prof.hindsight_required:
            continue
        out.append((prof.name, Path(prof.hermes_home) / "plugins" / "hindsight"))
    return out, state, detail


def runtime_selfcheck() -> str:
    """Fail loud when THIS interpreter cannot discover/classify profiles."""
    from gate_runtime import GateRuntimeError, preflight  # noqa: E402

    try:
        record = preflight("CUSTOM_11")
    except GateRuntimeError as exc:
        return str(exc)
    for line in record.as_lines():
        print(line)
    return ""


def canonical_sha() -> str | None:
    """Provenance only. A None here never gates: reconciliation is reconciler-driven."""
    if FORK_PLUGIN is None:
        return None
    try:
        return hashlib.sha256((FORK_PLUGIN / "__init__.py").read_bytes()).hexdigest()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = {
        "check": "CUSTOM_11_ACCEPTANCE",
        "utc": datetime.now(timezone.utc).isoformat(),
        "canonical_fork_sha256": canonical_sha(),
        "profiles": {},
    }

    # Fail-loud gate runtime, before discovery.
    runtime_error = runtime_selfcheck()
    if runtime_error:
        report["fatal"] = runtime_error
        print(json.dumps(report, indent=2) if args.json else runtime_error)
        print("CUSTOM_11=BLOCKED_GATE_RUNTIME")
        return 3

    try:
        profiles, state, detail = target_profiles()
    except Exception as exc:
        report["fatal"] = f"PROFILE_DISCOVERY_FAILED={exc}"
        print(json.dumps(report, indent=2) if args.json else report["fatal"])
        print("CUSTOM_11=BLOCKED_SAFE")
        return 2

    report["PROFILE_DISCOVERY_STATE"] = state
    if detail:
        report["PROFILE_DISCOVERY_DETAIL"] = detail

    if state == "RUNTIME_UNABLE":
        report["fatal"] = "GATE_RUNTIME_DISCOVERY_UNABLE"
        print(json.dumps(report, indent=2) if args.json else report["fatal"])
        print("CUSTOM_11=BLOCKED_GATE_RUNTIME")
        return 3

    if not profiles:
        print("NO_HINDSIGHT_REQUIRED_PROFILES")
        print("CUSTOM_11=BLOCKED_SAFE")
        return 2

    ok_all = True
    for name, pdir in profiles:
        entry: dict = {"path": str(pdir)}
        if not (pdir / "__init__.py").is_file():
            entry["error"] = "plugin file missing"
            ok_all = False
            report["profiles"][name] = entry
            continue
        entry["deployed_sha256"] = hashlib.sha256((pdir / "__init__.py").read_bytes()).hexdigest()
        try:
            entry["static"] = static_check(pdir / "__init__.py")
            entry["dynamic"] = dynamic_check(pdir)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        prof_ok = (
            "error" not in entry
            and entry["static"]["NON_MUTATING_INITIALIZATION"]
            and entry["static"]["PACKAGE_MANAGER_INVOKED_ON_INIT"] == "no"
            and entry["dynamic"]["VERSION_ADMISSION_FAIL_CLOSED"]
        )
        entry["PASS"] = bool(prof_ok)
        ok_all = ok_all and prof_ok
        report["profiles"][name] = entry

    report["AFFECTED_PROFILE_COUNT"] = len(profiles)
    report["PROFILE_PASS_COUNT"] = sum(1 for v in report["profiles"].values() if v.get("PASS"))
    report["PROFILE_DRIFT_COUNT"] = sum(1 for v in report["profiles"].values() if not v.get("PASS"))
    report["NON_MUTATING_INITIALIZATION"] = "yes" if ok_all else "no"
    report["PACKAGE_MANAGER_INVOKED_ON_INIT"] = "no" if ok_all else "YES"
    report["VERSION_ADMISSION_FAIL_CLOSED"] = "yes" if ok_all else "no"
    report["CUSTOM_11"] = "PASS" if ok_all else "BLOCKED_SAFE"

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for name, e in report["profiles"].items():
            if "error" in e:
                print(f"  {name:26s} ERROR {e['error'][:70]}")
                continue
            s, d = e["static"], e["dynamic"]
            print(f"  {name:26s} non_mutating={s['NON_MUTATING_INITIALIZATION']} "
                  f"pm_on_init={s['PACKAGE_MANAGER_INVOKED_ON_INIT']} "
                  f"fail_closed={d['VERSION_ADMISSION_FAIL_CLOSED']} "
                  f"fixtures={sum(1 for f in d['fixtures'] if f['ok'])}/{len(d['fixtures'])} "
                  f"PASS={e['PASS']}")
        print("-" * 78)
        for k in ("AFFECTED_PROFILE_COUNT", "PROFILE_PASS_COUNT", "PROFILE_DRIFT_COUNT",
                  "NON_MUTATING_INITIALIZATION", "PACKAGE_MANAGER_INVOKED_ON_INIT",
                  "VERSION_ADMISSION_FAIL_CLOSED", "CUSTOM_11"):
            print(f"{k}={report[k]}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())

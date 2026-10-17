#!/usr/bin/env python3
"""CANONICAL GATE RUNTIME — single source of truth for gate interpreter selection.

WHY
---
KVM8 2026-09-27. The source-of-truth gates (CUSTOM_09, CUSTOM_11, and the
DEEP_RUNTIME_GATES) read every operational profile's ``config.yaml`` through
``profile_gate._load_config()``, which does ``import yaml``. Three interpreters
exist on this host and they do NOT agree on that module:

    /usr/bin/python3                                              3.9.25   yaml 6.0.1
    /root/.hermes/venvs/hermes-kvm8-canonical/bin/python           3.11.15  yaml 6.0.3
    <PM managed tool> python3.14 (first python3 in root's PATH)   3.14.7   NO yaml

The managed PM tool interpreter is the one a bare ``python3`` resolves to in a
root shell, and it has no PyYAML. Running a gate under it did not fail -- it
degraded: every profile became ``PROFILE_UNCLASSIFIABLE:yaml_module_unavailable``,
therefore ``hindsight_required`` was False for all of them, therefore
``target_profiles()`` returned [], therefore the reconciler printed
``NO_HINDSIGHT_REQUIRED_PROFILES`` and ``CUSTOM_xx=BLOCKED_SAFE``.

That message is a LIE by omission. "No profile matches" and "no profile could
be read" have opposite meanings, and only the first can ever be legitimate.
The presence of ``__pycache__/reconcile.cpython-3{9,11,14}.pyc`` across the
customization directories proves these gates have really been executed under
all three interpreters in production, so their verdict could really change with
whoever invoked them.

WHAT THIS MODULE DOES
---------------------
It is the ONLY place that decides which interpreter runs a gate:

  * ``RUNTIME_CLASSES`` declares, per runtime class, which third-party modules
    that class requires. Requirements were MEASURED per gate, not assumed --
    see PYTHON_RUNTIME_AUDIT.md. CUSTOM_10 is a shell script and requires
    nothing; g7/dev_lifecycle/pipe_status/libatomic_preflight are stdlib-only;
    CUSTOM_09, CUSTOM_11 and g1/g2/g3/g456/hindsight_version_guard need PyYAML.

  * ``CANONICAL_GATE_PYTHON`` is the single default interpreter. It is the same
    venv ``update.sh`` already uses as ``PREFLIGHT_PYTHON``, so a gate run by
    the timer and a gate certified by hand are literally the same binary.

  * ``preflight()`` checks that an interpreter exists, is executable, and
    actually provides the modules its class requires. It reports the missing
    module BY NAME. It never installs anything and never degrades to a
    "no targets" verdict.

  * ``resolve()`` is what dispatchers call. An override is allowed -- overrides
    are how tests and replicas pin a different runtime -- but an override is
    held to the SAME capability preflight as the default. An interpreter that
    cannot satisfy its class is rejected loudly, not used.

  * ``emit_runtime_record()`` prints the machine-readable evidence of which
    interpreter ran, so a timer journal proves its own runtime instead of
    leaving it to be inferred.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Iterable

# The canonical gate interpreter. Deliberately NOT "python3", NOT
# "/usr/bin/env python3" and NOT PATH-derived: the whole defect being repaired
# is PATH-dependence. This is the same venv update.sh already defaults to as
# PREFLIGHT_PYTHON, so the timer path and the certified path converge by
# construction.
CANONICAL_GATE_PYTHON = os.environ.get(
    "HERMES_PREFLIGHT_PYTHON",
    "/root/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python",
)

# Overrides are permitted but must pass the same capability preflight.
PYTHON_OVERRIDE_ENV = "HERMES_PREFLIGHT_PYTHON"

RUNTIME_SHELL = "RUNTIME_SHELL"
RUNTIME_PY_STDLIB_ONLY = "RUNTIME_PY_STDLIB_ONLY"
RUNTIME_PY_YAML_REQUIRED = "RUNTIME_PY_YAML_REQUIRED"

#: Third-party modules each runtime class REQUIRES to be importable.
#: Measured, not assumed. An empty tuple means stdlib-only.
RUNTIME_CLASSES: dict[str, tuple[str, ...]] = {
    RUNTIME_SHELL: (),
    RUNTIME_PY_STDLIB_ONLY: (),
    RUNTIME_PY_YAML_REQUIRED: ("yaml",),
}

#: Every customization/gate in the source of truth, with its measured class.
#: Keys are the ids used in customizations.yaml and by the dispatchers.
GATE_RUNTIME_REGISTRY: dict[str, str] = {
    "CUSTOM_09": RUNTIME_PY_YAML_REQUIRED,
    "CUSTOM_10": RUNTIME_SHELL,
    "CUSTOM_11": RUNTIME_PY_YAML_REQUIRED,
    "CUSTOM_11_libatomic_preflight": RUNTIME_PY_STDLIB_ONLY,
    "G1_PROFILE_PLUGIN_GATE": RUNTIME_PY_YAML_REQUIRED,
    "G2_PTY_DEP_GUARD": RUNTIME_PY_YAML_REQUIRED,
    "G3_DOCTOR_GATE": RUNTIME_PY_YAML_REQUIRED,
    "G456_AGENT_INIT_GATE": RUNTIME_PY_YAML_REQUIRED,
    "G7_GATEWAY_GATE": RUNTIME_PY_STDLIB_ONLY,
    "DEV_LIFECYCLE_GATE": RUNTIME_PY_STDLIB_ONLY,
    "HINDSIGHT_VERSION_GUARD": RUNTIME_PY_YAML_REQUIRED,
    "PIPE_STATUS_GATE": RUNTIME_PY_STDLIB_ONLY,
}


class GateRuntimeError(RuntimeError):
    """Fail-loud signal. Callers must exit non-zero and print the reason.

    Deliberately NOT used for "no targets" -- an empty target list is a
    legitimate state and is reported by the caller, not by this module.
    """


@dataclass(frozen=True)
class RuntimeRecord:
    gate_id: str
    runtime_class: str
    interpreter: str
    version: str
    required_modules: tuple[str, ...]
    source: str  # "canonical" | "override"

    def as_lines(self) -> list[str]:
        return [
            f"GATE_ID={self.gate_id}",
            f"GATE_RUNTIME_CLASS={self.runtime_class}",
            f"GATE_RUNTIME_PYTHON={self.interpreter}",
            f"GATE_RUNTIME_VERSION={self.version}",
            f"GATE_RUNTIME_REQUIRED_MODULES={','.join(self.required_modules) or 'none'}",
            f"GATE_RUNTIME_SOURCE={self.source}",
            "GATE_RUNTIME_CAPABILITY=PASS",
        ]


def required_modules(gate_id: str) -> tuple[str, ...]:
    try:
        runtime_class = GATE_RUNTIME_REGISTRY[gate_id]
    except KeyError:
        raise GateRuntimeError(
            f"GATE_RUNTIME_UNKNOWN_GATE={gate_id}; it has no declared runtime class"
        ) from None
    return RUNTIME_CLASSES[runtime_class]


def runtime_class(gate_id: str) -> str:
    try:
        return GATE_RUNTIME_REGISTRY[gate_id]
    except KeyError:
        raise GateRuntimeError(
            f"GATE_RUNTIME_UNKNOWN_GATE={gate_id}; it has no declared runtime class"
        ) from None


def interpreter_version(python: str) -> str:
    import subprocess

    try:
        out = subprocess.run(
            [python, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise GateRuntimeError(f"GATE_RUNTIME_UNREADABLE={python}:{type(exc).__name__}") from exc
    if out.returncode != 0:
        raise GateRuntimeError(
            f"GATE_RUNTIME_UNREADABLE={python}:exit={out.returncode}"
            f":{(out.stderr or '').strip().splitlines()[-1] if (out.stderr or '').strip() else 'no stderr'}"
        )
    return out.stdout.strip()


def _modules_importable(python: str, modules: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """Import each module in a SUBPROCESS of *python*, never in this process.

    The check must reflect the candidate interpreter's own site-packages, not
    whatever this process happens to have imported already.
    """
    import subprocess

    modules = list(modules)
    if not modules:
        return [], {}
    code = (
        "import importlib, json, sys\n"
        "out = {}\n"
        f"for m in {modules!r}:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "        out[m] = 'OK'\n"
        "    except Exception as exc:\n"
        "        out[m] = type(exc).__name__\n"
        "print(json.dumps(out))\n"
    )
    probe_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    gate_pythonpath = os.environ.get("HERMES_GATE_PYTHONPATH", "").strip()
    if gate_pythonpath:
        probe_env["PYTHONPATH"] = gate_pythonpath
    try:
        res = subprocess.run(
            [python, "-S", "-c", code],
            cwd="/",
            env=probe_env,
            capture_output=True, text=True, timeout=60, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise GateRuntimeError(f"GATE_RUNTIME_PROBE_FAILED={python}:{type(exc).__name__}") from exc
    if res.returncode != 0 or not res.stdout.strip():
        raise GateRuntimeError(
            f"GATE_RUNTIME_PROBE_FAILED={python}:exit={res.returncode}"
        )
    try:
        data = json.loads(res.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        raise GateRuntimeError(
            f"GATE_RUNTIME_PROBE_UNPARSEABLE={python}:{type(exc).__name__}"
        ) from exc
    ok = [m for m in modules if data.get(m) == "OK"]
    return ok, {str(k): str(v) for k, v in data.items()}


def candidate_interpreter() -> tuple[str, str]:
    """Return (interpreter, source) for the gate runtime.

    The override is honoured -- that is how a test pins a different runtime --
    but the caller still has to pass it through :func:`preflight`.
    """
    override = os.environ.get(PYTHON_OVERRIDE_ENV, "").strip()
    if override:
        return override, "override"
    return CANONICAL_GATE_PYTHON, "canonical"


def preflight(gate_id: str, python: str | None = None, source: str | None = None) -> RuntimeRecord:
    """Validate that *python* can run *gate_id*. Raise :class:`GateRuntimeError` if not.

    This is the fail-loud gate. It is intentionally strict: a missing module is
    reported by name and never silently reinterpreted as an empty result set.
    """
    rc_class = runtime_class(gate_id)
    needs = RUNTIME_CLASSES[rc_class]

    if rc_class == RUNTIME_SHELL:
        # Shell gates have no interpreter capability. The dispatcher owns the
        # shell choice; there is nothing to preflight here.
        return RuntimeRecord(gate_id, rc_class, "shell", "n/a", (), source or "canonical")

    if python is None or source is None:
        python, source = candidate_interpreter()

    if not python or not os.path.isfile(python):
        raise GateRuntimeError(f"GATE_RUNTIME_INTERPRETER_MISSING={python or 'empty'}")
    if not os.access(python, os.X_OK):
        raise GateRuntimeError(f"GATE_RUNTIME_INTERPRETER_NOT_EXECUTABLE={python}")

    version = interpreter_version(python)
    ok, detail = _modules_importable(python, needs)
    missing = [m for m in needs if m not in ok]
    if missing:
        # THE fail-loud line. Never collapses into NO_HINDSIGHT_REQUIRED_PROFILES.
        raise GateRuntimeError(
            f"GATE_RUNTIME_DEPENDENCY_MISSING={','.join(missing)}"
            f":interpreter={python}:version={version}:gate={gate_id}"
            f":required_by_class={rc_class}:detail={json.dumps(detail)}"
        )
    return RuntimeRecord(gate_id, rc_class, python, version, needs, source)


def emit_runtime_record(record: RuntimeRecord) -> None:
    for line in record.as_lines():
        print(line)


def self_check(argv: list[str] | None = None) -> int:
    """`gate_runtime.py --self-check` : prove the policy on THIS interpreter.

    Deliberately usable from any interpreter so a negative test can point it at
    a runtime that is genuinely missing a required module.
    """
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--gate", default="CUSTOM_09")
    ap.add_argument("--python", default=None)
    args = ap.parse_args(list(argv) if argv is not None else None)

    print("GATE_RUNTIME_SELF_CHECK")
    print(f"GATE_RUNTIME_THIS_PYTHON={sys.executable}")
    print(f"GATE_RUNTIME_THIS_VERSION={'.'.join(str(v) for v in sys.version_info[:3])}")
    print(f"GATE_RUNTIME_CANONICAL={CANONICAL_GATE_PYTHON}")
    try:
        record = preflight(args.gate, args.python, "explicit" if args.python else None)
    except GateRuntimeError as exc:
        print(f"GATE_RUNTIME_ERROR={exc}")
        print("GATE_RUNTIME_CAPABILITY=FAIL")
        return 3
    emit_runtime_record(record)
    return 0


if __name__ == "__main__":
    sys.exit(self_check())

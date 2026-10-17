#!/usr/bin/env python3
"""CUSTOM_11 — Hindsight non-mutating initialization (auto-upgrade hygiene), reconciler.

WHY
---
``HindsightMemoryProvider.initialize()`` called ``_maybe_upgrade_client()``
unconditionally::

    def initialize(self, session_id, **kwargs):
        ...
        _maybe_upgrade_client()            # side effect of initialization

    def _maybe_upgrade_client():
        installed = pkg_version("hindsight-client")
        if Version(installed) < Version(_MIN_CLIENT_VERSION):     # floor = 0.10.1
            from tools.lazy_deps import install_specs
            install_specs([f"hindsight-client>={_MIN_CLIENT_VERSION}"], timeout=120)

``install_specs`` does NOT install anything. ``/opt/hermes-agent-clean/tools/lazy_deps.py``
is a 16-line shim whose body is ``stop_for_relaunch()``. That spawns
``hermes_cli/_update_takeover.py``, which runs the package manager's
``historical_takeover`` step: it stages and verifies every MANAGED TOOL (node, python,
uv, ffmpeg), writes ``tools/facts.json``, promotes a ~490 MB ffmpeg tree, and then
raises ``SystemExit`` in the Hermes process.

Proven on KVM8 2026-09-27 (``update_20260927_044737_746502_*.json``)::

    outcome      = failed
    pre_update   = {sha: 3db206a52c..., version: 0.21.5}
    post_update  = {sha: 3db206a52c..., version: 0.21.5}   <-- identical
    steps[0]     = historical_takeover ok=false
                   "node: staged entry failed verification: ... --version exited 127:
                    libatomic.so.1: cannot open shared object file"

``pre_update.sha == post_update.sha`` proves nothing was upgraded, yet a read-only
provider instantiation had already produced a PM receipt, a new ffmpeg tree and a
``facts.json`` write. The declared intent was "update hindsight-client"; the actual
effect was the whole runtime package manager's tool staging. ffmpeg has no
relationship to Hindsight.

The SAME method serves session start, ``doctor``, ``status``, a dry-run and test
fixtures. There was no context and no suppression flag (the real signature is
``(specs, *, timeout=300)`` — no ``allow_upgrade``, ``read_only``, ``dry_run`` or
admission gate), and suppression was not an option regardless: ``install_specs`` ends
the process with ``SystemExit``, a ``BaseException``, so no ``except Exception``
contains it without also swallowing legitimate exits — and the staging/facts.json
writes have already happened by then. (The upstream ``except Exception: pass`` at the
end of the old function was dead code for the same reason.) The mutation path must
not be ENTERED.

THE REPAIR
----------
1. ``_maybe_upgrade_client()`` is removed; ``initialize()`` calls
   ``_check_client_admission()`` instead.
2. ``_check_client_admission()`` is check-only: it inspects the installed client and
   validates it against the EFFECTIVE range — the intersection of the plugin's own
   floor (0.10.1) with the local 0.10.x support policy — and never installs, upgrades
   or invokes a package manager.
3. Admission is FAIL-CLOSED and unconditional. Below floor, at/above the 0.11.0
   exclusive ceiling, missing, malformed and unreadable all raise
   ``ClientAdmissionError`` carrying an explicit remediation instruction. There is
   deliberately no lenient mode: an incompatible client is an operational problem
   regardless of which entry path reached the check, so doctor and status see the same
   behaviour as a session start. A version that cannot be proven admitted is not
   admitted.
4. ``upgrade_client_explicitly()`` keeps the legitimate upgrade capability as an
   EXPLICIT entrypoint only. Removing implicit mutation must not remove the ability to
   upgrade.

The local policy floor (0.10.0) is NOT weakened to admit the installed-but-broken
0.10.0. ``pyproject.toml`` documents why that release is broken for this plugin
(hindsight-embed 0.10.0 ran ``asyncio.run()`` on the caller's thread, killing its
event loop), so the effective floor stays 0.10.1 and 0.10.0 is rejected.

SCOPE
-----
Applies ONLY to profiles whose canonical intent flag says ``hindsight_required`` is
true (``memory.provider == hindsight`` and memory enabled), discovered through the
canonical ``profile_gate`` helper — never a hardcoded list. Profiles that do not use
the Hindsight plugin are never touched, and the DEV profile is included only when it
is itself Hindsight-required, preserving its uid/home/store isolation. The plugin is a
per-profile installed copy, so every intended copy is repaired and then verified for
drift.

IDEMPOTENT
----------
Each edit is anchored on an exact source signature. If an anchor is not found, or is
found more than once, the reconciler fails closed (BLOCKED_SAFE) rather than guessing.
Re-running on an already-repaired file is a no-op.

Usage:
    reconcile.py            # apply to all hindsight-required profiles
    reconcile.py --check    # report drift only, change nothing
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SOT_ROOT = Path(__file__).resolve().parents[2]
DEEP_GATES = SOT_ROOT / "customizations" / "DEEP_RUNTIME_GATES"
if str(DEEP_GATES) not in sys.path:
    sys.path.insert(0, str(DEEP_GATES))

PROFILES_ROOT = Path("/root/.hermes/profiles")
PLUGIN_REL = Path("plugins/hindsight/__init__.py")
SETTINGS_REL = Path("plugins/hindsight/settings.py")

# --- the repair -------------------------------------------------------------

# 1. The implicit upgrade hook itself. Exact current body; if upstream re-pins and
#    this text changes, the anchor stops matching and we fail closed.
OLD_HOOK = '''def _maybe_upgrade_client() -> None:
    """Auto-upgrade an outdated hindsight-client via the environment-aware lazy_deps
    installer (sealed hosted venvs redirect to the durable target)."""
    try:
        from importlib.metadata import version as pkg_version

        from packaging.version import Version

        installed = pkg_version("hindsight-client")
        if Version(installed) < Version(_MIN_CLIENT_VERSION):
            logger.warning(
                "hindsight-client %s is outdated (need >=%s), attempting upgrade...", installed, _MIN_CLIENT_VERSION
            )
            from tools.lazy_deps import install_specs

            outcome = install_specs([f"hindsight-client>={_MIN_CLIENT_VERSION}"], timeout=120)
            if outcome.ok:
                logger.info("hindsight-client upgraded to >=%s", _MIN_CLIENT_VERSION)
            elif outcome.blocked:
                logger.warning(
                    "Auto-upgrade unavailable: %s. Run: uv pip install 'hindsight-client>=%s'",
                    outcome.reason,
                    _MIN_CLIENT_VERSION,
                )
            else:
                logger.warning(
                    "Auto-upgrade failed: %s. Run: uv pip install 'hindsight-client>=%s'",
                    (outcome.stderr or "").strip() or "install error",
                    _MIN_CLIENT_VERSION,
                )
    except Exception:
        pass  # packaging not available or other issue — proceed anyway
'''

NEW_HOOK = '''class ClientAdmissionError(RuntimeError):
    """Installed hindsight-client is not admitted by the effective version range.

    Raised by :func:`_check_client_admission`. This is a fail-closed signal: the
    provider refuses to initialize rather than installing, upgrading or handing
    control to the package manager on its own.
    """

    def __init__(self, reason: str, *, installed: str | None, effective: str, remediation: str):
        super().__init__(reason)
        self.reason = reason
        self.installed = installed
        self.effective = effective
        self.remediation = remediation


def _effective_client_version_range() -> str:
    """Human-readable effective admission range (intersection of the two policies)."""
    return f">={_MIN_CLIENT_VERSION},<{_CLIENT_VERSION_MAX_EXCLUSIVE_POLICY}"


def _check_client_admission() -> str:
    """Inspect the installed hindsight-client and validate it. NEVER mutates.

    KVM8 T2.1. This REPLACES the implicit ``_maybe_upgrade_client()`` call that used
    to run from :meth:`HindsightMemoryProvider.initialize`.

    Why the change (the exact incident, 2026-09-27): ``initialize()`` called
    ``_maybe_upgrade_client()``, which on a version mismatch called
    ``tools.lazy_deps.install_specs()``. In the current runtime that function is a
    16-line shim whose whole body is ``stop_for_relaunch()`` -- it does NOT install
    anything. It spawns ``hermes_cli/_update_takeover.py``, which runs the package
    manager's ``historical_takeover`` step: it stages and verifies every managed tool
    (node, python, uv, ffmpeg), writes ``tools/facts.json``, promotes a ~490 MB
    ffmpeg tree, and then raises ``SystemExit`` in the Hermes process. The receipt
    proved ``pre_update.sha == post_update.sha``: nothing was upgraded, and the step
    failed on a staged ``node`` missing ``libatomic.so.1``.

    A read-only path (doctor, status, a dry-run, a test fixture) reached all of that
    just by constructing a provider. Initialization must therefore be non-mutating:
    CHECK, REPORT, and fail closed when operationally incompatible. Installing or
    upgrading belongs exclusively to an explicit lifecycle (the Hermes package
    manager / a maintenance action), which has its own gates.

    Suppressing the old path is NOT an option, and this is not a style preference:
    ``install_specs`` ends the process with ``SystemExit``, which derives from
    ``BaseException``, so no ``except Exception`` can contain it without also
    swallowing legitimate exits -- and the staging/facts.json writes have already
    happened by then. The mutation path must simply never be entered.

    FAIL-CLOSED, unconditionally. There is deliberately no "lenient" mode: a version
    that cannot be proven admitted is not admitted. A below-floor, above-ceiling,
    missing, malformed or unreadable client all raise :class:`ClientAdmissionError`
    carrying an explicit remediation instruction. This applies to every caller
    including ``doctor``/``status``: an incompatible client is an operational problem
    regardless of who is asking, and a read-only inspector has no business hiding it.

    The caller (the memory manager) logs the failure and drops the provider, so the
    session continues without a Hindsight memory rather than with a memory provider
    running against a client it was not written for.

    Returns the installed version string on success.
    """
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version
    except Exception as exc:  # pragma: no cover - importlib.metadata is stdlib
        raise ClientAdmissionError(
            f"Cannot verify hindsight-client version: importlib.metadata is unusable ({exc}).",
            installed=None,
            effective=_effective_client_version_range(),
            remediation=(
                "Repair the environment through the explicit lifecycle (`hermes update`). "
                "Initialization will not install anything."
            ),
        ) from exc

    try:
        installed = pkg_version("hindsight-client")
    except PackageNotFoundError:
        raise ClientAdmissionError(
            "hindsight-client is not installed in this environment, so the plugin's "
            "declared dependency floor cannot be satisfied.",
            installed=None,
            effective=_effective_client_version_range(),
            remediation=(
                f"Install it through the explicit lifecycle: `hermes update` (preferred), "
                f"or `uv pip install 'hindsight-client{_effective_client_version_range()}'` "
                f"inside the profile venv. Initialization will not install anything."
            ),
        ) from None
    except Exception as exc:  # pragma: no cover - broken dist metadata
        raise ClientAdmissionError(
            f"hindsight-client is installed but its version metadata is unreadable: {exc}",
            installed=None,
            effective=_effective_client_version_range(),
            remediation=(
                "Reinstall through the explicit lifecycle (`hermes update`) so the "
                "distribution metadata is valid. Initialization will not install anything."
            ),
        ) from exc

    try:
        from packaging.version import InvalidVersion, Version
    except ImportError as exc:
        # packaging is not importable: we cannot prove the range, so we must not
        # claim compatibility. Fail closed rather than assume.
        raise ClientAdmissionError(
            f"hindsight-client {installed}: cannot validate the version range because "
            "'packaging' is not importable in this interpreter.",
            installed=installed,
            effective=_effective_client_version_range(),
            remediation=(
                "Repair the environment through the explicit lifecycle (`hermes update`) so "
                "'packaging' is present. Initialization will not install anything."
            ),
        ) from exc

    try:
        parsed = Version(installed)
        floor = Version(_MIN_CLIENT_VERSION)
        ceiling = Version(_CLIENT_VERSION_MAX_EXCLUSIVE_POLICY)
    except InvalidVersion as exc:
        raise ClientAdmissionError(
            f"hindsight-client version {installed!r} is not a valid version string.",
            installed=installed,
            effective=_effective_client_version_range(),
            remediation=(
                "Reinstall through the explicit lifecycle (`hermes update` or "
                "`hermes plugins install hindsight`) to get a well-formed version."
            ),
        ) from exc

    if parsed < floor:
        raise ClientAdmissionError(
            f"hindsight-client {installed} is below the required minimum {_MIN_CLIENT_VERSION} "
            f"(effective range {_effective_client_version_range()}). Note that the local "
            f"support policy floor {_CLIENT_VERSION_MIN_POLICY} is the RUNTIME's, not this "
            f"plugin's: 0.10.0 is inside that window but is broken for this plugin.",
            installed=installed,
            effective=_effective_client_version_range(),
            remediation=(
                f"Install the supported client through the explicit lifecycle (`hermes update`, "
                f"or `uv pip install 'hindsight-client{_effective_client_version_range()}'` "
                f"inside the profile venv), then restart Hermes. Initialization will not "
                f"install anything and will not invoke the package manager."
            ),
        )

    if parsed >= ceiling:
        raise ClientAdmissionError(
            f"hindsight-client {installed} is at or above the maximum supported version "
            f"{_CLIENT_VERSION_MAX_EXCLUSIVE_POLICY} (exclusive); the effective range is "
            f"{_effective_client_version_range()}. This plugin is certified against the "
            f"0.10.x line only.",
            installed=installed,
            effective=_effective_client_version_range(),
            remediation=(
                "Move to a supported release through the explicit lifecycle: pin the "
                "profile's hindsight plugin to a 0.10.x-compatible revision and reinstall "
                "the matching client. Initialization will not change anything on its own."
            ),
        )

    return installed


def upgrade_client_explicitly(*, timeout: int = 120) -> str:
    """EXPLICIT upgrade entrypoint for the Hindsight client. NEVER called by initialize().

    This is the only sanctioned path from this plugin to the package manager, and it
    is not part of any read-only flow. It is invoked by an explicit maintenance
    action or by the Hermes package-manager lifecycle, each of which has its own
    gates, backup and rollback.

    KVM8 T2.1 kept this function so that removing the implicit mutation did NOT remove
    legitimate upgrade capability. It returns a short string describing the outcome.

    Note for operators: on a host where the package manager's tool staging cannot be
    completed (e.g. a staged ``node`` whose ``libatomic.so.1`` is missing), this call
    can end in a package-manager takeover failure and a process exit WITHOUT
    upgrading anything. That failure belongs here, on an explicit path, and not in a
    session start.
    """
    installed = None
    with contextlib.suppress(Exception):
        from importlib.metadata import version as pkg_version

        installed = pkg_version("hindsight-client")

    target = f">={_MIN_CLIENT_VERSION},<{_CLIENT_VERSION_MAX_EXCLUSIVE_POLICY}"
    logger.warning(
        "EXPLICIT hindsight-client upgrade requested: installed=%s, target=%s", installed, target
    )
    from tools.lazy_deps import install_specs

    # NoReturn in the current runtime: this may end the process. That is acceptable
    # ONLY here, on an explicit path, and never as a consequence of initialize().
    install_specs([f"hindsight-client{target}"], timeout=timeout)
    return "install_specs returned without ending the process (unreachable in the current runtime)"
'''

# 2. The call site inside initialize(). The load-bearing line of the whole repair.
OLD_CALL = """        self._document_id = _mint_document_id(self._session_id)
        _maybe_upgrade_client()
"""

NEW_CALL = '''        self._document_id = _mint_document_id(self._session_id)

        # KVM8 T2.1: dependency admission is CHECK-ONLY. This used to call
        # _maybe_upgrade_client(), which on a version mismatch reached the package
        # manager through tools.lazy_deps.install_specs() -> stop_for_relaunch():
        # a read-only initialize() staged tools, wrote tools/facts.json, promoted a
        # ~490 MB ffmpeg tree and ended the process WITHOUT upgrading anything
        # (receipt proved pre_update.sha == post_update.sha). An initialization hook
        # must not be able to trigger a package installation, update or takeover.
        #
        # A mismatch is now REPORTED and fails closed with an explicit remediation
        # instruction. Repair belongs to an explicit lifecycle (Hermes PM / a
        # maintenance action) via upgrade_client_explicitly() or `hermes update`.
        #
        # There is no lenient mode on purpose: an out-of-range, missing or unreadable
        # client is an operational problem regardless of which entry path got here,
        # so doctor and status see the same fail-closed behaviour as a session start.
        # The memory manager logs the error and drops the provider, leaving the
        # session without a Hindsight memory rather than with a memory provider
        # running against a client it was not written for.
        _check_client_admission()
'''

# 3. Import the new policy constants from .settings
OLD_IMPORT = """from .settings import (
    _DEFAULT_API_URL,
"""
NEW_IMPORT = """from .settings import (
    _CLIENT_VERSION_MAX_EXCLUSIVE_POLICY,
    _CLIENT_VERSION_MIN_POLICY,
    _DEFAULT_API_URL,
"""

# 4. settings.py: the effective-range policy window.
OLD_SETTINGS = '''# Keep in sync with plugin.yaml and pyproject.toml. Raised to 0.10.1 with the embed
# floor (see pyproject.toml): _maybe_upgrade_client() then pulls the 0.10.0 cohort's
# client forward on session start instead of waiting for a `hermes update`.
_MIN_CLIENT_VERSION = "0.10.1"
'''

NEW_SETTINGS = '''# Keep in sync with plugin.yaml and pyproject.toml.
#
# This is the PLUGIN's own dependency floor: the client/embed the plugin is written
# against. It is the *plugin* minimum, not a local policy knob.
#
# Upstream raised this to 0.10.1 with the embed floor (see pyproject.toml) so that a
# session start would pull the 0.10.0 cohort's client forward. KVM8 T2.1 removed
# that pull: initialization is now check-only, so a stale client is REPORTED and
# fails closed instead of being auto-repaired from initialize(). See
# _check_client_admission() in __init__.py.
_MIN_CLIENT_VERSION = "0.10.1"
# Local support policy for the 0.10.x line. The EFFECTIVE admission range is the
# intersection of this window and the plugin's own floor, applied in
# _check_client_admission() — never the wider of the two. A client below
# _MIN_CLIENT_VERSION is rejected even though 0.10.0 is inside this window,
# because 0.10.0 is genuinely broken for this plugin (hindsight-embed 0.10.0 ran
# asyncio.run() on the caller's thread and killed its event loop; see
# pyproject.toml). A client at or above _CLIENT_VERSION_MAX_EXCLUSIVE_POLICY fails
# closed because the 0.10.x line is the only supported one, even though pyproject.toml
# declares the looser `<1`.
_CLIENT_VERSION_MIN_POLICY = "0.10.0"
_CLIENT_VERSION_MAX_EXCLUSIVE_POLICY = "0.11.0"
'''


class ReconcileError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_single(text: str, needle: str, label: str) -> None:
    n = text.count(needle)
    if n != 1:
        raise ReconcileError(f"anchor {label} matched {n} times (expected exactly 1)")


def apply_repair_plugin(text: str) -> tuple[str, list[str]]:
    """Return (new_text, applied_step_names) for plugins/hindsight/__init__.py. Idempotent."""
    applied: list[str] = []

    if "def _check_client_admission()" in text and "def _maybe_upgrade_client()" not in text:
        return text, ["already_repaired"]

    # Step 1: the settings import must carry the new policy constants.
    if "from .settings import (\n    _CLIENT_VERSION_MAX_EXCLUSIVE_POLICY," not in text:
        _require_single(text, OLD_IMPORT, ".settings import block")
        text = text.replace(OLD_IMPORT, NEW_IMPORT)
        applied.append("import_policy_constants")

    # Step 2: the hook itself becomes check-only + explicit upgrade entrypoint.
    _require_single(text, OLD_HOOK, "_maybe_upgrade_client body")
    text = text.replace(OLD_HOOK, NEW_HOOK)
    applied.append("non_mutating_admission")

    # Step 3: initialize() stops entering the mutation path.
    _require_single(text, OLD_CALL, "initialize() upgrade call")
    text = text.replace(OLD_CALL, NEW_CALL)
    applied.append("drop_implicit_upgrade_from_initialize")

    # The mutation path must be unreachable from initialize, not merely guarded.
    # Check the AST for actual call nodes instead of grepping text: the docstrings
    # in this file legitimately mention install_specs(), and a substring check
    # would flag those as reachable call sites.
    _assert_install_specs_only_in_explicit_upgrade(text)
    applied.append("verify_mutation_unreachable")

    return text, applied


def _assert_install_specs_only_in_explicit_upgrade(text: str) -> None:
    """Fail closed unless every ``install_specs`` CALL lives in upgrade_client_explicitly().

    Uses the AST so docstrings and comments cannot produce a false positive, and
    cannot produce a false negative the way a substring search would.
    """
    import ast

    tree = ast.parse(text)
    callers: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            fname = None
            if isinstance(node.func, ast.Name):
                fname = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            if fname == "install_specs":
                callers.append(self.stack[-1] if self.stack else "<module>")
            self.generic_visit(node)

    Visitor().visit(tree)

    if not callers:
        raise ReconcileError(
            "no install_specs call site found at all; the explicit upgrade path must be preserved"
        )
    outside = sorted({c for c in callers if c != "upgrade_client_explicitly"})
    if outside:
        raise ReconcileError(
            f"install_specs is still callable outside upgrade_client_explicitly: {outside}"
        )


def apply_repair_settings(text: str) -> tuple[str, list[str]]:
    """Return (new_text, applied_step_names) for plugins/hindsight/settings.py. Idempotent."""
    if "_CLIENT_VERSION_MAX_EXCLUSIVE_POLICY" in text:
        return text, ["already_repaired"]
    _require_single(text, OLD_SETTINGS, "_MIN_CLIENT_VERSION block")
    return text.replace(OLD_SETTINGS, NEW_SETTINGS), ["effective_version_range"]


def target_profiles() -> tuple[list[tuple[str, Path]], str, str]:
    """Profiles whose canonical intent flag is hindsight_required (dynamic discovery).

    Returns ``(targets, state, detail)``; ``state`` separates a legitimate
    empty match set from a runtime that could not classify anything at all.
    See ``profile_gate.discover_profiles_checked``.
    """
    from profile_gate import discover_profiles_checked  # noqa: E402  (path set above)

    profiles, state, detail = discover_profiles_checked(include_dev=True)
    out: list[tuple[str, Path]] = []
    # include_dev=True: a DEV profile that is itself Hindsight-required consumes the
    # same affected integration and is repaired through its own uid/home/store. The
    # gate's own hindsight_required flag is what decides, not a hardcoded name.
    for prof in profiles:
        if not prof.hindsight_required:
            continue
        out.append((prof.name, Path(prof.hermes_home) / PLUGIN_REL))
    return out, state, detail


def preserve_owner(dst: Path, reference: Path) -> None:
    """Restore ``reference``'s owner/group/mode onto ``dst``.

    The updater runs as root and would otherwise hand a non-root profile's own
    plugin to root. The repair is root-written on purpose; the OWNERSHIP is the
    user's and is taken from the pre-image backup.
    """
    try:
        info = reference.stat()
    except OSError:
        return
    for action in (
        lambda: os.chown(dst, info.st_uid, info.st_gid),
        lambda: os.chmod(dst, info.st_mode & 0o7777),
    ):
        try:
            action()
        except OSError:
            pass


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


def compile_check(path: Path) -> None:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as exc:  # pragma: no cover
        raise ReconcileError(f"syntax error in {path}: {exc}") from exc


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only; change nothing")
    args = ap.parse_args(list(argv) if argv is not None else None)

    print("CUSTOM_11_HINDSIGHT_NON_MUTATING_INIT_RECONCILE")
    print(f"MODE={'CHECK' if args.check else 'APPLY'}")
    print(f"UTC={datetime.now(timezone.utc).isoformat()}")

    # Fail-loud gate runtime, before discovery. Without this an interpreter
    # lacking PyYAML reports "no profile matches" instead of "this gate cannot
    # read any profile" -- see profile_gate.discover_profiles_checked.
    runtime_error = runtime_selfcheck()
    if runtime_error:
        print(runtime_error)
        print("CUSTOM_11=BLOCKED_GATE_RUNTIME")
        return 3

    try:
        targets, state, detail = target_profiles()
    except Exception as exc:
        print(f"PROFILE_DISCOVERY_FAILED={exc}")
        print("CUSTOM_11=BLOCKED_SAFE")
        return 2

    print(f"PROFILE_DISCOVERY_STATE={state}")
    if detail:
        print(f"PROFILE_DISCOVERY_DETAIL={detail}")

    if state == "RUNTIME_UNABLE":
        print("GATE_RUNTIME_DISCOVERY_UNABLE")
        print("CUSTOM_11=BLOCKED_GATE_RUNTIME")
        return 3

    if not targets:
        print("NO_HINDSIGHT_REQUIRED_PROFILES")
        print("CUSTOM_11=BLOCKED_SAFE")
        return 2

    print(f"PROFILE_PLUGIN_COUNT={len(targets)}")

    drift = 0
    repaired = 0
    for name, plugin_path in targets:
        rel = f"{name}/{PLUGIN_REL}"
        settings_path = plugin_path.parent / "settings.py"
        for p in (plugin_path, settings_path):
            if not p.is_file():
                print(f"  {p.name}: MISSING ({p})")
                drift += 1

        if not plugin_path.is_file() or not settings_path.is_file():
            continue

        try:
            new_text, applied = apply_repair_plugin(plugin_path.read_text(encoding="utf-8"))
            new_settings, applied_settings = apply_repair_settings(settings_path.read_text(encoding="utf-8"))
        except ReconcileError as exc:
            print(f"  {rel}: BLOCKED ({exc})")
            drift += 1
            continue

        if applied == ["already_repaired"] and applied_settings == ["already_repaired"]:
            print(f"  {rel}: ALREADY_REPAIRED sha={sha256(plugin_path)[:16]}")
            continue

        try:
            compile(new_text, str(plugin_path), "exec")
            compile(new_settings, str(settings_path), "exec")
        except SyntaxError as exc:
            print(f"  {rel}: BLOCKED (patch would not compile: {exc})")
            drift += 1
            continue

        if args.check:
            print(f"  {rel}: WOULD_APPLY {','.join(applied + applied_settings)}")
            drift += 1
            continue

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for path, text in ((plugin_path, new_text), (settings_path, new_settings)):
            backup = path.with_suffix(f"{path.suffix}.CUSTOM11.{ts}.bak")
            shutil.copy2(path, backup)
            path.write_text(text, encoding="utf-8")
            # Ownership comes from the pre-image backup, never from the file
            # just rewritten: a root-written repair must not leave a
            # non-root profile's plugin owned by root.
            preserve_owner(backup, path)
            preserve_owner(path, backup)
            compile_check(path)
        info = plugin_path.stat()
        if info.st_uid != 0 or info.st_gid != 0:
            print(f"  {rel}: OWNER={info.st_uid}:{info.st_gid} mode={info.st_mode & 0o7777:o}")
        repaired += 1
        print(
            f"  {rel}: REPAIRED ({','.join(applied + applied_settings)}) "
            f"sha={sha256(plugin_path)[:16]} backup={plugin_path.name}.CUSTOM11.{ts}.bak"
        )

    print(f"PROFILE_PLUGIN_REPAIRED={repaired}")
    print(f"PROFILE_PLUGIN_DRIFT_COUNT={drift}")

    if drift:
        print("CUSTOM_11=BLOCKED_SAFE")
        return 1
    print("CUSTOM_11=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

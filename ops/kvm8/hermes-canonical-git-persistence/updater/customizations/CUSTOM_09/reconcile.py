#!/usr/bin/env python3
"""CUSTOM_09 — Hindsight retain async-ACK repair (reconciler).

WHY
---
The explicit ``hindsight_retain`` TOOL submits WITHOUT ``retain_async``:

    def _tool_retain(self, args: dict) -> str:
        ...
        self._retain_batch(item, bank_id=self._bank_id)     # <-- no retain_async

``_retain_batch`` drops ``None`` values, so the server default (``retain_async=False``)
applies. The HTTP call therefore blocks until the FULL pipeline — including
``retain_extract_facts`` (observed median 118s, p90 395s, max 600s) — has finished.
The plugin's sync bridge then aborts at its own ceiling:

    plugins/hindsight/__init__.py:234   future.result(timeout=timeout)   # 120s
    -> concurrent.futures.TimeoutError  -> "Failed to store memory: "

That is a FALSE NEGATIVE: the server keeps processing and persists the memory
(135/135 observed retains ended with docs>=1, 0 server errors). The user retries, the
server enqueues another expensive extract_facts op, and the backlog grows.

The automatic per-turn path never had this problem: ``_make_turn_retain_job`` already
passes ``retain_async=self._retain_async`` (config default True). This reconciler brings
the TOOL onto the same OFFICIAL, already-shipped async mechanism. Nothing is invented:
``retain_async``, ``operation_id`` and ``GET /banks/{id}/operations`` are first-class in
hindsight-client 0.10.1 and the server returns the ACK right after
``submit_async_retain()``.

CONTRACT HONESTY
----------------
The tool now returns an ACCEPTED/QUEUED acknowledgement carrying the operation id. It
must NOT claim the memory is fully persisted — extraction has not run yet. Failures are
NOT swallowed: submission errors still raise, so a refused connection, an HTTP error, a
malformed body or a rejected submission propagates and the tool still reports failure.

SCOPE
-----
Applies to EVERY profile whose canonical intent flag says ``hindsight_required``
is true (``memory.provider == hindsight`` and memory enabled), discovered via the
canonical profile_gate helper -- ROOT **and** DEV. Profiles that do not use the
Hindsight plugin are never touched. The plugin is a per-profile installed copy, so
every intended copy is patched and then verified for drift.

DEV PARITY (KVM8 2026-09-27)
---------------------------
This reconciler originally called ``discover_profiles(include_dev=False)`` and
rebuilt each path as ``/root/.hermes/profiles/<name>/...``. Both were wrong for
a reason that was invisible from the output:

* ``hermes-monepeviq-dev`` is a fully operational Hindsight-enabled profile
  (``memory.provider: hindsight``, memory enabled, plugin installed, user
  ``mpq-dev``). It was excluded from a repair whose entire point is that the
  explicit ``hindsight_retain`` tool path is broken -- so DEV kept the
  synchronous submit, and the 120s-ceiling false "Failed to store memory" was
  still reachable there. The exclusion was a scope bug, not a design decision:
  nothing in this customization's rationale depends on the profile being root.
* It also hardcoded the ROOT profiles directory while ``Profile`` already
  carries ``hermes_home``. Any non-root profile would have been looked up under
  the wrong home even if discovery had returned it.

Both are fixed by using ``prof.hermes_home`` -- the real, discovered home -- and
by including DEV. Writing the DEV file as root is required (the updater is a
root transaction) and safe: the reconciler preserves the existing file's owner
and mode instead of creating a root-owned artifact inside a user home. The DEV
profile's own PM store, HERMES_HOME, uid and workspace are untouched.

GATE RUNTIME (KVM8 2026-09-27)
------------------------------
Interpreter selection is NOT this file's concern and must never be inherited
from PATH. Dispatchers preflight the runtime through
``DEEP_RUNTIME_GATES/gate_runtime.py`` and re-exec this reconciler with the
canonical interpreter; direct invocation additionally self-checks, so a caller
who reaches for a bare ``python3`` is told the real cause instead of receiving
``NO_HINDSIGHT_REQUIRED_PROFILES``.

IDEMPOTENT
----------
Each edit is anchored on an exact source signature. If the anchor is not found, or is
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
import re
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

# --- the repair -------------------------------------------------------------
# Anchor: the exact current body of the interactive tool. If the upstream plugin is
# re-pinned and this text changes, the anchor stops matching and we fail closed
# instead of writing a patch we cannot verify.
OLD_RETAIN_TAIL = '''        logger.debug("Tool hindsight_retain: bank=%s, content_len=%d, context=%s", self._bank_id, len(content), context)
        self._retain_batch(item, bank_id=self._bank_id)
        logger.debug("Tool hindsight_retain: success")
        return "Memory stored successfully."
'''

NEW_RETAIN_TAIL = '''        logger.debug("Tool hindsight_retain: bank=%s, content_len=%d, context=%s", self._bank_id, len(content), context)
        # CUSTOM_09: submit asynchronously. The server accepts the operation and returns
        # an operation_id before fact extraction runs; waiting for the full pipeline is
        # what used to abort at the plugin's 120s sync-bridge ceiling and report a false
        # "Failed to store memory" for a memory the server persisted anyway.
        resp = self._retain_batch(
            item, bank_id=self._bank_id, retain_async=True, operation_id=self._mint_retain_operation_id()
        )
        # Submission errors are NOT swallowed: aretain_batch raises on connection
        # refusal, HTTP error, malformed body and rejected submission, so those still
        # propagate to handle_tool_call and the tool still reports failure.
        op_id = getattr(resp, "operation_id", None) or (
            (getattr(resp, "operation_ids", None) or [None])[0]
        )
        self._track_retain_ops(resp, self._bank_id)
        logger.debug("Tool hindsight_retain: accepted operation_id=%s", op_id)
        if op_id:
            return (
                f"Memory ACCEPTED for processing (queued). operation_id={op_id}. "
                "Fact extraction runs server-side and is not yet complete; the memory "
                "will be retrievable once that operation completes."
            )
        return (
            "Memory ACCEPTED for processing (queued). The server did not return an "
            "operation id; completion cannot be tracked by id."
        )

    def _mint_retain_operation_id(self) -> str:
        """Caller-supplied UUID for the async retain.

        The server treats a caller-supplied operation_id as the identity of the async
        operation and de-duplicates against it ("Re-submitting with the same id returns
        the original operation and creates no new work"). Minting it client-side means a
        retry after a lost acknowledgement cannot enqueue a duplicate extraction.
        """
        return str(uuid.uuid4())
'''

# _retain_batch must forward operation_id; the current body does not.
OLD_RETAIN_BATCH = '''        kwargs: Dict[str, Any] = {
            "bank_id": bank_id,
            "items": [item],
            "document_id": document_id,
            "retain_async": retain_async,
        }'''

NEW_RETAIN_BATCH = '''        kwargs: Dict[str, Any] = {
            "bank_id": bank_id,
            "items": [item],
            "document_id": document_id,
            "retain_async": retain_async,
            "operation_id": operation_id,
        }'''

OLD_SIG = '''    def _retain_batch(
        self, item: dict, *, bank_id: str, document_id: str | None = None, retain_async: bool | None = None
    ):'''

NEW_SIG = '''    def _retain_batch(
        self,
        item: dict,
        *,
        bank_id: str,
        document_id: str | None = None,
        retain_async: bool | None = None,
        operation_id: str | None = None,
    ):'''


class ReconcileError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_single(text: str, needle: str, label: str) -> int:
    n = text.count(needle)
    if n != 1:
        raise ReconcileError(f"anchor {label} matched {n} times (expected exactly 1)")
    return n


def _has_uuid_import(text: str) -> bool:
    if re.search(r"^import uuid$", text, re.MULTILINE):
        return True
    return bool(re.search(r"^from uuid import ", text, re.MULTILINE))


def apply_repair(text: str) -> tuple[str, list[str]]:
    """Return (new_text, applied_step_names). Idempotent."""
    applied: list[str] = []

    if NEW_RETAIN_TAIL in text:
        return text, ["already_repaired"]

    if OLD_RETAIN_TAIL not in text:
        raise ReconcileError("interactive _tool_retain anchor not found; refusing to guess")

    if not _has_uuid_import(text):
        # The stdlib block is alphabetically ordered and ends at `import time`,
        # immediately before the `from ...` imports. Anchor there; refuse otherwise.
        if "import time\n" not in text:
            raise ReconcileError("cannot place 'import uuid' safely; refusing to guess")
        if text.count("import time\n") != 1:
            raise ReconcileError("'import time' anchor is ambiguous; refusing to guess")
        text = text.replace("import time\n", "import time\nimport uuid\n", 1)
        applied.append("add_uuid_import")

    _require_single(text, OLD_SIG, "_retain_batch signature")
    text = text.replace(OLD_SIG, NEW_SIG)
    applied.append("widen_retain_batch_signature")

    _require_single(text, OLD_RETAIN_BATCH, "_retain_batch kwargs")
    text = text.replace(OLD_RETAIN_BATCH, NEW_RETAIN_BATCH)
    applied.append("forward_operation_id")

    _require_single(text, OLD_RETAIN_TAIL, "_tool_retain body")
    text = text.replace(OLD_RETAIN_TAIL, NEW_RETAIN_TAIL)
    applied.append("async_ack_tool_retain")

    return text, applied


def target_profiles() -> tuple[list[tuple[str, Path]], str, str]:
    """Profiles whose canonical intent flag is hindsight_required.

    Returns ``(targets, state, detail)`` where ``state`` is one of the
    profile_gate DISCOVERY_* constants.

    Two corrections, both from the DEV-parity audit:

    * ``include_dev=True``. hermes-monepeviq-dev is an operational
      Hindsight-enabled profile and consumes the same broken tool path, so it
      belongs here on exactly the same evidence as the root profiles.
    * ``Path(prof.hermes_home)``, not ``/root/.hermes/profiles/<name>``. The
      profile object already knows its real home; rebuilding it from the root
      directory was wrong for any non-root profile.

    The ``state`` matters as much as the list: ``RUNTIME_UNABLE`` must never be
    allowed to look like "no profile matches".
    """
    from profile_gate import discover_profiles_checked  # noqa: E402  (path set above)

    profiles, state, detail = discover_profiles_checked(include_dev=True)
    out: list[tuple[str, Path]] = []
    for prof in profiles:
        if not prof.hindsight_required:
            continue
        out.append((prof.name, Path(prof.hermes_home) / PLUGIN_REL))
    return out, state, detail


def preserve_owner(dst: Path, reference: Path) -> None:
    """Copy ``reference``'s owner/group onto ``dst`` in place.

    The updater runs as root, so every file this reconciler rewrites is written
    as root by definition. For a non-root profile that would silently hand a
    user's own profile plugin to root and break the very isolation that keeps
    the DEV profile independent. The content is root-written on purpose; the
    OWNERSHIP is the user's, and is restored from the file's own pre-image.
    """
    try:
        info = reference.stat()
    except OSError:
        return
    try:
        os.chown(dst, info.st_uid, info.st_gid)
    except (OSError, PermissionError):
        # Non-fatal only if we could not change it AND the owner already
        # matches; anything else is a real ownership defect and the caller
        # re-verifies via stat.
        pass
    try:
        os.chmod(dst, info.st_mode & 0o7777)
    except OSError:
        pass


def runtime_selfcheck() -> str:
    """Fail loud when THIS interpreter cannot do the job. '' when it can.

    Uses the canonical gate_runtime preflight, which validates the candidate
    interpreter in a subprocess so the answer reflects that interpreter's own
    site-packages. Direct invocation of this script is the documented way an
    operator reaches for a bare ``python3``; this is where that is caught.
    """
    from gate_runtime import GateRuntimeError, preflight  # noqa: E402

    try:
        record = preflight("CUSTOM_09")
    except GateRuntimeError as exc:
        return str(exc)
    for line in record.as_lines():
        print(line)
    return ""


def compile_check(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    try:
        compile(src, str(path), "exec")
    except SyntaxError as exc:  # pragma: no cover
        raise ReconcileError(f"syntax error in {path}: {exc}") from exc


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only; change nothing")
    args = ap.parse_args(list(argv) if argv is not None else None)

    print("CUSTOM_09_HINDSIGHT_ASYNC_ACK_RECONCILE")
    print(f"MODE={'CHECK' if args.check else 'APPLY'}")
    print(f"UTC={datetime.now(timezone.utc).isoformat()}")

    # Fail-loud gate runtime. Runs BEFORE discovery so an incapable
    # interpreter is reported as an interpreter problem, never as an empty
    # profile set. Under the dispatcher this is a redundant confirmation (the
    # dispatcher already preflighted and re-exec'd with the canonical
    # interpreter); under a direct `python3 reconcile.py` it is the only thing
    # standing between the operator and a misleading NO_HINDSIGHT_REQUIRED.
    runtime_error = runtime_selfcheck()
    if runtime_error:
        print(runtime_error)
        print("CUSTOM_09=BLOCKED_GATE_RUNTIME")
        return 3

    try:
        targets, state, detail = target_profiles()
    except Exception as exc:
        print(f"PROFILE_DISCOVERY_FAILED={exc}")
        print("CUSTOM_09=BLOCKED_SAFE")
        return 2

    print(f"PROFILE_DISCOVERY_STATE={state}")
    if detail:
        print(f"PROFILE_DISCOVERY_DETAIL={detail}")

    if state == "RUNTIME_UNABLE":
        # A broken gate must never present itself as a clean one: "no profile
        # matches" and "no profile could be read" have opposite meanings.
        print("GATE_RUNTIME_DISCOVERY_UNABLE")
        print("CUSTOM_09=BLOCKED_GATE_RUNTIME")
        return 3

    if not targets:
        print("NO_HINDSIGHT_REQUIRED_PROFILES")
        print("CUSTOM_09=BLOCKED_SAFE")
        return 2

    print(f"PROFILE_PLUGIN_COUNT={len(targets)}")

    drift = 0
    repaired = 0
    for name, path in targets:
        rel = f"{name}/{PLUGIN_REL}"
        if not path.is_file():
            print(f"  {rel}: MISSING")
            drift += 1
            continue

        original = path.read_text(encoding="utf-8")
        try:
            new_text, applied = apply_repair(original)
        except ReconcileError as exc:
            print(f"  {rel}: BLOCKED ({exc})")
            drift += 1
            continue

        if applied == ["already_repaired"]:
            print(f"  {rel}: ALREADY_REPAIRED sha={sha256(path)[:16]}")
            continue

        try:
            compile(new_text, str(path), "exec")
        except SyntaxError as exc:
            print(f"  {rel}: BLOCKED (patch would not compile: {exc})")
            drift += 1
            continue

        if args.check:
            print(f"  {rel}: WOULD_APPLY {','.join(applied)}")
            drift += 1
            continue

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_suffix(f".py.CUSTOM09.{ts}.bak")
        shutil.copy2(path, backup)
        path.write_text(new_text, encoding="utf-8")
        # Restore ownership from the PRE-IMAGE (the backup), not from the file
        # that was just rewritten: shutil.copy2 has already propagated the
        # original owner onto the backup, so it is the faithful record of who
        # owns this profile. Reading the rewritten file would be circular and
        # would happily preserve a root owner it had already taken.
        preserve_owner(backup, path)
        preserve_owner(path, backup)
        compile_check(path)
        info = path.stat()
        if info.st_uid != 0 or info.st_gid != 0:
            # Root-owned profiles (uid 0) are expected; a non-root profile
            # must have kept its own owner. Reported so the operator can see
            # the isolation was preserved rather than having to trust it.
            print(f"  {rel}: OWNER={info.st_uid}:{info.st_gid} mode={info.st_mode & 0o7777:o}")
        repaired += 1
        print(
            f"  {rel}: REPAIRED ({','.join(applied)}) "
            f"sha={sha256(path)[:16]} backup={backup.name}"
        )

    print(f"PROFILE_PLUGIN_REPAIRED={repaired}")
    print(f"PROFILE_PLUGIN_DRIFT_COUNT={drift}")

    if drift:
        print("CUSTOM_09=BLOCKED_SAFE")
        return 1
    print("CUSTOM_09=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

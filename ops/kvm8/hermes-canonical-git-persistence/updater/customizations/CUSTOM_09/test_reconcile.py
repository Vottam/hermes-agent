#!/usr/bin/env python3
"""CUSTOM_09 fail-closed fixture tests (PHASE 8).

The async repair must decouple ONLY long post-acceptance processing. Real failures must
still fail: a submission that never reached the server cannot be reported as ACCEPTED.

R1 connection refused      -> must raise, no ACK
R2 server immediate error  -> must raise, no ACK
R3 malformed response      -> must raise, no ACK
R4 invalid operation id    -> must raise, no ACK
R5 submission rejected     -> must raise, no ACK

These run entirely against fakes. No retain is issued, no network call leaves the host.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reconcile import (  # noqa: E402
    NEW_RETAIN_TAIL,
    OLD_RETAIN_BATCH,
    OLD_RETAIN_TAIL,
    OLD_SIG,
    apply_repair,
)


class ConnectionRefused(Exception):
    pass


class ServerHTTPError(Exception):
    pass


class MalformedResponse(Exception):
    pass


class InvalidOperationId(Exception):
    pass


class SubmissionRejected(Exception):
    pass


class FakeClient:
    """Stands in for hindsight_client.Hindsight.aretain_batch."""

    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def aretain_batch(self, **kwargs):
        if kwargs.get("retain_async") is not True:
            raise AssertionError("repair did not set retain_async=True")
        if not kwargs.get("operation_id"):
            raise AssertionError("repair did not supply a caller-minted operation_id")
        return self.behaviour(**kwargs)


class Resp:
    def __init__(self, operation_id=None, operation_ids=None):
        self.operation_id = operation_id
        self.operation_ids = operation_ids


class Harness:
    """Minimal stand-in the real plugin methods are bound onto.

    The real ``_retain_batch`` builds the kwargs (including retain_async and
    operation_id) and calls ``_run_hindsight_operation(lambda client:
    client.aretain_batch(...))``. Supplying ``_get_client`` is therefore enough: the
    production code path — including its kwargs filtering — is what gets exercised.
    """

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.tracked = []
        self._bank_id = "test-bank"
        self._pending_retain_ops = set()
        self._pending_retain_ops_lock = __import__("threading").Lock()
        self._retain_ops_bank_id = None

    # helpers the patched method uses
    def _build_retain_kwargs(self, content, **kw):
        return {"content": content}

    def _get_client(self):
        return FakeClient(self.behaviour)

    def _run_hindsight_operation(self, operation):
        # The real bridge is sync (it drives the coroutine on its own loop and waits
        # with a timeout). The stub must therefore RESOLVE the coroutine, not return it.
        import asyncio

        coro = operation(self._get_client())
        if asyncio.iscoroutine(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        return coro

    def _track_retain_ops(self, resp, bank_id):
        ids = [getattr(resp, "operation_id", None)] + list(getattr(resp, "operation_ids", None) or [])
        self.tracked.append([str(i) for i in ids if i])

    def _mint_retain_operation_id(self):
        import uuid

        return str(uuid.uuid4())


def build_method():
    """Load the repaired _tool_retain from a real patched plugin file.

    Most faithful harness: apply the patch to an actual copy of the deployed plugin
    source, then exec just the provider class from that patched text. The plugin's
    module-level imports need the agent runtime, so only the class body is extracted
    and bound to a minimal stand-in object.
    """
    import importlib.util
    import tempfile
    import types

    from reconcile import PLUGIN_REL

    src = Path("/root/.hermes/profiles/hermes-kvm8") / PLUGIN_REL
    patched, _ = apply_repair(src.read_text(encoding="utf-8"))

    # Extract the class that DEFINES _tool_retain plus its sibling helpers.
    #
    # The previous selector accepted any top-level `class` whose following 4000
    # lines merely MENTIONED _tool_retain. T2.1 added
    # `class ClientAdmissionError(RuntimeError)` at the top of the plugin, and
    # from that line the provider class is inside the window, so the selector
    # bound to ClientAdmissionError and main() died on StopIteration. The bug
    # was latent because nothing dispatched this test.
    #
    # `_tool_retain` must be DEFINED (`def _tool_retain`) inside the class body,
    # which is what the harness actually binds.
    lines = patched.splitlines()

    def _class_extent(idx: int) -> list[str]:
        rest = lines[idx:]
        stop = next(
            (j for j, l in enumerate(rest[1:], 1) if l and not l[0].isspace()),
            len(rest),
        )
        return rest[:stop]

    start = next(
        i
        for i, l in enumerate(lines)
        if l.startswith("class ")
        and any(
            sub.lstrip().startswith(("def _tool_retain", "async def _tool_retain"))
            for sub in _class_extent(i)
        )
    )
    class_src = "\n".join(_class_extent(start))

    mod_src = (
        "from __future__ import annotations\n"
        "import asyncio, atexit, contextlib, json, logging, os, queue, sys, threading, time, uuid\n"
        "from typing import Any, Callable, Dict, List, Optional\n"
        "MemoryProvider = object; RecallStatus = object\n"
        "def spawn_context_thread(*a, **k): raise NotImplementedError\n"
        "class UnscopedSecretError(Exception): pass\n"
        "def get_secret(*a, **k): raise NotImplementedError\n"
        "def cfg_get(*a, **k): raise NotImplementedError\n"
        "def get_hermes_home(*a, **k): raise NotImplementedError\n"
        "import logging as _lg; logger = _lg.getLogger('c09test')\n"
        "RETAIN_SCHEMA = {}; RECALL_SCHEMA = {}; REFLECT_SCHEMA = {}\n"
        "DEFAULT_TAGS = []\n"
        + class_src
        + "\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(mod_src)
        tmp = fh.name
    spec = importlib.util.spec_from_file_location("_c09_plugin", tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    Path(tmp).unlink(missing_ok=True)
    del types
    return mod


def main() -> int:
    failures: list[str] = []
    mod = build_method()
    provider_cls = next(
        v for v in vars(mod).values() if isinstance(v, type) and hasattr(v, "_tool_retain")
    )

    def bind(behaviour):
        h = Harness(behaviour)
        # Bind the real plugin methods onto the stand-in. _track_retain_ops is the
        # plugin's own (it records into _pending_retain_ops), so the real one runs.
        h._tool_retain = provider_cls._tool_retain.__get__(h)
        h._retain_batch = provider_cls._retain_batch.__get__(h)
        h._track_retain_ops = provider_cls._track_retain_ops.__get__(h)
        h._mint_retain_operation_id = provider_cls._mint_retain_operation_id.__get__(h)
        return h

    # --- positive control: real async submission returns ACCEPTED, not "stored" ----
    h = bind(lambda **kw: Resp(operation_id="op-abc-123"))
    out = h._tool_retain({"content": "hello"})
    if "ACCEPTED" not in out:
        failures.append("R0: success path did not report ACCEPTED")
    if "stored successfully" in out.lower():
        failures.append("R0: success path claimed persistence (dishonest ACK)")
    if "op-abc-123" not in out:
        failures.append("R0: operation_id missing from ACK")
    # The plugin's real _track_retain_ops records into _pending_retain_ops.
    if h._pending_retain_ops != {"op-abc-123"}:
        failures.append(
            f"R0: op id not recorded in _pending_retain_ops (got {h._pending_retain_ops!r})"
        )
    print(f"R0_async_acceptance: {'PASS' if not failures else 'FAIL'} -> {out[:78]!r}")

    # --- R1..R5: every submission failure must propagate, never ACK --------------
    cases = [
        ("R1_connection_refused", ConnectionRefused("refused")),
        ("R2_server_http_error", ServerHTTPError("500")),
        ("R3_malformed_response", MalformedResponse("no operation_id field")),
        ("R4_invalid_operation_id", InvalidOperationId("not-a-uuid")),
        ("R5_submission_rejected", SubmissionRejected("bank not found")),
    ]
    for label, exc in cases:
        def boom(_exc=exc, **kw):
            raise _exc

        h = bind(boom)
        try:
            out = h._tool_retain({"content": "x"})
        except type(exc):
            print(f"{label}: PASS (raised {type(exc).__name__}, no ACK)")
            continue
        except Exception as other:  # noqa: BLE001
            failures.append(f"{label}: raised {type(other).__name__}, expected {type(exc).__name__}")
            print(f"{label}: FAIL (wrong exception {type(other).__name__})")
            continue
        failures.append(f"{label}: returned ACK instead of raising -> {out!r}")
        print(f"{label}: FAIL (swallowed failure, returned {out[:50]!r})")

    # --- idempotency of the patcher itself ---------------------------------------
    src = _synth_source()
    once, applied1 = apply_repair(src)
    twice, applied2 = apply_repair(once)
    if twice != once:
        failures.append("P1: second apply changed the file (not idempotent)")
    if applied2 != ["already_repaired"]:
        failures.append(f"P1: second apply reported {applied2}, expected already_repaired")
    print(f"P1_patch_idempotent: {'PASS' if twice == once and applied2 == ['already_repaired'] else 'FAIL'}")

    print()
    if failures:
        for f in failures:
            print(f"FAILURE: {f}")
        print("CUSTOM_09_TESTS=FAIL")
        return 1
    print("CUSTOM_09_TESTS=PASS")
    return 0


def _synth_source() -> str:
    """A minimal source containing the real anchors, to exercise apply_repair()."""
    return (
        "from __future__ import annotations\n"
        "\n"
        "import asyncio\n"
        "import atexit\n"
        "import contextlib\n"
        "import json\n"
        "import logging\n"
        "import os\n"
        "import queue\n"
        "import sys\n"
        "import threading\n"
        "import time\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "from typing import Any, Callable, Dict, List, Optional\n"
        "\n"
        "class X:\n"
        + "\n".join("    " + ln if ln.strip() else ln for ln in [OLD_SIG.rstrip(), OLD_RETAIN_BATCH.rstrip(), OLD_RETAIN_TAIL.rstrip()])
        + "\n"
    )


if __name__ == "__main__":
    sys.exit(main())

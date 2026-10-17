#!/usr/bin/env python3
"""CUSTOM_11 — fail-closed PREFLIGHT for the explicit Hermes PM / update lifecycle.

WHAT THIS IS FOR
----------------
KVM8 T2.1 removed the implicit auto-upgrade from the Hindsight plugin's
``initialize()``. Normal initialization no longer reaches the package manager at
all, so this preflight is deliberately NOT part of it and normal startup must not
care about libatomic.

This script guards the OTHER path: an EXPLICIT ``hermes update`` / package-manager
run, which is still allowed to mutate and is still the sanctioned way to upgrade.

THE FAILURE IT PREVENTS
-----------------------
On 2026-09-27 the takeover that an ``initialize()`` accidentally triggered produced::

    historical_takeover: node: staged entry failed verification:
      /root/.hermes/tools/.staging-XXXX/tree/bin/node --version exited 127:
      error while loading shared libraries: libatomic.so.1:
      cannot open shared object file

That is a LATE failure with a real cost. Reading the installed PM code shows the
ordering inside ``pm/install.py``:

    _prepare_artifacts(...)        <- downloads + EXTRACTS into a staging tree
    reason = package.verify(...)   <- line 376: runs the staged binary; node dies here
    _publish_entry(...)            <- only reached if verify passed
    facts.record(...)              <- only reached if verify passed

So for the failing package the failure is contained (nothing is promoted, no facts
entry is written for it). But the tool closure is provisioned PER PACKAGE, and the
incident's receipt shows ``packages.ffmpeg`` promoted with a ``tools/facts.json``
write BEFORE node was attempted. The net result was a ~490 MB ffmpeg tree and a
mutated facts.json for an update that did not happen at all
(``pre_update.sha == post_update.sha``).

A missing host library is knowable BEFORE any of that. This preflight checks it
first and blocks, so an explicit update fails immediately and cleanly instead of
half-staging a tool closure.

EXACT INJECTION POINT (for a future runtime-level fix, NOT done here)
--------------------------------------------------------------------
    /opt/hermes-agent-clean/hermes_cli/_update_takeover.py:30
        ensure_tools_for_sync()          <- first thing the takeover provisions
        ...
        sync_venv(...)                   <- the per-package staging loop

A runtime-level preflight belongs immediately before ``ensure_tools_for_sync()``.
T2.1 does not edit runtime Hermes source: that is a fork of the whole
``hermes-agent`` repository and is a separate, larger change with its own
convergence path. This script is the host-level gate, invoked by the operator or
by a wrapper before an explicit update.

DECISION: libatomic is NOT installed by T2.1
--------------------------------------------
The task forbids it, and installing a system library so that an implicit
auto-upgrade can proceed would be exactly backwards: the missing library is what
made the implicit path fail, and the repair is to remove the implicit path, not to
unblock it. If an authorized explicit update genuinely needs libatomic, that is a
host-level decision taken separately, with its own rollback.

Usage:
    libatomic_preflight.py            # report only
    libatomic_preflight.py --strict   # exit 2 when unavailable (block the update)
"""

from __future__ import annotations

import argparse
import ctypes.util
import json
import subprocess
import sys
from datetime import datetime, timezone

LIB = "libatomic.so.1"
TAKEOVER_ENTRYPOINT = "/opt/hermes-agent-clean/hermes_cli/_update_takeover.py"
STAGING_VERIFY_POINT = "/opt/hermes-agent-clean/pm/install.py:376"
PM_TOOL_STAGING = "/opt/hermes-agent-clean/hermes_cli/_update_takeover.py:30 (ensure_tools_for_sync)"


def in_ldconfig() -> bool:
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=30)
        return any(line.split()[0] == LIB for line in out.stdout.splitlines() if line.strip())
    except Exception:
        return False


def loadable() -> bool:
    """Stronger than ldconfig: ask the dynamic loader to actually find it."""
    try:
        return ctypes.util.find_library("atomic") is not None
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when the dependency is missing (use to block an explicit update)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ld = in_ldconfig()
    found = loadable()
    available = ld or found

    res = {
        "check": "CUSTOM_11_LIBATOMIC_PREFLIGHT",
        "utc": datetime.now(timezone.utc).isoformat(),
        "library": LIB,
        "LIBATOMIC_AVAILABLE": "yes" if available else "no",
        "in_ldconfig": ld,
        "find_library": found,
        "pm_tool_staging_entrypoint": PM_TOOL_STAGING,
        "staging_verify_point": STAGING_VERIFY_POINT,
        "takeover_entrypoint": TAKEOVER_ENTRYPOINT,
    }

    if available:
        res["verdict"] = "PASS"
        res["detail"] = f"{LIB} is present; the explicit PM/tool path may stage its tool closure."
        code = 0
    else:
        res["verdict"] = "BLOCKED_SAFE"
        res["detail"] = (
            f"{LIB} is NOT available on this host. The Hermes package manager's tool staging "
            f"verifies each staged tool by RUNNING it (pm/install.py:376), and the managed node "
            f"build exits 127 without this library. Because the tool closure is provisioned per "
            f"package, a late failure still promotes earlier tools and writes tools/facts.json "
            f"for an update that then does not apply -- exactly the 2026-09-27 incident "
            f"(pre_update.sha == post_update.sha, ffmpeg promoted, facts.json written). "
            f"Block here, before {PM_TOOL_STAGING}, instead. Installing libatomic is a separate, "
            f"separately authorized host-level decision and was NOT done by T2.1. "
            f"NOTE: this does not affect normal Hermes startup or the Hindsight plugin: T2.1 made "
            f"initialization non-mutating, so no read-only path reaches tool staging at all."
        )
        code = 2 if args.strict else 0

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for k, v in res.items():
            print(f"{k}={v}")
    return code


if __name__ == "__main__":
    sys.exit(main())

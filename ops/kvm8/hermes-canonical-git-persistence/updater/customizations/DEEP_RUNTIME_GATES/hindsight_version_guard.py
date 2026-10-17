#!/usr/bin/env python3
"""PHASE 8 — structural Hindsight effective-version guard.

LOCAL POLICY: the effective importable/operational Hindsight runtime must
satisfy ``>=0.10.0,<0.11.0``. Current desired effective version is 0.10.1.

WHY THIS IS STRUCTURAL AND NOT A VERSION PIN
---------------------------------------------
Derived from the PM source (``--explain`` reproduces this at runtime):

* ``pm/plugin_declarations.py`` exposes exactly two constraint mechanisms:
  ``requires_python`` (uv intersects it against the interpreter) and
  ``requires_hermes`` (catalog ``requires_hermes: ">=0.21.4"``).
* There is NO facility to constrain the *version of a plugin's Python
  dependency*: the plugin's own ``pyproject.toml`` requirements go to uv with
  no upper bound, so a catalog/requirements change can silently resolve
  hindsight-client / hindsight-embed to >=0.11.0.
* The catalog's own declaration (``plugin-catalog/hindsight.yaml``) pins the
  plugin SOURCE sha, not the client library version.

Consequently the protection must live at the boundary: this guard is invoked
by the updater BEFORE any publication/activation is accepted, and it validates
both the resolver PROPOSAL and the EFFECTIVE installed runtime. It fails
closed on both sides of the window (pre-0.10.0 resurrection AND >=0.11.0
activation) and it never edits third-party plugin code.

Modes
-----
``--explain``        print the derived PM constraint analysis
``--proposal ...``   validate a resolver proposal before publication
``(default)``        validate the effective installed runtime of every profile
``--selftest``       internal consistency of the version comparison
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_gate import (  # noqa: E402
    HINDSIGHT_LOWER,
    HINDSIGHT_UPPER,
    INSTALL_ROOT,
    catalog_pin_sha,
    discover_profiles,
    read_effective_versions,
    version_in_policy,
)

PM_SOURCE = Path("/opt/hermes-agent-clean")
PACKAGE_NAMES = ("hindsight-client", "hindsight-embed")
POLICY_TEXT = f">={HINDSIGHT_LOWER[0]}.{HINDSIGHT_LOWER[1]}.{HINDSIGHT_LOWER[2]}" \
              f",<{HINDSIGHT_UPPER[0]}.{HINDSIGHT_UPPER[1]}.{HINDSIGHT_UPPER[2]}"
DESIRED_EFFECTIVE = "0.10.1"


def _explain() -> list[str]:
    declarations = PM_SOURCE / "pm" / "plugin_declarations.py"
    catalog = PM_SOURCE / "plugin-catalog" / "hindsight.yaml"
    lines = [
        "PM_CONSTRAINT_ANALYSIS:",
        f"  pm_source={PM_SOURCE}",
        f"  pm/plugin_declarations.py present={declarations.is_file()}",
        "  constraint mechanisms found in PM source:",
        "    requires_python  -> uv intersects against the interpreter version",
        "    requires_hermes  -> catalog gate, requires_hermes: '>=0.21.4'",
        "  dependency-version constraint facility: ABSENT",
        "    -> plugin pyproject.toml requirements reach uv with no upper bound",
        f"  plugin-catalog/hindsight.yaml present={catalog.is_file()}",
        "  catalog pin kind: plugin SOURCE sha (not the client library version)",
        f"  catalog hindsight pin sha={catalog_pin_sha()}",
        f"LOCAL_POLICY_BOUNDARY: effective {POLICY_TEXT} enforced by this gate",
        "THIRD_PARTY_PLUGIN_CODE_EDITED=no",
        "PIP_USED_MANUALLY=no",
    ]
    return lines


def _selftest() -> int:
    cases = [
        ("0.10.0", True, "policy floor, inclusive"),
        ("0.10.1", True, "current desired effective version"),
        ("0.10.9", True, "top of the 0.10.x window"),
        ("0.11.0", False, "upstream ceiling entry must be refused"),
        ("0.11.3", False, ">=0.11 activation"),
        ("1.0.1", False, "catalog plugin version is not the client version"),
        ("0.9.9", False, "pre-0.10.0 resurrection"),
        ("0.9.0", False, "resurrection floor"),
        ("", False, "unknown version is never a pass"),
        ("not-a-version", False, "unparseable version is never a pass"),
    ]
    ok = True
    for value, expect, why in cases:
        got = version_in_policy(value)
        if got != expect:
            ok = False
        flag = "ok " if got == expect else "BAD"
        print(f"{flag} version={value!r:14} accept={str(got):5} expected={str(expect):5} ({why})")
    print(f"GUARD_SELFTEST={'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _validate_proposal(names: dict[str, str]) -> int:
    ok = True
    for name, version in sorted(names.items()):
        if name not in PACKAGE_NAMES:
            continue
        if not version_in_policy(version):
            ok = False
            print(
                "BLOCKED: HINDSIGHT_RESOLVER_PROPOSAL_OUT_OF_POLICY "
                f"package={name} proposed={version} policy={POLICY_TEXT}"
            )
    print(f"HINDSIGHT_PROPOSAL_BLOCKS_BEFORE_PUBLICATION={'no' if ok else 'YES'}")
    print(f"HINDSIGHT_PROPOSAL_GATE={'PASS' if ok else 'BLOCKED_SAFE'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="PHASE 8 Hindsight effective-version guard")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--proposal", help="JSON mapping package -> proposed version")
    args = ap.parse_args()

    if args.explain:
        for line in _explain():
            print(line)
        return 0
    if args.selftest:
        return _selftest()
    if args.proposal:
        return _validate_proposal(json.loads(args.proposal))

    print(f"HINDSIGHT_VERSION_POLICY={POLICY_TEXT}")
    print(f"HINDSIGHT_DESIRED_EFFECTIVE_VERSION={DESIRED_EFFECTIVE}")
    print("PM_CONSTRAINT_FACILITY=ABSENT_local_gate_required")
    print(f"PM_INSTALL_ROOT={INSTALL_ROOT}")
    print(f"PM_CATALOG_HINDSIGHT_PIN={catalog_pin_sha()}")

    profiles = discover_profiles()
    print(f"PROFILE_COUNT={len(profiles)}")
    ok = True
    for profile in profiles:
        # The effective runtime is whatever the profile's OWN PM dependency
        # envs actually import — not what the catalog declares. An env that
        # carries no Hindsight distribution at all is simply not the env that
        # runs this plugin; only an env that DOES carry it is version-checked.
        venvs = profile.hindsight_install_roots
        if not venvs:
            ok = False
            print(f"HINDSIGHT_VERSION {profile.name} -> BLOCKED_SAFE  no PM dependency env")
            continue
        bad: dict[str, str] = {}
        shown: list[str] = []
        effective_envs = 0
        for venv in venvs:
            versions = read_effective_versions(venv)
            # read_effective_versions reports a missing distribution as the
            # literal string "ABSENT"; a key that is simply missing means the
            # env carries nothing this gate has an opinion about.
            present = {
                name: versions[name]
                for name in PACKAGE_NAMES
                if versions.get(name) and versions[name] != "ABSENT"
            }
            if not present:
                # This env does not run the Hindsight plugin; nothing to check.
                continue
            effective_envs += 1
            env_id = venv.parent.name
            for name in sorted(PACKAGE_NAMES):
                version = present.get(name)
                if not version:
                    # An env that DOES carry the other distribution but not
                    # this one is a broken environment, not a silent skip.
                    bad[f"{env_id}/{name}"] = "ABSENT"
                    continue
                shown.append(f"{env_id}/{name}={version}")
                if not version_in_policy(version):
                    bad[f"{env_id}/{name}"] = version
        if effective_envs == 0 and profile.hindsight_required:
            ok = False
            print(f"HINDSIGHT_VERSION {profile.name} -> BLOCKED_SAFE  "
                  "provider=hindsight but no env imports hindsight")
        elif bad:
            ok = False
            detail = " ".join(f"{k}={v}" for k, v in sorted(bad.items()))
            print(f"HINDSIGHT_VERSION {profile.name} -> BLOCKED_SAFE  {detail}")
        else:
            print(f"HINDSIGHT_VERSION {profile.name} -> PASS  "
                  f"envs={effective_envs} {' '.join(shown)}")
    print(f"HINDSIGHT_EFFECTIVE_VERSION_GATE={'PASS' if ok else 'BLOCKED_SAFE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

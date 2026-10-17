#!/usr/bin/env python3
"""G1 — per-profile Hindsight provider/plugin consistency gate.

Eligibility is refused unless, for every distinct operational profile, a
declared `memory.provider: hindsight` with memory enabled has:
  - the plugin installed in that profile's OWN plugin home,
  - the plugin enabled,
  - a pinned install record whose revision matches the catalog pin,
  - no residual .install-* staging,
  - a PM dependency environment carrying the required distributions, with
    every effective version inside >=0.10.0,<0.11.0.

Fail-closed: an unclassifiable profile is FAIL, never a PASS by omission.
A profile with memory disabled is explicitly classified HINDSIGHT_REQUIRED=no
and is not failed for the absence of the plugin.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_gate import (  # noqa: E402
    HINDSIGHT_LOWER,
    HINDSIGHT_UPPER,
    Profile,
    catalog_pin_sha,
    discover_profiles,
    read_effective_versions,
    version_in_policy,
)

REQUIRED_DISTRIBUTIONS = ("hindsight-client", "hindsight-embed")

# When a profile's memory provider is active, at least one PM dependency env
# must carry the required distributions. The core env legitimately carries none
# (that is the false-ABSENT trap documented in G2), so the gate requires the
# set across ALL envs of that profile's store to be satisfied, not any single.
MIN_ENVS_WITH_DEPS = 1


def check_effective_versions(profile: Profile) -> tuple[bool, list[str], dict[str, str]]:
    reasons: list[str] = []
    versions: dict[str, str] = {}
    if not profile.hindsight_required:
        return True, reasons, versions

    roots = profile.hindsight_install_roots
    if not roots:
        reasons.append("PM_DEPENDENCY_STORE_MISSING")
        return False, reasons, versions

    envs_with_deps = 0
    for venv in roots:
        found = read_effective_versions(venv)
        if not found:
            continue
        env_id = venv.parent.name
        complete = True
        for dist in REQUIRED_DISTRIBUTIONS:
            version = found.get(dist, "ABSENT")
            versions[f"{env_id}:{dist}"] = version
            if version == "ABSENT":
                # Not every env carries the plugin deps; only a declared-absent
                # env that DOES carry the other dist is a policy failure.
                continue
            if not version_in_policy(version):
                reasons.append(
                    f"HINDSIGHT_VERSION_OUT_OF_POLICY:{dist}={version}"
                    f":policy=>={HINDSIGHT_LOWER[0]}.{HINDSIGHT_LOWER[1]}.{HINDSIGHT_LOWER[2]}"
                    f",<{HINDSIGHT_UPPER[0]}.{HINDSIGHT_UPPER[1]}.{HINDSIGHT_UPPER[2]}")
                complete = False
        if any(found.get(dist, "ABSENT") != "ABSENT" for dist in REQUIRED_DISTRIBUTIONS):
            envs_with_deps += 1
            for dist in REQUIRED_DISTRIBUTIONS:
                if found.get(dist, "ABSENT") == "ABSENT":
                    reasons.append(f"HINDSIGHT_DIST_INCOMPLETE:{env_id}:{dist}=ABSENT")
                    complete = False
            if not complete:
                pass

    if envs_with_deps < MIN_ENVS_WITH_DEPS:
        reasons.append("HINDSIGHT_DEPENDENCY_ENV_ABSENT")
    return not reasons, reasons, versions


def check_profile(profile: Profile, expected_pin: str) -> tuple[bool, list[str], dict[str, str]]:
    reasons = list(profile.failure_reasons)

    if not profile.classification_ok:
        return False, reasons or ["PROFILE_UNCLASSIFIABLE"], {}

    if profile.hindsight_required and expected_pin and profile.revision != expected_pin:
        reasons.append(
            f"CATALOG_PIN_UNREADABLE_EXPECTED={expected_pin}:GOT={profile.revision}")

    ok_versions, version_reasons, versions = check_effective_versions(profile)
    reasons.extend(version_reasons)
    if not ok_versions:
        return False, reasons, versions
    return not reasons, reasons, versions


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-dev", choices=("yes", "no"), default="yes")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    expected_pin = catalog_pin_sha()
    if not expected_pin:
        if not args.quiet:
            print("CATALOG_PIN=UNREADABLE")
            print("G1_PROFILE_PROVIDER_PLUGIN_GATE=BLOCKED_SAFE")
            print("BLOCK_REASON=CATALOG_PIN_UNREADABLE")
        return 2

    profiles = discover_profiles(include_dev=(args.include_dev == "yes"))
    results: list[tuple[Profile, bool, list[str]]] = []
    matrix: list[dict[str, object]] = []

    for profile in profiles:
        ok, reasons, versions = check_profile(profile, expected_pin)
        results.append((profile, ok, reasons))
        row = profile.to_dict()
        row["VERSIONS_OK"] = "yes" if not [r for r in reasons if "VERSION" in r] else "no"
        row["RESULT"] = "PASS" if ok else "FAIL"
        matrix.append(row)

    passed = [item for item in results if item[1]]
    failed = [item for item in results if not item[1]]

    if args.json:
        import json
        print(json.dumps({
            "catalog_pin": expected_pin,
            "policy": ">=0.10.0,<0.11.0",
            "matrix": matrix,
            "passed": len(passed),
            "failed": len(failed),
        }, indent=2, sort_keys=True))
    else:
        print(f"CATALOG_PIN={expected_pin}")
        print("HINDSIGHT_VERSION_POLICY=>=0.10.0,<0.11.0")
        print(f"PROFILE_COUNT={len(results)}")
        for profile, ok, reasons in results:
            marker = "PASS" if ok else "FAIL"
            print(f"PROFILE_GATE {profile.name} [{profile.user}] {marker}")
            for reason in reasons:
                print(f"  REASON {profile.name}: {reason}")
        print(f"PROFILE_PASS_COUNT={len(passed)}")
        print(f"PROFILE_FAIL_COUNT={len(failed)}")

    if not results:
        if not args.quiet:
            print("G1_PROFILE_PROVIDER_PLUGIN_GATE=BLOCKED_SAFE")
            print("BLOCK_REASON=NO_PROFILES_CLASSIFIED")
        return 2
    if failed:
        if not args.quiet:
            print("G1_PROFILE_PROVIDER_PLUGIN_GATE=BLOCKED_SAFE")
            print("BLOCK_REASON=PROFILE_PROVIDER_PLUGIN_INCONSISTENT")
        return 1
    if not args.quiet:
        print("G1_PROFILE_PROVIDER_PLUGIN_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

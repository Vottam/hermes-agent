#!/usr/bin/env python3
"""Fase 9 — Sandbox explicit rollback, no production mutation.

Builds an isolated release/current structure equivalent to production, then
invokes the EXACT admission implementation the updater uses
(lifecycle.classify_transition via its own CLI) and the EXACT source-mode
transaction the updater uses (lifecycle.execute_source_mode_transaction).

Starting state mirrors the certified production:
    current      = prod-0.10.1+mastertrend.t22.1
    source mode  = MASTER_TREND_FORK
Required end state:
    current      = prod-0.10.1   (official)
    source mode  = OFFICIAL_UPSTREAM
    and the version-order guard was bypassed ONLY because
    operation_mode=EXPLICIT_ROLLBACK.

Nothing here touches /opt/hindsight-kvm8.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

LIB = "/opt/mastertrend-hermes/customizations/CUSTOM_15"
PY = "/usr/bin/python3.11"
sys.path.insert(0, LIB)
import lifecycle as L  # noqa: E402

FORK = "0.10.1+mastertrend.t22.1"
OFFICIAL = "0.10.1"

results = []


def rec(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))


def mkrelease(root, name, version):
    """A release with its own venv whose package reports `version`."""
    rel = os.path.join(root, name)
    venv = os.path.join(rel, "venv")
    os.makedirs(os.path.join(venv, "bin"), exist_ok=True)
    subprocess.run([PY, "-m", "venv", "--without-pip", venv], capture_output=True)
    # a stand-in dist-info so importlib.metadata reports the exact version
    name_ = "hindsight_api_slim-" + version
    di = os.path.join(venv, "lib", "python%d.%d" % sys.version_info[:2],
                      "site-packages", name_ + ".dist-info")
    os.makedirs(di, exist_ok=True)
    with open(os.path.join(di, "METADATA"), "w") as fh:
        fh.write("Metadata-Version: 2.1\nName: hindsight-api-slim\nVersion: %s\n" % version)
    return rel


def main():
    root = tempfile.mkdtemp(prefix="c15-sbx-")
    try:
        base = os.path.join(root, "hindsight")
        releases = os.path.join(base, "releases")
        os.makedirs(releases)
        fork = mkrelease(releases, "prod-" + FORK, FORK)
        official = mkrelease(releases, "prod-" + OFFICIAL, OFFICIAL)

        current = os.path.join(base, "current")
        os.symlink(fork, current)
        dropin_dir = os.path.join(root, "unit.d")
        os.makedirs(dropin_dir)
        dropin = os.path.join(dropin_dir, "10-source-mode.conf")
        with open(dropin, "w") as fh:
            fh.write("Environment=HINDSIGHT_SOURCE_MODE=MASTER_TREND_FORK\n")

        print("SANDBOX_START current=%s source=MASTER_TREND_FORK"
              % os.path.basename(os.path.realpath(current)))

        # --- admission through the real CLI (exactly what the shell calls) ---
        out = subprocess.run(
            [PY, os.path.join(LIB, "lifecycle.py"),
             "--current", FORK, "--target", OFFICIAL,
             "--operation-mode", "EXPLICIT_ROLLBACK",
             "--source-mode", "OFFICIAL_UPSTREAM",
             "--root", releases, "--target-explicit", "True"],
            capture_output=True, text=True)
        adm = json.loads(out.stdout.strip())
        rec("SBX1_admitted_via_exact_cli", adm["allowed"] and adm["bypasses_regression"],
            out.stdout.strip())
        rec("SBX2_bypass_attributed_to_EXPLICIT_ROLLBACK",
            "operation_mode=EXPLICIT_ROLLBACK" in adm["reasons"],
            "; ".join(adm["reasons"]))

        # --- the transaction the updater performs ---
        res = L.execute_source_mode_transaction(
            base_dir=base, current_link=current, target_release=official,
            previous_release=fork, desired_source="OFFICIAL_UPSTREAM",
            source_mode_dropin=dropin, restart=False)
        rec("SBX3_transaction_succeeded", res.ok, f"steps={res.steps}")

        end_current = os.path.basename(os.path.realpath(current))
        end_source = "OFFICIAL_UPSTREAM" if "OFFICIAL_UPSTREAM" in open(dropin).read() \
            else "MASTER_TREND_FORK"
        rec("SBX4_current_is_official", end_current == "prod-" + OFFICIAL, end_current)
        rec("SBX5_source_mode_is_OFFICIAL_UPSTREAM",
            end_source == "OFFICIAL_UPSTREAM", end_source)
        rec("SBX6_no_mixed_state",
            not (end_current.startswith("prod-0.10.1+") and end_source == "OFFICIAL_UPSTREAM"),
            "official runtime + official desired source")

        # --- rollback-on-failure: a FAILING transaction restores BOTH ---
        os.unlink(current)
        os.symlink(fork, current)
        with open(dropin, "w") as fh:
            fh.write("Environment=HINDSIGHT_SOURCE_MODE=MASTER_TREND_FORK\n")
        res2 = L.execute_source_mode_transaction(
            base_dir=base, current_link=current, target_release=official,
            previous_release=None, desired_source="OFFICIAL_UPSTREAM",
            source_mode_dropin=dropin, restart=True, service_check=lambda: False)
        back_runtime = os.path.basename(os.path.realpath(current)) == "prod-" + FORK
        back_source = "MASTER_TREND_FORK" in open(dropin).read()
        rec("SBX7_failed_transaction_restores_both",
            (not res2.ok) and back_runtime and back_source,
            f"runtime_back={back_runtime} source_back={back_source} steps={res2.steps}")

        # --- Phase 10: the SAME transition without the flag is blocked ---
        out2 = subprocess.run(
            [PY, os.path.join(LIB, "lifecycle.py"),
             "--current", FORK, "--target", OFFICIAL,
             "--operation-mode", "NORMAL_UPDATE",
             "--source-mode", "OFFICIAL_UPSTREAM",
             "--root", releases, "--target-explicit", "True"],
            capture_output=True, text=True)
        adm2 = json.loads(out2.stdout.strip())
        rec("SBX8_normal_downgrade_STILL_BLOCKED",
            (not adm2["allowed"]) and not adm2["bypasses_regression"]
            and adm2["classification"] == "BLOCKED_DOWNGRADE",
            out2.stdout.strip())

        w = max(len(n) for n, _, _ in results)
        failed = 0
        for n, s, d in results:
            if s == "FAIL":
                failed += 1
            print(f"{n.ljust(w)}  {s}  :: {d}")
        print()
        print(f"SANDBOX_EXPLICIT_ROLLBACK="
              f"{'PASS' if all(s == 'PASS' for _, s, _ in results) else 'FAIL'}")
        print(f"NORMAL_DOWNGRADE_STILL_BLOCKED="
              f"{'yes' if results[-1][1] == 'PASS' else 'no'}")
        return 1 if failed else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

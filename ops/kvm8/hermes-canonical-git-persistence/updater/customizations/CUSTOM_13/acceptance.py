#!/usr/bin/env python3
"""CUSTOM_13 acceptance gate — canonical fork release path + updater source mode.

Proves the fork is DURABLE, not merely built: the same canonical updater that
moves production can install it from scratch, recognize its exact version,
verify its provenance, and fail closed when the artifact is missing or changed.

The gate is written to FAIL on a healthy-looking but non-durable fork. Every
check reads the LIVE updater and the LIVE artifact repository, not a copy.

Interprete: /opt/hindsight-kvm8/current/venv/bin/python (tem packaging).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys
import zipfile

UPDATER = pathlib.Path("/usr/local/bin/hindsight-update-safe.sh")
REPO = pathlib.Path("/opt/hindsight-kvm8/packages/releases")
WHEEL = REPO / "hindsight_api_slim-0.10.1+mastertrend.t22.1-py3-none-any.whl"
MANIFEST = REPO / "0.10.1+mastertrend.t22.1.manifest.json"
AUDIT_DIRS = sorted(
    p for p in pathlib.Path("/root/hermes-audits").glob("kvm8-hindsight-t2-5-*") if p.is_dir()
)

FORK_VERSION = "0.10.1+mastertrend.t22.1"
BASE_VERSION = "0.10.1"
EXPECTED_SHA = "bfb9b92da5b49d555e0d6030302705170ea57784f530115508949b5ad0f330bb"
FORK_COMMIT = "a4397db59d2a7eb5a859644a20eb6bb8816ffcca"
BASE_COMMIT = "f8950b0c07d9e34c76493dba802bb309f0ce60fd"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    return bool(ok)


def updater_text() -> str:
    return UPDATER.read_text()


# ── 1. The fork has a DISTINCT, VALID package version ──────────────────────
try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except ImportError as exc:  # pragma: no cover
    check("packaging_available", False, str(exc))
    raise SystemExit(2)

try:
    v = Version(FORK_VERSION)
    check("fork_version_parses", True, str(v))
    check("fork_version_is_local", v.local is not None, f"local={v.local}")
    check(
        "fork_version_distinct_from_official",
        str(v) != BASE_VERSION and str(v) != "0.10.1",
        f"{v} != {BASE_VERSION}",
    )
    check("fork_version_ge_base", v > Version(BASE_VERSION), f"{v} > {BASE_VERSION}")
    check("fork_version_lt_next_minor", v < Version("0.11.0"), f"{v} < 0.11.0")
    check(
        "fork_version_roundtrip_exact",
        str(Version(str(v))) == FORK_VERSION,
        f"str(Version(str)) == {FORK_VERSION}",
    )
    spec = SpecifierSet(">=0.10.0,<0.11.0")
    check("fork_version_in_policy_range", FORK_VERSION in spec, ">=0.10.0,<0.11.0")
    check("policy_ceiling_not_bypassed", "0.11.0" not in spec, "0.11.0 excluded")
    check("policy_floor_enforced", "0.9.9" not in spec, "0.9.9 excluded")
except Exception as exc:  # InvalidVersion and any parse failure
    check("fork_version_parses", False, str(exc))


# ── 2. The artifact is immutable and hash-verified ────────────────────────
if check("wheel_exists", WHEEL.is_file(), str(WHEEL)):
    actual = hashlib.sha256(WHEEL.read_bytes()).hexdigest()
    check("wheel_sha256_matches_pinned", actual == EXPECTED_SHA, actual)
    st = WHEEL.stat()
    import os

    mode = st.st_mode & 0o777
    check("wheel_not_world_writable", not (mode & 0o002), oct(mode))
    check("wheel_not_group_writable", not (mode & 0o020), oct(mode))
    check("wheel_root_owned", st.st_uid == 0, f"uid={st.st_uid}")
    with zipfile.ZipFile(WHEEL) as z:
        meta = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")][0]
        wheel_version = next(
            line.split(":", 1)[1].strip()
            for line in z.read(meta).decode().splitlines()
            if line.startswith("Version:")
        )
        names = z.namelist()
    check("wheel_embedded_version_is_fork", wheel_version == FORK_VERSION, wheel_version)
    check(
        "t2_2_telemetry_module_in_wheel",
        "hindsight_api/engine/retain/llm_stage_telemetry.py" in names,
        "llm_stage_telemetry.py",
    )
    # Payload identity: the fork payload must be exactly the certified source.
    payload = {
        "hindsight_api/engine/retain/llm_stage_telemetry.py":
            "0f80dbd8db25d9753e0617e72c67587faf68f495288df04907879ecb8706df26",
        "hindsight_api/engine/llm_wrapper.py":
            "02c2ffc694c5a520a66b8f57cae86da6851076231f5dafdd04ad8f6bdf45897c",
        "hindsight_api/engine/retain/fact_extraction.py":
            "74bd461d55f0240e63651e9a1c19c309456956e64d4643afc0a21409cf9055aa",
    }
    with zipfile.ZipFile(WHEEL) as z:
        for member, want in payload.items():
            got = hashlib.sha256(z.read(member)).hexdigest()
            check(f"payload_sha256:{member.rsplit('/', 1)[-1]}", got == want, got[:16])


# ── 3. The manifest records provenance ────────────────────────────────────
if check("manifest_exists", MANIFEST.is_file(), str(MANIFEST)):
    try:
        m = json.loads(MANIFEST.read_text())
    except json.JSONDecodeError as exc:
        check("manifest_parses", False, str(exc))
        m = {}
    else:
        check("manifest_parses", True, "valid JSON")
    for key, want in [
        ("package_version", FORK_VERSION),
        ("wheel_filename", WHEEL.name),
        ("wheel_sha256", EXPECTED_SHA),
        ("fork_source_commit", FORK_COMMIT),
        ("upstream_base_commit", BASE_COMMIT),
        ("upstream_base_version", BASE_VERSION),
    ]:
        check(f"manifest_field:{key}", m.get(key) == want, f"{m.get(key)!r}")
    series = m.get("patch_series")
    check(
        "manifest_patch_series",
        isinstance(series, list) and len(series) == 2 and series[-1] == FORK_COMMIT,
        str(series),
    )
    feats = m.get("t2_features") or {}
    check("manifest_t2_2_present", feats.get("T2_2_QUEUE_TELEMETRY") == "yes", "")
    check("manifest_t2_3_absent", feats.get("T2_3_MEMBER_DEADLINE") == "no", "")
    check("manifest_t2_4_absent", feats.get("T2_4_FALLBACK_BUDGET") == "no", "")


# ── 4. The UPDATER has the source-mode logic (reads the LIVE script) ─────
if check("updater_exists", UPDATER.is_file(), str(UPDATER)):
    src = updater_text()
    required = {
        "source_mode_constant": r'HINDSIGHT_SOURCE_MODE="\$\{HINDSIGHT_SOURCE_MODE:-OFFICIAL_UPSTREAM\}"',
        "fork_version_pin": r'HINDSIGHT_FORK_PACKAGE_VERSION="\$\{HINDSIGHT_FORK_PACKAGE_VERSION:-0\.10\.1\+mastertrend\.t22\.1\}"',
        "fork_sha_pin": r'HINDSIGHT_FORK_EXPECTED_SHA256="\$\{HINDSIGHT_FORK_EXPECTED_SHA256:-bfb9b92d',
        "provenance_gate_fn": r'^hindsight_source_provenance_gate\(\)',
        "policy_gate_fn": r'^hindsight_target_in_policy\(\)',
        "no_silent_fallback": r'SILENT_PYPI_FALLBACK=NO',
        "fail_closed_verdict": r'BLOCKED_SOURCE_PROVENANCE',
        "hash_mismatch_verdict": r'BLOCK_WHEEL_HASH_MISMATCH',
        "missing_wheel_verdict": r'BLOCK_WHEEL_MISSING',
        "manifest_corrupt_verdict": r'BLOCK_MANIFEST_CORRUPT',
        "fork_identity_gate": r'FORK_IDENTITY_VERDICT=PASS_EXACT_MATCH',
        "identity_block": r'BLOCKED_FORK_VERSION_IDENTITY',
        "auto_converge_off": r'HINDSIGHT_AUTO_CONVERGE_TO_UPSTREAM:-no',
        "rebase_required_signal": r'FORK_REBASE_REQUIRED=yes',
        "upstream_not_installed": r'UPSTREAM_UPDATE_AVAILABLE_BUT_NOT_INSTALLED=yes',
        "fork_target_flag": r'--fork-target\)',
        "source_mode_flag": r'--source-mode\)',
        "explicit_rollback_flag": r'--rollback-to-official\)',
        "local_wheel_path_install": r'LOCAL_PACKAGE_SOURCE_MODE|LOCAL_WHEEL_PATH=',
    }
    for name, pattern in required.items():
        check(f"updater_has:{name}", re.search(pattern, src, re.M) is not None, pattern)

    # The provenance gate must be invoked BEFORE the install path exists.
    gate_at = src.find("PROVENANCE_OUTPUT=\"$(hindsight_source_provenance_gate")
    install_at = src.find('"${CANDIDATE_DIR}/venv/bin/pip" install')
    check(
        "gate_runs_before_install",
        gate_at != -1 and install_at != -1 and gate_at < install_at,
        f"gate@{gate_at} install@{install_at}",
    )

    # Fork mode must NOT adopt a discovered official version as the target.
    check(
        "fork_mode_does_not_adopt_upstream",
        re.search(
            r'HINDSIGHT_SOURCE_MODE\}" == "MASTER_TREND_FORK".*?TARGET_VERSION="\$\{HINDSIGHT_FORK_PACKAGE_VERSION\}"',
            src,
            re.S,
        )
        is not None,
        "UPDATE_AVAILABLE branch pinned to fork",
    )

    # No audit-path dependency may be introduced for the artifact.
    check(
        "artifact_not_from_audit_dir",
        not re.search(r'hermes-audits[^\n]*(\.whl|packages/releases)', src),
        "no audit-path wheel source",
    )

    # base_version must be wrapped in Version() — the str/Version TypeError bug.
    check(
        "upstream_obs_comparison_fixed",
        re.search(r"up_line = Version\(up\.base_version\)", src) is not None,
        "Version(up.base_version)",
    )


# ── 5. The artifact must be reconstructible from the exact source commit ──
fork_repo = pathlib.Path("/opt/hindsight-kvm8/source/hindsight-upstream")
if check("fork_repo_exists", fork_repo.is_dir(), str(fork_repo)):
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(fork_repo), *args],
            capture_output=True, text=True, check=False,
        ).stdout.strip()

    check("fork_repo_clean", git("status", "--porcelain=v1") == "", "clean tree")
    head = git("rev-parse", "HEAD")
    check("fork_repo_head_is_certified", head == FORK_COMMIT, head)
    parents = git("rev-list", "--parents", "-n", "1", FORK_COMMIT).split()
    # HEAD's direct parent is the FIRST T2.2 commit (0241b64f...); the upstream
    # base (f8950b0c...) is HEAD's GRANDparent. Asserting parents[2] == BASE_COMMIT
    # would test the wrong thing — a two-commit series means the base is two
    # hops back, not one.
    check(
        "t2_2_direct_parent_is_series_head",
        len(parents) == 2 and parents[1] == "0241b64fa707f36536beb2464a09c1ff48624e8d",
        f"parent={parents[1] if len(parents) > 1 else 'NONE'}",
    )
    grandparent = git("rev-parse", f"{FORK_COMMIT}~2").strip()
    check(
        "t2_2_grandparent_is_upstream_base",
        grandparent == BASE_COMMIT,
        f"{FORK_COMMIT}~2 = {grandparent}",
    )
    series = git("rev-list", "--reverse", f"{BASE_COMMIT}..{FORK_COMMIT}").splitlines()
    check("t2_2_series_is_two_commits", len(series) == 2, f"{len(series)} commits")


# ── 6. Certification evidence must exist (not just be asserted) ───────────
if AUDIT_DIRS:
    aud = AUDIT_DIRS[0]
    evidence = {
        "T2_2_CANARY.md": "t2_2_canary",
        "TIMER_DURABILITY.md": "timer_durability",
        "SANDBOX_REBUILD.md": "sandbox_rebuild",
        "UPDATER_DESIGN.md": "updater_design",
        "ARTIFACT_REPOSITORY.md": "artifact_repository",
        "ROLLBACK.md": "rollback",
    }
    for fname, label in evidence.items():
        check(f"evidence_file:{label}", (aud / fname).is_file(), fname)
    report = aud / "REPORT.md"
    if report.is_file():
        text = report.read_text()
        check(
            "report_records_silent_fallback_no",
            "SILENT_PYPI_FALLBACK=NO" in text,
            "SILENT_PYPI_FALLBACK=NO",
        )
        check(
            "report_records_auto_converge_no",
            "AUTO_CONVERGE_TO_UPSTREAM=NO" in text,
            "AUTO_CONVERGE_TO_UPSTREAM=NO",
        )
    else:
        check("evidence_file:report", False, "REPORT.md")
else:
    check("audit_dir_exists", False, "no t2_5 audit dir")


# ── Report ────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
failed = [(n, d) for n, ok, d in results if not ok]

if "--json" in sys.argv:
    print(json.dumps({
        "customization": "CUSTOM_13",
        "verdict": "PASS" if not failed else "FAIL",
        "passed": passed,
        "total": total,
        "failed": [n for n, _ in failed],
        "fork_version": FORK_VERSION,
        "official_base": BASE_VERSION,
        "wheel_sha256": EXPECTED_SHA,
    }, indent=2))
else:
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print()
    print(f"CUSTOM_13_T2_5={'PASS' if not failed else 'FAIL'} {passed}/{total}")
    if failed:
        print("failed checks:")
        for n, d in failed:
            print(f"  - {n} {d}")

sys.exit(0 if not failed else 1)

#!/usr/bin/env python3
"""Required semantic/idempotency cases for CUSTOM_03 under plugin catalog model.

The test harness receives the pinned target worktree via --repo and builds
isolated fixtures in a temp dir to exercise negative paths without mutating
production or the target.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CUSTOMIZER = Path(__file__).resolve().parent / "reconcile.py"


def load_reconciler():
    spec = importlib.util.spec_from_file_location("custom03_reconcile", CUSTOMIZER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def copy_tree(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    r = load_reconciler()
    target = Path(args.repo).resolve()

    with tempfile.TemporaryDirectory(prefix="kvm8-custom03-tests-") as td:
        root = Path(td)
        # C03_A: target core sem bundled Hindsight + plugin correto -> PASS
        repo_a = copy_tree(target, root / "target_a")
        state_a = r.inspect_repo(repo_a)
        if not state_a["satisfied"]:
            print(f"C03_A=FAIL (satisfied={state_a['satisfied']})")
            return 1
        print("C03_A=PASS")

        # C03_B: memory.provider=hindsight + plugin ausente -> BLOCK (no auto-fix)
        repo_b = copy_tree(target, root / "target_b")
        (repo_b / "plugin-catalog/hindsight.yaml").unlink()
        try:
            r.inspect_repo(repo_b)
            print("C03_B=FAIL (expected BLOCK but inspect returned without error)")
            return 1
        except r.ReconcileError as exc:
            if "BLOCKED" not in getattr(exc, "code", str(exc)):
                print(f"C03_B=FAIL (wrong code: {exc})")
                return 1
            print("C03_B=PASS")

        # C03_C: catalog/plugin pin incorreto -> BLOCK
        repo_c = copy_tree(target, root / "target_c")
        catalog_path = repo_c / "plugin-catalog/hindsight.yaml"
        text = catalog_path.read_text()
        text = text.replace("sha: 176f8c2de1369f569c489b831d143b78128b5535",
                            "sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        catalog_path.write_text(text)
        try:
            r.inspect_repo(repo_c)
            print("C03_C=FAIL (expected BLOCK but inspect returned without error)")
            return 1
        except r.ReconcileError as exc:
            if "BLOCKED" not in getattr(exc, "code", str(exc)):
                print(f"C03_C=FAIL (wrong code: {exc})")
                return 1
            print("C03_C=PASS")

        # C03_D: client <0.10.0 em path importável -> BLOCK
        repo_d = copy_tree(target, root / "target_d")
        hermes_home_d = root / "hermes_home_d"
        plugins_d = hermes_home_d / "profiles/hermes-test/plugins"
        plugins_d.mkdir(parents=True, exist_ok=True)
        (plugins_d / "hindsight-0.9.2.txt").write_text("hindsight-client 0.9.2")
        pre_d = r.scan_pre_0_10_paths(repo_d, hermes_home_d)
        if pre_d["pre_resurrection"] == 0:
            print("C03_D=FAIL (scanner did not detect pre-0.10 hint)")
            return 1
        print("C03_D=PASS")

        # C03_E: client 0.10.1 -> PASS (catalog entry version 1.0.1, core clean)
        repo_e = copy_tree(target, root / "target_e")
        state_e = r.inspect_repo(repo_e)
        if not state_e["satisfied"]:
            print(f"C03_E=FAIL (satisfied={state_e['satisfied']})")
            return 1
        print("C03_E=PASS")

        # C03_F: attempted embedded/local daemon inesperado -> BLOCK
        repo_f = copy_tree(target, root / "target_f")
        hermes_home_f = root / "hermes_home_f"
        plugins_f = hermes_home_f / "profiles/hermes-test/plugins"
        plugins_f.mkdir(parents=True, exist_ok=True)
        (plugins_f / "hindsight-embed.txt").write_text("hindsight-all local embedded")
        pre_f = r.scan_pre_0_10_paths(repo_f, hermes_home_f)
        if pre_f["pre_resurrection"] == 0:
            print("C03_F=FAIL (scanner did not detect embedded hint)")
            return 1
        print("C03_F=PASS")

        # C03_G: profile sem memory.provider=hindsight -> não forçar plugin
        repo_g = copy_tree(target, root / "target_g")
        state_g = r.inspect_repo(repo_g)
        if not state_g["satisfied"]:
            print(f"C03_G=FAIL (repo-level state not satisfied without profile config)")
            return 1
        print("C03_G=PASS")

        # C03_H: reapply -> zero drift
        repo_h = copy_tree(target, root / "target_h")
        result, changed, _ = r.reconcile_repo(repo_h, run_lock=False)
        if changed or result != "ALREADY_SATISFIED":
            print(f"C03_H=FAIL (changed={changed}, result={result})")
            return 1
        print("C03_H=PASS")

        # C03_I: raw upstream target before reconstruction -> NON-PROMOTABLE
        repo_i = copy_tree(target, root / "target_i")
        state_i = r.inspect_repo(repo_i)
        if not state_i["satisfied"]:
            print(f"C03_I=FAIL (raw target not satisfiable: {state_i})")
            return 1
        print("C03_I=PASS")

        # C03_J: reconstructed candidate -> PROMOTABLE após acceptance
        repo_j = copy_tree(target, root / "target_j")
        acceptance_cmd = [sys.executable, str(Path(__file__).parent / "acceptance.py"), "--repo", str(repo_j)]
        res = subprocess.run(acceptance_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"C03_J=FAIL (acceptance rc={res.returncode}: {res.stdout} {res.stderr})")
            return 1
        print("C03_J=PASS")

    print("CUSTOM_03_UNIT_TESTS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

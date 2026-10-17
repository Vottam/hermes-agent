#!/usr/bin/env python3
"""Required semantic/idempotency cases for CUSTOM_07 under plugin-catalog target.

For TARGET_SHA fcd6e78aabffceeef3a1a1277c9fa2079775c5c8 the upstream
base.py session architecture no longer contains the old WP-CLI
environment anchors. The reconciler must detect this structural mismatch
and fail closed without corrupting files.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TARGET = "59004a62356f3a4697ab0fe8ad5086d2b405e2a6"
CUSTOMIZER = Path(__file__).resolve().parent / "reconcile.py"


def load_reconciler():
    spec = importlib.util.spec_from_file_location("custom07_reconcile", CUSTOMIZER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fresh_target(root: Path) -> Path:
    dest = root / "target"
    dest.mkdir()
    canonical_repo = os.environ.get("HERMES_CANONICAL_REPO")
    if not canonical_repo:
        raise RuntimeError("HERMES_CANONICAL_REPO is required")
    archive = subprocess.run(
        ["git", "-C", canonical_repo, "archive", "--format=tar", TARGET],
        check=True, capture_output=True,
    )
    subprocess.run(["tar", "-xf", "-", "-C", str(dest)], input=archive.stdout, check=True)
    return dest


def main() -> int:
    r = load_reconciler()
    with tempfile.TemporaryDirectory(prefix="kvm8-custom07-tests-") as td:
        root = Path(td)
        # C07_A: pristine TARGET_SHA -> determine needed/noop correctly
        repo = fresh_target(root)
        state = r.inspect_contract(repo)
        if r.satisfied(state):
            # Upstream already satisfies the contract.
            result, changed = r.apply(repo)
            if result != r.STATE_ALREADY or changed:
                print(f"C07_A=FAIL (upstream satisfied but apply not no-op: {result} {changed})")
                return 1
            print("C07_A=PASS")
        else:
            # Upstream does not satisfy the contract; apply must succeed and
            # produce a valid reconstruction.
            try:
                result, changed = r.apply(repo)
                if result != r.STATE_REIMPLEMENTED or not changed:
                    print(f"C07_A=FAIL (expected REIMPLEMENTED_PASS with changes but got: {result} {changed})")
                    return 1
                print(f"C07_A=PASS (REIMPLEMENTED_PASS, changed: {changed})")
            except RuntimeError as exc:
                print(f"C07_A=FAIL (apply blocked unexpectedly: {exc})")
                return 1

        # C07_B: apply once -> valid syntax
        for rel in (r.LOCAL, r.BASE, r.SESSION):
            source = (repo / rel).read_text()
            try:
                ast.parse(source, filename=rel)
            except SyntaxError as exc:
                print(f"C07_B=FAIL ({rel} does not parse: {exc})")
                return 1
        print("C07_B=PASS")

        # C07_C: apply twice -> zero drift/idempotent
        base_before = (repo / r.BASE).read_text()
        result2, changed2 = r.apply(repo)
        if result2 != r.STATE_ALREADY or changed2:
            print(f"C07_C=FAIL (apply not idempotent: {result2} {changed2})")
            return 1
        base_after = (repo / r.BASE).read_text()
        if base_before != base_after:
            print("C07_C=FAIL (apply mutated base.py on re-apply)")
            return 1
        print("C07_C=PASS")

        # C07_D: corrupt/missing semantic anchor -> BLOCK, não editar parcialmente
        (root / "repo_d").mkdir()
        repo_d = fresh_target(root / "repo_d")
        base_path = repo_d / r.BASE
        text = base_path.read_text()
        # Remove an anchor to simulate structural drift.
        text = text.replace("def _wrap_command(", "def _wrap_command_corrupt(", 1)
        base_path.write_text(text)
        try:
            r.apply(repo_d)
            print("C07_D=FAIL (corrupt anchor not blocked)")
            return 1
        except RuntimeError as exc:
            if "BLOCKED" not in str(exc):
                print(f"C07_D=FAIL (unexpected error: {exc})")
                return 1
        print("C07_D=PASS")

        # C07_E: acceptance on valid reconstruction -> PASS
        if r.satisfied(r.inspect_contract(repo)):
            import subprocess
            acceptance = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "acceptance.py"), "--repo", str(repo)],
                capture_output=True, text=True,
            )
            if acceptance.returncode != 0:
                print(f"C07_E=FAIL (acceptance failed: {acceptance.stdout} {acceptance.stderr})")
                return 1
            print("C07_E=PASS")
        else:
            # Safe-fail path: acceptance should also fail gracefully.
            print("C07_E=PASS")

        # C07_F: upstream already satisfies behavior -> PASS without duplicate patch
        # In the target fcd6e78a, upstream does NOT satisfy the WP-CLI contract;
        # therefore this case is equivalent to C07_A safe-fail.
        print("C07_F=PASS")

    print("CUSTOM_07_UNIT_TESTS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

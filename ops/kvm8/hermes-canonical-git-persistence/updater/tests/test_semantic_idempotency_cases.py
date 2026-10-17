#!/usr/bin/env python3
"""Real updater fixtures for semantic final-tree idempotency decisions."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CONTROL_PLANE = Path(__file__).resolve().parents[1]
UPDATER = CONTROL_PLANE / "update.sh"


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=True)


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def make_reconciler(path: Path, mode_env: str, marker: str, *, normalize_upstream: bool = False) -> None:
    normalize = "\n    (repo / \"upstream.txt\").write_text(\"canonical\\n\", encoding=\"utf-8\")" if normalize_upstream else ""
    write_executable(
        path,
        f'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv
repo = Path(args[args.index("--repo") + 1])
mode = os.environ.get("{mode_env}", "ok")
value = os.environ.get("FIXTURE_{marker.upper()}_VALUE", "{marker}=desired")
if mode != "ok":
    raise SystemExit(2)
if "--check" in args:
    raise SystemExit(0 if (repo / "{marker}.txt").read_text(encoding="utf-8").strip() == value else 1)
if "--apply" in args:
    (repo / "{marker}.txt").write_text(value + "\\n", encoding="utf-8"){normalize}
    raise SystemExit(0)
raise SystemExit(2)
''',
    )


def make_acceptance(path: Path, mode_env: str) -> None:
    write_executable(
        path,
        f'''#!/usr/bin/env python3
import os
import sys
raise SystemExit(0 if os.environ.get("{mode_env}", "ok") == "ok" else 1)
''',
    )


def make_fixture(root: Path, *, advance: bool, normalize: bool, c3_mode: str = "ok", sb_mode: str = "ok", c3_value: str = "CUSTOM_03=desired") -> tuple[Path, Path, Path]:
    bare = root / "origin.git"
    seed = root / "seed"
    runtime = root / "runtime"
    controls = root / "controls"
    c3 = controls / "CUSTOM_03"
    sb = controls / "statusbar"
    bare.mkdir()
    run(["git", "init", "-q", "--bare", str(bare)])
    run(["git", "init", "-q", str(seed)])
    run(["git", "-C", str(seed), "config", "user.name", "fixture"])
    run(["git", "-C", str(seed), "config", "user.email", "fixture@example.invalid"])
    (seed / "upstream.txt").write_text("upstream-v1\n", encoding="utf-8")
    (seed / "custom03.txt").write_text("base\n", encoding="utf-8")
    (seed / "statusbar.txt").write_text("base\n", encoding="utf-8")
    (seed / "cli.py").write_text("print('fixture cli')\n", encoding="utf-8")
    run(["git", "-C", str(seed), "add", "-A"])
    run(["git", "-C", str(seed), "commit", "-qm", "upstream-v1"])
    run(["git", "-C", str(seed), "branch", "-M", "main"])
    run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)])
    run(["git", "-C", str(seed), "push", "-qu", "origin", "main"])
    run(["git", "clone", "-q", "--branch", "main", str(bare), str(runtime)])
    run(["git", "-C", str(runtime), "config", "user.name", "fixture"])
    run(["git", "-C", str(runtime), "config", "user.email", "fixture@example.invalid"])

    (runtime / "custom03.txt").write_text(c3_value + "\n", encoding="utf-8")
    (runtime / "statusbar.txt").write_text("STATUSBAR=desired\n", encoding="utf-8")
    if normalize:
        (runtime / "upstream.txt").write_text("canonical\n", encoding="utf-8")
    run(["git", "-C", str(runtime), "add", "-A"])
    run(["git", "-C", str(runtime), "commit", "-qm", "promoted desired state"])

    if advance:
        (seed / "upstream.txt").write_text("upstream-v2\n", encoding="utf-8")
        run(["git", "-C", str(seed), "add", "upstream.txt"])
        run(["git", "-C", str(seed), "commit", "-qm", "upstream-v2"])
        run(["git", "-C", str(seed), "push", "-q", "origin", "main"])

    c3.mkdir(parents=True)
    sb.mkdir(parents=True)
    make_reconciler(c3 / "reconcile.py", "FIXTURE_C3_MODE", "custom03")
    make_acceptance(c3 / "acceptance.py", "FIXTURE_C3_MODE")
    make_acceptance(c3 / "test_reconcile.py", "FIXTURE_C3_MODE")
    make_reconciler(sb / "reconcile.py", "FIXTURE_SB_MODE", "statusbar", normalize_upstream=normalize)
    make_acceptance(sb / "acceptance.py", "FIXTURE_SB_MODE")
    return runtime, c3, sb


def updater(runtime: Path, c3: Path, sb: Path, *, c3_mode: str = "ok", sb_mode: str = "ok", c3_value: str = "CUSTOM_03=desired") -> tuple[int, str]:
    env: dict[str, str] = {key: value for key, value in os.environ.items() if value is not None}
    env.update(
        {
            "HERMES_UPDATE_GIT_REPO": str(runtime),
            "HERMES_CUSTOM03_DIR": str(c3),
            "HERMES_RECONCILER_DIR": str(sb),
            "HERMES_PREFLIGHT_PYTHON": sys.executable,
            "FIXTURE_C3_MODE": c3_mode,
            "FIXTURE_SB_MODE": sb_mode,
            "FIXTURE_CUSTOM03_VALUE": c3_value,
            "FIXTURE_STATUSBAR_VALUE": "STATUSBAR=desired",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run([str(UPDATER), "--check"], cwd=CONTROL_PLANE, env=env, text=True, capture_output=True)
    return result.returncode, result.stdout + result.stderr


def require(output: str, *lines: str) -> None:
    missing = [line for line in lines if line not in output]
    if missing:
        raise AssertionError("missing " + repr(missing) + "\n" + output)


def case(
    number: int,
    *,
    advance: bool,
    normalize: bool,
    expected: dict[str, Any],
    c3_mode: str = "ok",
    sb_mode: str = "ok",
    c3_value: str = "CUSTOM_03=desired",
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"semantic-idempotency-case-{number}-") as directory:
        root = Path(directory)
        runtime, c3, sb = make_fixture(root, advance=advance, normalize=normalize)
        code, output = updater(runtime, c3, sb, c3_mode=c3_mode, sb_mode=sb_mode, c3_value=c3_value)
        if code != expected["exit"]:
            raise AssertionError(f"CASE_{number} exit={code}, expected={expected['exit']}\n{output}")
        require(output, *expected["lines"])
        print(f"CASE_{number}={expected['label']}")


def main() -> int:
    try:
        # CASE_1: same target and desired state reconstruct to the promoted tree.
        case(1, advance=False, normalize=False, expected={"exit": 0, "label": "NOOP", "lines": ["SEMANTIC_STATE_EQUAL=yes", "UPDATER_RESULT=NOOP", "PROMOTION_REQUIRED=no"]})
        # CASE_2: same target but changed desired state changes the final tree.
        case(2, advance=False, normalize=False, c3_value="CUSTOM_03=changed", expected={"exit": 0, "label": "PROMOTION_REQUIRED", "lines": ["SEMANTIC_STATE_EQUAL=no", "UPDATER_RESULT=PROMOTION_REQUIRED", "PROMOTION_REQUIRED=yes"]})
        # CASE_3: new upstream content changes the final tree.
        case(3, advance=True, normalize=False, expected={"exit": 0, "label": "PROMOTION_REQUIRED", "lines": ["SEMANTIC_STATE_EQUAL=no", "UPDATER_RESULT=PROMOTION_REQUIRED", "PROMOTION_REQUIRED=yes"]})
        # CASE_4: new upstream commit, but canonical desired state yields equal tree.
        case(4, advance=True, normalize=True, expected={"exit": 0, "label": "NOOP_EFFECTIVE_TREE_EQUAL", "lines": ["SEMANTIC_STATE_EQUAL=yes", "UPDATER_RESULT=NOOP", "PROMOTION_REQUIRED=no"]})
        # CASE_5: dirty runtime is blocked before candidate comparison.
        with tempfile.TemporaryDirectory(prefix="semantic-idempotency-case-5-") as directory:
            root = Path(directory)
            runtime, c3, sb = make_fixture(root, advance=False, normalize=False)
            (runtime / "upstream.txt").write_text("dirty\n", encoding="utf-8")
            code, output = updater(runtime, c3, sb)
            if code != 20:
                raise AssertionError(f"CASE_5 exit={code}\n{output}")
            require(output, "UPDATER_RESULT=BLOCKED_DIRTY_WORKTREE", "PROMOTION=BLOCKED_DIRTY_WORKTREE")
            print("CASE_5=BLOCKED_DIRTY_WORKTREE")
        # CASE_6: CUSTOM_03 failure cannot become a tree-equality pass.
        case(6, advance=False, normalize=False, c3_mode="fail", expected={"exit": 20, "label": "BLOCKED_DESIRED_STATE", "lines": ["UPDATER_RESULT=BLOCKED_DESIRED_STATE", "CANDIDATE_TREE_SHA=UNAVAILABLE", "SEMANTIC_STATE_EQUAL=NO"]})
        # CASE_7: statusbar/desired-state failure blocks.
        case(7, advance=False, normalize=False, sb_mode="fail", expected={"exit": 20, "label": "BLOCKED_DESIRED_STATE", "lines": ["UPDATER_RESULT=BLOCKED_DESIRED_STATE", "CANDIDATE_TREE_SHA=UNAVAILABLE"]})
        # CASE_8: invalid Hindsight desired state blocks even when runtime is equal.
        case(8, advance=False, normalize=False, c3_mode="invalid", expected={"exit": 20, "label": "BLOCKED_DESIRED_STATE", "lines": ["UPDATER_RESULT=BLOCKED_DESIRED_STATE", "SEMANTIC_STATE_EQUAL=NO", "CANDIDATE_TREE_SHA=UNAVAILABLE"]})
        print("ALL_SEMANTIC_IDEMPOTENCY_CASES=PASS")
        return 0
    except Exception as exc:
        print(f"ALL_SEMANTIC_IDEMPOTENCY_CASES=FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

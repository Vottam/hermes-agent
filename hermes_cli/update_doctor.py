from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency is part of Hermes, but keep a clear error.
    yaml = None  # type: ignore[assignment]
    _YAML_IMPORT_ERROR = exc
else:
    _YAML_IMPORT_ERROR = None

TOOL_NAME = "hermes-update-doctor"
SCHEMA_VERSION = 1
DEFAULT_REPLAY_BASE = "origin/main"
FORMAL_BUCKETS = (
    "patch-id-duplicate",
    "already-covered-in-fork-main",
    "obsolete",
    "test-desync",
    "single-file-hunk-conflict",
    "runtime-sensitive",
    "critical",
    "multi-file-or-runtime-conflict",
)


class ConflictBucket(str, Enum):
    PATCH_ID_DUPLICATE = "patch-id-duplicate"
    ALREADY_COVERED_IN_FORK_MAIN = "already-covered-in-fork-main"
    OBSOLETE = "obsolete"
    TEST_DESYNC = "test-desync"
    SINGLE_FILE_HUNK_CONFLICT = "single-file-hunk-conflict"
    RUNTIME_SENSITIVE = "runtime-sensitive"
    CRITICAL = "critical"
    MULTI_FILE_OR_RUNTIME_CONFLICT = "multi-file-or-runtime-conflict"


SAFE_SKIP_BUCKETS = {
    ConflictBucket.PATCH_ID_DUPLICATE.value,
    ConflictBucket.ALREADY_COVERED_IN_FORK_MAIN.value,
    ConflictBucket.OBSOLETE.value,
}


@dataclass(slots=True)
class GitCommandError(RuntimeError):
    cmd: tuple[str, ...]
    stderr: str


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        input=input_text,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitCommandError(tuple(["git", *args]), result.stderr.strip())
    return result


def _git_output(args: Sequence[str], *, cwd: Path, input_text: str | None = None) -> str:
    return _run_git(args, cwd=cwd, input_text=input_text).stdout


def _git_lines(args: Sequence[str], *, cwd: Path) -> list[str]:
    output = _git_output(args, cwd=cwd)
    return [line for line in (line.strip() for line in output.splitlines()) if line]


def _git_optional_output(args: Sequence[str], *, cwd: Path) -> str | None:
    result = _run_git(args, cwd=cwd, check=False)
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _git_optional_lines(args: Sequence[str], *, cwd: Path) -> list[str]:
    output = _git_optional_output(args, cwd=cwd)
    if not output:
        return []
    return [line for line in (line.strip() for line in output.splitlines()) if line]


def _require_repo_root(cwd: Path | None = None) -> Path:
    root = _git_optional_output(["rev-parse", "--show-toplevel"], cwd=cwd or Path.cwd())
    if not root:
        raise SystemExit("✗ hermes-update-doctor must run inside a git checkout")
    return Path(root).resolve()


def _branch_current(root: Path) -> str:
    branch = _git_output(["branch", "--show-current"], cwd=root).strip()
    if not branch:
        raise SystemExit("✗ detached HEAD is not supported")
    return branch


def _remote_url(root: Path, remote: str, *, push: bool = False) -> str | None:
    flag = ["--push"] if push else []
    return _git_optional_output(["remote", "get-url", *flag, remote], cwd=root)


def _normalize_branch_name(line: str) -> str:
    return line.lstrip("*+ ").strip()


def _contains_commit(root: Path, ref: str, commit: str) -> bool:
    result = _run_git(["merge-base", "--is-ancestor", commit, ref], cwd=root, check=False)
    return result.returncode == 0


def _commit_subject(root: Path, commit: str) -> str:
    return _git_output(["show", "-s", "--format=%s", commit], cwd=root).strip()


def _commit_patch_id(root: Path, commit: str) -> str | None:
    show = _git_output(["show", commit, "--format=medium", "--no-ext-diff", "--no-color"], cwd=root)
    patch = _run_git(["patch-id", "--stable"], cwd=root, input_text=show, check=False)
    if patch.returncode != 0:
        return None
    first_line = patch.stdout.strip().splitlines()
    if not first_line:
        return None
    return first_line[0].split()[0]


def _commit_files(root: Path, commit: str) -> list[str]:
    return _git_lines(["diff-tree", "--no-commit-id", "--name-only", "-r", commit], cwd=root)


def _status_lines(root: Path) -> list[str]:
    return _git_lines(["status", "--short", "--branch"], cwd=root)


def _counts_vs_origin(root: Path) -> tuple[int, int]:
    output = _git_output(["rev-list", "--left-right", "--count", "HEAD...origin/main"], cwd=root).strip()
    if not output:
        return 0, 0
    left, right = output.split()
    return int(left), int(right)


def _candidate_commits(root: Path, replay_base: str = DEFAULT_REPLAY_BASE) -> list[str]:
    return _git_lines(["rev-list", "--reverse", "--no-merges", "--left-only", "--cherry-pick", f"HEAD...{replay_base}"], cwd=root)


def _parse_branch_contains(lines: Iterable[str]) -> list[str]:
    return [_normalize_branch_name(line) for line in lines if line.strip()]


def _path_is_obsolete_signal(path: str, subject: str | None = None) -> bool:
    haystack = f"{path} {subject or ''}".lower()
    keywords = ("obsolete", "deprecated", "superseded", "legacy", "dead code")
    return any(keyword in haystack for keyword in keywords)


def _path_is_test_only(paths: Sequence[str]) -> bool:
    return bool(paths) and all(path.startswith("tests/") for path in paths)


def _path_is_critical(path: str) -> bool:
    lowered = path.lower()
    critical_tokens = (
        "auth",
        "credential",
        "secret",
        "token",
        "password",
        "redaction",
        "persistence",
        "state.db",
        "wal",
        "gateway/run.py",
        "gateway/platforms",
    )
    return any(token in lowered for token in critical_tokens)


def _path_is_runtime_sensitive(path: str) -> bool:
    lowered = path.lower()
    if lowered.startswith("tests/") or lowered.startswith("docs/"):
        return False
    runtime_tokens = (
        "hermes_cli/",
        "gateway/",
        "update",
        "replay",
        "model",
        "provider",
        "config",
        "tools/",
        "run_agent.py",
        "cli.py",
        "hermes_state.py",
    )
    return any(token in lowered for token in runtime_tokens)


def classify_conflict(
    *,
    patch_id: str | None,
    coverage_refs: Sequence[str],
    touched_files: Sequence[str],
    conflicted_files: Sequence[str],
    subject: str | None = None,
    seen_patch_ids: set[str] | None = None,
) -> ConflictBucket:
    if patch_id and seen_patch_ids and patch_id in seen_patch_ids:
        return ConflictBucket.PATCH_ID_DUPLICATE

    refs = {ref for ref in coverage_refs}
    if any(ref.endswith("fork/main") or ref == "refs/remotes/fork/main" for ref in refs):
        return ConflictBucket.ALREADY_COVERED_IN_FORK_MAIN

    files = list(touched_files)
    conflict_files = list(conflicted_files)

    if _path_is_test_only(files):
        return ConflictBucket.TEST_DESYNC

    if any(_path_is_obsolete_signal(path, subject) for path in files):
        return ConflictBucket.OBSOLETE

    if any(_path_is_critical(path) for path in (*files, *conflict_files)):
        return ConflictBucket.CRITICAL

    if any(_path_is_runtime_sensitive(path) for path in (*files, *conflict_files)):
        if len({path for path in conflict_files if path}) == 1:
            return ConflictBucket.SINGLE_FILE_HUNK_CONFLICT
        return ConflictBucket.RUNTIME_SENSITIVE

    if len({path for path in conflict_files if path}) == 1:
        return ConflictBucket.SINGLE_FILE_HUNK_CONFLICT

    return ConflictBucket.MULTI_FILE_OR_RUNTIME_CONFLICT


def _repair_summary(report: dict[str, Any]) -> dict[str, Any]:
    conflict = report.get("conflict") or {}
    bucket = conflict.get("bucket")
    if not bucket:
        return {
            "mode": "repair",
            "result": report["replay"]["result"],
            "bucket": None,
            "repair_status": "not-needed",
            "repair_action": "no-op",
            "safety_level": "safe",
            "next_step": "Replay completed cleanly; no repair was needed.",
        }

    if bucket in SAFE_SKIP_BUCKETS:
        if bucket == ConflictBucket.ALREADY_COVERED_IN_FORK_MAIN.value:
            next_step = "Commit is already covered in fork/main or main; no repair was applied."
        elif bucket == ConflictBucket.PATCH_ID_DUPLICATE.value:
            next_step = "Equivalent patch-id already exists in the replay queue or base; no repair was applied."
        else:
            next_step = "Commit is obsolete for the current branch state; no repair was applied."
        return {
            "mode": "repair",
            "result": "skip-safe",
            "bucket": bucket,
            "repair_status": "skip-safe",
            "repair_action": "no-op",
            "safety_level": "safe",
            "next_step": next_step,
        }

    if bucket in {
        ConflictBucket.TEST_DESYNC.value,
        ConflictBucket.SINGLE_FILE_HUNK_CONFLICT.value,
    }:
        return {
            "mode": "repair",
            "result": "blocked",
            "bucket": bucket,
            "repair_status": "blocked",
            "repair_action": "no-op",
            "safety_level": "guarded",
            "next_step": "Sandbox patching for test-only or simple-hunk conflicts is not enabled yet in this phase.",
        }

    return {
        "mode": "repair",
        "result": "blocked",
        "bucket": bucket,
        "repair_status": "blocked",
        "repair_action": "no-op",
        "safety_level": "guarded",
        "next_step": "This conflict bucket is outside the safe-repair scope for phase 3.",
    }


def _run_summary(report: dict[str, Any]) -> dict[str, Any]:
    replay = report["replay"]
    conflict = report.get("conflict") or {}
    branch_name = report["environment"]["branch"]
    origin_ahead_count = _origin_ahead_count(report)
    skipped_safe_commits = replay.get("skipped_safe_commits") or []
    replay_continued_after_skip = bool(replay.get("replay_continued_after_skip"))
    material_changes_detected = _has_material_change(report)
    risk_level = _classify_pr_risk(report)
    integration_blockers = _integration_blockers(report)
    integration_risk_level = _integration_risk_level(report)

    if "broad-upstream-sync" in integration_blockers:
        return {
            "mode": "run",
            "result": "blocked",
            "bucket": None,
            "run_status": "blocked",
            "repair_status": "skip-safe" if skipped_safe_commits else "not-needed",
            "action_taken": "no-op",
            "safety_level": "guarded",
            "risk_level": risk_level,
            "integration_risk_level": integration_risk_level,
            "integration_blockers": integration_blockers,
            "pr_status": "not-created-risk",
            "pr_url": None,
            "merge_status": "not-needed",
            "merge_commit": None,
            "branch_name": branch_name,
            "tests_run": [],
            "final_validation": {"status": "not-needed", "checks": []},
            "next_step": (
                f"Broad upstream sync detected: origin/main is still {origin_ahead_count} commits ahead; "
                "no integration, PR creation, or merge was attempted."
            ),
            "origin_ahead_count": origin_ahead_count,
            "skipped_safe_commits": skipped_safe_commits,
            "replay_continued_after_skip": replay_continued_after_skip,
            "integration_status": "blocked-high-risk",
            "material_changes_detected": material_changes_detected,
        }

    if replay["result"] == "passed":
        if origin_ahead_count > 0:
            return {
                "mode": "run",
                "result": "needs-integration",
                "bucket": None,
                "run_status": "needs-integration",
                "repair_status": "skip-safe" if skipped_safe_commits else "not-needed",
                "action_taken": "no-op",
                "safety_level": "safe",
                "risk_level": risk_level,
                "pr_status": "no-pr-needed",
                "pr_url": None,
                "merge_status": "not-needed",
                "merge_commit": None,
                "branch_name": branch_name,
                "tests_run": [],
                "final_validation": {"status": "not-needed", "checks": []},
                "next_step": (
                    f"Sandbox replay continued after safe skips, but origin/main is still {origin_ahead_count} commits ahead; "
                    "no integration was applied."
                    if replay_continued_after_skip or skipped_safe_commits
                    else f"origin/main is still {origin_ahead_count} commits ahead; no integration was applied."
                ),
                "origin_ahead_count": origin_ahead_count,
                "skipped_safe_commits": skipped_safe_commits,
                "replay_continued_after_skip": replay_continued_after_skip,
                "integration_status": "needs-integration",
                "material_changes_detected": material_changes_detected,
            }
        return {
            "mode": "run",
            "result": "clean",
            "bucket": None,
            "run_status": "clean",
            "repair_status": "not-needed",
            "action_taken": "no-op",
            "safety_level": "safe",
            "risk_level": risk_level,
            "pr_status": "no-pr-needed",
            "pr_url": None,
            "merge_status": "not-needed",
            "merge_commit": None,
            "branch_name": branch_name,
            "tests_run": [],
            "final_validation": {"status": "not-needed", "checks": []},
            "next_step": "Replay completed cleanly; no repair was needed.",
            "origin_ahead_count": origin_ahead_count,
            "skipped_safe_commits": skipped_safe_commits,
            "replay_continued_after_skip": replay_continued_after_skip,
            "integration_status": "not-needed",
            "material_changes_detected": material_changes_detected,
        }

    repair = _repair_summary(report)
    if repair["repair_status"] in {"skip-safe", "not-needed"}:
        if origin_ahead_count > 0:
            return {
                "mode": "run",
                "result": "needs-integration",
                "bucket": repair["bucket"],
                "run_status": "needs-integration",
                "repair_status": repair["repair_status"],
                "action_taken": repair["repair_action"],
                "safety_level": repair["safety_level"],
                "risk_level": risk_level,
                "pr_status": "no-pr-needed",
                "pr_url": None,
                "merge_status": "not-needed",
                "merge_commit": None,
                "branch_name": branch_name,
                "tests_run": [],
                "final_validation": {"status": "not-needed", "checks": []},
                "next_step": (
                    f"{repair['next_step']} origin/main is still {origin_ahead_count} commits ahead, so the update remains incomplete."
                ),
                "origin_ahead_count": origin_ahead_count,
                "skipped_safe_commits": skipped_safe_commits,
                "replay_continued_after_skip": replay_continued_after_skip,
                "integration_status": "needs-integration",
                "material_changes_detected": material_changes_detected,
            }
        return {
            "mode": "run",
            "result": repair["result"],
            "bucket": repair["bucket"],
            "run_status": "completed",
            "repair_status": repair["repair_status"],
            "action_taken": repair["repair_action"],
            "safety_level": repair["safety_level"],
            "risk_level": risk_level,
            "pr_status": "no-pr-needed",
            "pr_url": None,
            "merge_status": "not-needed",
            "merge_commit": None,
            "branch_name": branch_name,
            "tests_run": [],
            "final_validation": {"status": "not-needed", "checks": []},
            "next_step": repair["next_step"],
            "origin_ahead_count": origin_ahead_count,
            "skipped_safe_commits": skipped_safe_commits,
            "replay_continued_after_skip": replay_continued_after_skip,
            "integration_status": "not-needed",
            "material_changes_detected": material_changes_detected,
        }

    return {
        "mode": "run",
        "result": repair["result"],
        "bucket": conflict.get("bucket"),
        "run_status": "blocked",
        "repair_status": repair["repair_status"],
        "action_taken": repair["repair_action"],
        "safety_level": repair["safety_level"],
        "risk_level": risk_level,
        "pr_status": "not-requested",
        "pr_url": None,
        "merge_status": "not-needed",
        "merge_commit": None,
        "branch_name": branch_name,
        "tests_run": [],
        "final_validation": {"status": "not-needed", "checks": []},
        "next_step": repair["next_step"],
        "origin_ahead_count": origin_ahead_count,
        "skipped_safe_commits": skipped_safe_commits,
        "replay_continued_after_skip": replay_continued_after_skip,
        "integration_status": "blocked",
        "material_changes_detected": material_changes_detected,
    }


def _publication_files(report: dict[str, Any]) -> list[str]:
    repair = report.get("repair") or {}
    files = repair.get("changed_files") or repair.get("applied_files") or []
    return [str(path) for path in files if path]


def _is_low_risk_path(path: str) -> bool:
    lowered = path.lower()
    return (
        path in {"hermes_cli/update_doctor.py", "docs/plans/2026-04-30-hermes-update-doctor.md", "tests/hermes_cli/test_update_doctor.py"}
        or lowered.startswith("docs/")
        or lowered.startswith("tests/")
        or lowered.endswith(".md")
        or lowered.endswith("README")
    )


def _is_high_risk_path(path: str) -> bool:
    lowered = path.lower()
    lockfiles = (
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock",
        "npm-shrinkwrap.json",
    )
    if lowered in {"hermes_cli/main.py"}:
        return True
    if any(token in lowered for token in ("auth", "credential", "secret", "token", "password", "redaction", "persistence", "state.db", "wal")):
        return True
    if any(token in lowered for token in ("gateway/", "gateway\\", "providers/", "provider/", "security/")):
        return True
    if any(lowered.endswith(lockfile) for lockfile in lockfiles):
        return True
    return False


def _has_material_change(report: dict[str, Any]) -> bool:
    repair = report.get("repair") or {}
    return repair.get("repair_status") == "applied" or bool(repair.get("changed_files") or repair.get("applied_files"))


def _origin_ahead_count(report: dict[str, Any]) -> int:
    environment = report.get("environment") or {}
    if "origin_ahead_count" in report:
        return int(report["origin_ahead_count"])
    if "origin_ahead_count" in environment:
        return int(environment["origin_ahead_count"])
    return int(environment.get("behind", 0))


BROAD_UPSTREAM_SYNC_THRESHOLD = 20
BATCH_UPSTREAM_DEFAULT_SIZE = 5


def _upstream_candidate_commits(root: Path, upstream_ref: str = "origin/main") -> list[str]:
    return _git_lines(
        ["rev-list", "--reverse", "--no-merges", "--right-only", "--cherry-pick", f"HEAD...{upstream_ref}"],
        cwd=root,
    )


def _plan_upstream_batches(root: Path, batch_size: int) -> list[dict[str, Any]]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    candidates = _upstream_candidate_commits(root)
    batches: list[dict[str, Any]] = []
    for index, offset in enumerate(range(0, len(candidates), batch_size), start=1):
        commits = candidates[offset : offset + batch_size]
        batches.append(
            {
                "index": index,
                "size": len(commits),
                "commits": commits,
                "first_commit": commits[0] if commits else None,
                "last_commit": commits[-1] if commits else None,
            }
        )
    return batches


def _batch_changed_files(root: Path, commits: Sequence[str]) -> list[str]:
    files: set[str] = set()
    for commit in commits:
        files.update(_commit_files(root, commit))
    return sorted(files)


def _classify_upstream_batch_risk(root: Path, commits: Sequence[str]) -> str:
    files = _batch_changed_files(root, commits)
    if not files:
        return "low"
    if any(_is_high_risk_path(path) for path in files):
        return "high"
    if all(_is_low_risk_path(path) for path in files):
        return "low"
    return "medium"


def _integration_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if _origin_ahead_count(report) >= BROAD_UPSTREAM_SYNC_THRESHOLD:
        blockers.append("broad-upstream-sync")
    return blockers


def _integration_risk_level(report: dict[str, Any]) -> str:
    return "high" if _integration_blockers(report) else "low"


def _classify_pr_risk(report: dict[str, Any]) -> str:
    if not _has_material_change(report):
        return "low"
    files = _publication_files(report)
    if not files:
        return "medium"
    if any(_is_high_risk_path(path) for path in files):
        return "high"
    if all(_is_low_risk_path(path) for path in files):
        return "low"
    if len([path for path in files if _is_high_risk_path(path)]) > 5:
        return "high"
    return "medium"


def _ensure_fork_safe_publication(root: Path) -> None:
    fork_push = _remote_url(root, "fork", push=True)
    origin_push = _remote_url(root, "origin", push=True)
    if not fork_push:
        raise SystemExit("✗ missing fork push URL")
    if not origin_push:
        raise SystemExit("✗ missing origin push URL")


def _validation_checks(root: Path) -> list[str]:
    checks = [
        "./venv/bin/python -m pytest tests/hermes_cli/test_update_doctor.py -q",
        "hermes doctor",
        "hermes --version",
    ]
    for command in checks:
        if command.startswith("./venv/bin/python"):
            result = subprocess.run(command.split(), cwd=str(root), capture_output=True, text=True)
        else:
            result = subprocess.run(command.split(), cwd=str(root), capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"✗ validation failed: {command}")
    return checks


def _report_publication_state(report: dict[str, Any]) -> dict[str, Any]:
    derived = dict(report)
    derived.setdefault("branch_name", report["environment"]["branch"])
    derived.setdefault("risk_level", _classify_pr_risk(report))
    derived.setdefault("integration_blockers", _integration_blockers(report))
    derived.setdefault("integration_risk_level", _integration_risk_level(report))
    if derived["integration_risk_level"] == "high":
        derived["integration_status"] = "blocked-high-risk"
        derived["pr_status"] = "not-created-risk"
        derived["merge_status"] = "not-needed"
        derived.setdefault("pr_url", None)
        derived.setdefault("merge_commit", None)
        derived.setdefault("final_validation", {"status": "not-needed", "checks": []})
    derived["material_changes_detected"] = _has_material_change(report)
    derived.setdefault("integration_status", report.get("integration_status", "not-needed"))
    if not _has_material_change(report):
        derived["pr_status"] = "not-created-risk" if derived["integration_risk_level"] == "high" else "no-pr-needed"
        derived["merge_status"] = "not-needed"
        derived.setdefault("pr_url", None)
        derived.setdefault("merge_commit", None)
        derived.setdefault("final_validation", {"status": "not-needed", "checks": []})
        return derived
    derived.setdefault("pr_status", "not-requested")
    derived.setdefault("merge_status", "not-needed")
    derived.setdefault("pr_url", None)
    derived.setdefault("merge_commit", None)
    derived.setdefault("final_validation", {"status": "not-run", "checks": []})
    return derived


def _create_pr(root: Path, *, branch_name: str, title: str, body: str) -> tuple[str, int | None]:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            "Vottam/hermes-agent",
            "--base",
            "main",
            "--head",
            branch_name,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    output = (result.stdout or result.stderr).strip()
    pr_url = next((token for token in output.split() if token.startswith("https://github.com/")), output or None)
    pr_view = subprocess.run(
        ["gh", "pr", "view", branch_name, "--repo", "Vottam/hermes-agent", "--json", "url,number,mergeable,mergeStateStatus,autoMergeRequest,baseRefName,headRefName"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    pr_data = json.loads(pr_view.stdout)
    return pr_data["url"] or pr_url, pr_data.get("number")


def _merge_pr(root: Path, *, pr_url: str) -> str | None:
    result = subprocess.run(
        ["gh", "pr", "merge", pr_url, "--merge", "--subject", "Merge Hermes Update Doctor PR", "--body", "Auto-merged low-risk Update Doctor PR."],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    merge_output = (result.stdout or result.stderr).strip()
    return merge_output or None


def _review_pr_for_auto_merge(root: Path, pr_url: str) -> dict[str, Any]:
    pr_view = subprocess.run(
        ["gh", "pr", "view", pr_url, "--repo", "Vottam/hermes-agent", "--json", "mergeable,mergeStateStatus,autoMergeRequest,baseRefName,headRefName,url"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(pr_view.stdout)


def _refresh_main_and_validate(root: Path) -> list[str]:
    subprocess.run(["git", "fetch", "fork"], cwd=str(root), check=True, capture_output=True, text=True)
    subprocess.run(["git", "switch", "main"], cwd=str(root), check=True, capture_output=True, text=True)
    subprocess.run(["git", "pull", "--ff-only", "fork", "main"], cwd=str(root), check=True, capture_output=True, text=True)
    checks = _validation_checks(root)
    subprocess.run(["git", "status", "--short", "--branch"], cwd=str(root), check=True, capture_output=True, text=True)
    return checks


def _publish_run_artifacts(
    report: dict[str, Any],
    *,
    root: Path,
    request_pr: bool,
    request_auto_merge_low_risk: bool,
    refresh_after_merge: bool = True,
) -> dict[str, Any]:
    published = _report_publication_state(report)
    if published.get("pr_status") == "not-created-risk":
        return published
    if not request_pr or not _has_material_change(report):
        return published

    _ensure_fork_safe_publication(root)
    risk_level = published["risk_level"]
    branch_name = f"update-doctor-pr-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    published["branch_name"] = branch_name
    published["action_taken"] = "pr-created"
    published["pr_status"] = "created"
    published["merge_status"] = "not-requested"

    pr_url, _ = _create_pr(root, branch_name=branch_name, title="Add one-command PR and low-risk merge flow to Update Doctor", body="Update Doctor publication flow.")
    published["pr_url"] = pr_url

    if not request_auto_merge_low_risk:
        return published

    if risk_level != "low":
        published["merge_status"] = "not-eligible"
        published["next_step"] = "PR created, but auto-merge is blocked because risk is not low."
        return published

    pr_data = _review_pr_for_auto_merge(root, pr_url)
    if pr_data.get("mergeable") != "MERGEABLE" or pr_data.get("mergeStateStatus") != "CLEAN" or pr_data.get("baseRefName") != "main" or pr_data.get("autoMergeRequest") not in (None, {}):
        published["merge_status"] = "blocked"
        published["next_step"] = "PR is not mergeable/clean enough for auto-merge."
        return published

    merge_output = _merge_pr(root, pr_url=pr_url)
    published["merge_status"] = "merged"
    published["merge_commit"] = merge_output
    published["action_taken"] = "pr-created-and-merged"
    if refresh_after_merge:
        published["final_validation"] = {"status": "passed", "checks": _refresh_main_and_validate(root)}
        published["tests_run"] = ["./venv/bin/python -m pytest tests/hermes_cli/test_update_doctor.py -q"]
        published["next_step"] = "Merged low-risk PR and refreshed main locally."
    else:
        published.setdefault("final_validation", {"status": "not-run", "checks": []})
        published.setdefault("tests_run", [])
        published["next_step"] = "Merged low-risk PR; main refresh deferred to the caller."
    return published


def _process_upstream_batch(
    report: dict[str, Any],
    *,
    root: Path,
    batch: dict[str, Any],
    request_pr: bool,
    request_auto_merge_low_risk: bool,
    refresh_after_merge: bool = True,
) -> dict[str, Any]:
    batch_commits = list(batch["commits"])
    batch_files = _batch_changed_files(root, batch_commits)
    batch_risk = _classify_upstream_batch_risk(root, batch_commits)
    batch_label = f"{batch['index']:02d}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    branch_name = f"update-doctor-batch-upstream-{batch_label}"
    worktree_parent = Path(tempfile.mkdtemp(prefix="hermes-update-doctor-batch-"))
    worktree_path = worktree_parent / "worktree"

    try:
        _run_git(["worktree", "add", "--detach", str(worktree_path), "fork/main"], cwd=root)
        _run_git(["switch", "-c", branch_name], cwd=worktree_path)

        for commit in batch_commits:
            cherry = _run_git(["cherry-pick", "--no-edit", commit], cwd=worktree_path, check=False)
            if cherry.returncode != 0:
                conflict_bucket = classify_conflict(
                    patch_id=_commit_patch_id(root, commit),
                    coverage_refs=_coverage_refs(root, commit),
                    touched_files=_commit_files(root, commit),
                    conflicted_files=_git_lines(["diff", "--name-only", "--diff-filter=U"], cwd=worktree_path),
                    subject=_commit_subject(root, commit),
                )
                _run_git(["cherry-pick", "--abort"], cwd=worktree_path, check=False)
                return {
                    "status": "blocked",
                    "reason": f"batch-conflict:{conflict_bucket.value}",
                    "risk": batch_risk,
                    "commits": batch_commits,
                    "files": batch_files,
                    "branch_name": branch_name,
                    "pr_url": None,
                    "merge_commit": None,
                    "published": None,
                }

        batch_report = dict(report)
        batch_report["tool"] = dict(report["tool"])
        batch_report["tool"]["mode"] = "run"
        batch_report["environment"] = dict(report["environment"])
        batch_report["environment"]["branch"] = branch_name
        batch_report["environment"]["status_before"] = _status_lines(worktree_path)
        batch_report["repair"] = {
            "mode": "repair",
            "result": "changed",
            "bucket": "batch-upstream",
            "repair_status": "applied",
            "repair_action": "batch-applied",
            "safety_level": "safe",
            "next_step": "Batch applied",
            "changed_files": batch_files,
        }
        batch_report["verification"] = {"origin_untouched": True, "main_untouched": True}

        published = _publish_run_artifacts(
            batch_report,
            root=worktree_path,
            request_pr=request_pr,
            request_auto_merge_low_risk=request_auto_merge_low_risk,
            refresh_after_merge=refresh_after_merge,
        )
        return {
            "status": published.get("merge_status") if published.get("merge_status") == "merged" else published.get("pr_status", "published"),
            "reason": None if published.get("merge_status") == "merged" else published.get("next_step"),
            "risk": batch_risk,
            "commits": batch_commits,
            "files": batch_files,
            "branch_name": branch_name,
            "pr_url": published.get("pr_url"),
            "merge_commit": published.get("merge_commit"),
            "published": published,
            "worktree_parent": worktree_parent,
            "worktree_path": worktree_path,
        }
    finally:
        _run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=root, check=False)
        shutil.rmtree(worktree_parent, ignore_errors=True)


def _process_upstream_batch_with_fallback(
    report: dict[str, Any],
    *,
    root: Path,
    batch: dict[str, Any],
    request_pr: bool,
    request_auto_merge_low_risk: bool,
    refresh_after_merge: bool = True,
) -> dict[str, Any]:
    batch_commits = list(batch["commits"])
    batch_files = _batch_changed_files(root, batch_commits)
    batch_risk = _classify_upstream_batch_risk(root, batch_commits)
    result = _process_upstream_batch(
        report,
        root=root,
        batch=batch,
        request_pr=request_pr,
        request_auto_merge_low_risk=request_auto_merge_low_risk,
        refresh_after_merge=refresh_after_merge,
    )
    result.setdefault("batch_fallback_used", False)
    result.setdefault("fallback_from_batch_size", batch["size"])
    result.setdefault("fallback_commits_processed", len(batch_commits))
    result.setdefault("fallback_commits_merged", 1 if result.get("merge_commit") else 0)
    result.setdefault("fallback_blocked_commit", None)
    result.setdefault("fallback_blocked_files", [])
    result.setdefault("fallback_blocked_reason", None)
    result["batch_files"] = batch_files
    result["batch_risk"] = batch_risk
    return result


def _fallback_batch_to_individual_commits(
    report: dict[str, Any],
    *,
    root: Path,
    batch: dict[str, Any],
    request_pr: bool,
    request_auto_merge_low_risk: bool,
) -> dict[str, Any]:
    batch_commits = list(batch["commits"])
    fallback: dict[str, Any] = {
        "status": "completed",
        "reason": None,
        "risk": "high",
        "commits": batch_commits,
        "files": _batch_changed_files(root, batch_commits),
        "batch_fallback_used": True,
        "fallback_from_batch_size": batch["size"],
        "fallback_commits_processed": 0,
        "fallback_commits_merged": 0,
        "fallback_blocked_commit": None,
        "fallback_blocked_files": [],
        "fallback_blocked_reason": None,
        "medium_triage_used": False,
        "medium_blocked_commit": None,
        "medium_blocked_subject": None,
        "medium_blocked_files": [],
        "medium_blocked_reasons": [],
        "medium_suggested_tests": [],
        "medium_reclassification_candidate": False,
        "pr_urls": [],
        "merge_commits": [],
        "published": [],
        "tests_run": [],
        "final_validation": {"status": "not-needed", "checks": []},
    }

    for offset, commit in enumerate(batch_commits, start=1):
        single_risk = _classify_upstream_batch_risk(root, [commit])
        single_files = _batch_changed_files(root, [commit])
        fallback["fallback_commits_processed"] += 1

        if single_risk != "low":
            medium_triage: dict[str, Any] = {}
            if single_risk == "medium":
                medium_triage = _triage_medium_commit(root, commit, files=single_files, subject=_commit_subject(root, commit))
            fallback.update(
                {
                    "status": "blocked",
                    "reason": f"batch-risk:{single_risk}",
                    "risk": single_risk,
                    "fallback_blocked_commit": commit,
                    "fallback_blocked_files": single_files,
                    "fallback_blocked_reason": f"batch-risk:{single_risk}",
                }
            )
            if medium_triage:
                fallback.update(
                    {
                        "medium_triage_used": True,
                        "medium_blocked_commit": medium_triage["commit"],
                        "medium_blocked_subject": medium_triage["subject"],
                        "medium_blocked_files": medium_triage["files"],
                        "medium_blocked_reasons": medium_triage["reasons"],
                        "medium_suggested_tests": medium_triage["suggested_tests"],
                        "medium_reclassification_candidate": medium_triage["reclassification_candidate"],
                    }
                )
            return fallback

        if not request_pr:
            continue

        single_batch = {
            "index": f"{batch['index']}.{offset}",
            "size": 1,
            "commits": [commit],
            "first_commit": commit,
            "last_commit": commit,
        }
        single_result = _process_upstream_batch(
            report,
            root=root,
            batch=single_batch,
            request_pr=True,
            request_auto_merge_low_risk=request_auto_merge_low_risk,
            refresh_after_merge=False,
        )
        fallback["pr_urls"].append(single_result.get("pr_url"))

        if single_result.get("status") == "blocked":
            fallback.update(
                {
                    "status": "blocked",
                    "reason": single_result.get("reason") or "batch-processing-blocked",
                    "risk": single_result.get("risk") or single_risk,
                    "fallback_blocked_commit": commit,
                    "fallback_blocked_files": single_result.get("files") or single_files,
                    "fallback_blocked_reason": single_result.get("reason") or "batch-processing-blocked",
                }
            )
            return fallback

        published = single_result.get("published") or {}
        if published.get("merge_status") == "blocked":
            fallback.update(
                {
                    "status": "blocked",
                    "reason": published.get("next_step") or "batch-auto-merge-blocked",
                    "risk": single_risk,
                    "fallback_blocked_commit": commit,
                    "fallback_blocked_files": single_files,
                    "fallback_blocked_reason": published.get("next_step") or "batch-auto-merge-blocked",
                }
            )
            return fallback

        if single_result.get("merge_commit"):
            fallback["fallback_commits_merged"] += 1
            fallback["merge_commits"].append(single_result["merge_commit"])
            fallback["tests_run"] = published.get("tests_run") or fallback["tests_run"]
            fallback["final_validation"] = published.get("final_validation") or fallback["final_validation"]
        else:
            fallback["status"] = "pr-created"
            fallback["reason"] = published.get("next_step") or single_result.get("reason")

    if fallback["fallback_commits_processed"] and fallback["fallback_commits_merged"] == fallback["fallback_commits_processed"]:
        fallback["status"] = "completed"
        fallback["reason"] = None
    fallback["material_changes_detected"] = fallback["fallback_commits_merged"] > 0 or bool(fallback["pr_urls"])
    return fallback


def _triage_medium_commit(
    root: Path,
    commit: str,
    *,
    files: Sequence[str] | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    resolved_files = list(files) if files is not None else _commit_files(root, commit)
    resolved_subject = subject or _commit_subject(root, commit)
    reasons: list[str] = []
    suggested_tests: list[str] = []

    if "hermes_cli/web_server.py" in resolved_files:
        reasons.append("touches hermes_cli/web_server.py (runtime web server path)")
        suggested_tests.append("pytest tests/hermes_cli/test_web_server.py -k profile -q")

    web_files = [path for path in resolved_files if path.startswith("web/")]
    if web_files:
        reasons.append(f"touches web UI/API files ({len(web_files)} path(s))")
        suggested_tests.append("cd web && npm run build")

    test_files = [path for path in resolved_files if path.startswith("tests/")]
    if test_files:
        reasons.append(f"updates targeted tests ({len(test_files)} path(s))")
        suggested_tests.append("pytest tests/hermes_cli/test_web_server.py -q")

    if not reasons:
        reasons.append("contains mixed-risk files outside the low-risk docs/tests allowlist")

    deduped_tests: list[str] = []
    for test in suggested_tests:
        if test not in deduped_tests:
            deduped_tests.append(test)

    return {
        "commit": commit,
        "subject": resolved_subject,
        "files": resolved_files,
        "reasons": reasons,
        "suggested_tests": deduped_tests,
        "reclassification_candidate": not any(_is_high_risk_path(path) for path in resolved_files),
    }


def _batch_upstream_run(
    report: dict[str, Any],
    *,
    root: Path,
    request_pr: bool,
    request_auto_merge_low_risk: bool,
    batch_size: int,
) -> dict[str, Any]:
    batches = _plan_upstream_batches(root, batch_size)
    summary: dict[str, Any] = {
        "batch_upstream": True,
        "batch_size": batch_size,
        "batches_total": len(batches),
        "batches_processed": 0,
        "batches_merged": 0,
        "batches_blocked": 0,
        "blocked_batch_reason": None,
        "blocked_batch_commits": [],
        "blocked_batch_files": [],
        "pr_urls": [],
        "merge_commits": [],
        "upstream_batches": [],
        "batch_fallback_used": False,
        "fallback_from_batch_size": None,
        "fallback_commits_processed": 0,
        "fallback_commits_merged": 0,
        "fallback_blocked_commit": None,
        "fallback_blocked_files": [],
        "fallback_blocked_reason": None,
        "medium_triage_used": False,
        "medium_blocked_commit": None,
        "medium_blocked_subject": None,
        "medium_blocked_files": [],
        "medium_blocked_reasons": [],
        "medium_suggested_tests": [],
        "medium_reclassification_candidate": False,
        "final_status": "completed" if not batches else "planned",
        "integration_status": "batched-upstream",
        "integration_risk_level": "low",
        "integration_blockers": [],
        "run_status": "completed" if not batches else "planned",
        "pr_status": "no-pr-needed",
        "merge_status": "not-needed",
        "risk_level": "low",
        "material_changes_detected": False,
        "tests_run": [],
        "final_validation": {"status": "not-needed", "checks": []},
        "next_step": "No upstream batches were pending." if not batches else f"Planned {len(batches)} upstream batches of size {batch_size}.",
    }

    if not batches:
        return summary

    for batch in batches:
        batch_risk = _classify_upstream_batch_risk(root, batch["commits"])
        batch_files = _batch_changed_files(root, batch["commits"])
        summary["batches_processed"] += 1
        summary["upstream_batches"].append(
            {
                "index": batch["index"],
                "size": batch["size"],
                "commits": batch["commits"],
                "files": batch_files,
                "risk": batch_risk,
            }
        )

        if batch_risk != "low" and len(batch["commits"]) > 1:
            fallback_result = _fallback_batch_to_individual_commits(
                report,
                root=root,
                batch=batch,
                request_pr=request_pr,
                request_auto_merge_low_risk=request_auto_merge_low_risk,
            )
            summary["batch_fallback_used"] = True
            summary["fallback_from_batch_size"] = batch["size"]
            summary["fallback_commits_processed"] += fallback_result.get("fallback_commits_processed", 0)
            summary["fallback_commits_merged"] += fallback_result.get("fallback_commits_merged", 0)
            summary["pr_urls"].extend([url for url in fallback_result.get("pr_urls", []) if url])
            summary["merge_commits"].extend(fallback_result.get("merge_commits", []))
            summary["material_changes_detected"] = summary["material_changes_detected"] or fallback_result.get("material_changes_detected", False)
            summary["upstream_batches"][-1]["fallback_used"] = True
            summary["upstream_batches"][-1]["fallback_commits_processed"] = fallback_result.get("fallback_commits_processed", 0)
            summary["upstream_batches"][-1]["fallback_commits_merged"] = fallback_result.get("fallback_commits_merged", 0)

            if fallback_result.get("fallback_blocked_commit"):
                summary["batches_blocked"] += 1
                summary["blocked_batch_reason"] = fallback_result.get("fallback_blocked_reason") or "batch-processing-blocked"
                summary["blocked_batch_commits"] = [fallback_result["fallback_blocked_commit"]]
                summary["blocked_batch_files"] = fallback_result.get("fallback_blocked_files") or []
                summary["fallback_blocked_commit"] = fallback_result.get("fallback_blocked_commit")
                summary["fallback_blocked_files"] = fallback_result.get("fallback_blocked_files") or []
                summary["fallback_blocked_reason"] = fallback_result.get("fallback_blocked_reason")
                summary["medium_triage_used"] = fallback_result.get("medium_triage_used", False)
                summary["medium_blocked_commit"] = fallback_result.get("medium_blocked_commit")
                summary["medium_blocked_subject"] = fallback_result.get("medium_blocked_subject")
                summary["medium_blocked_files"] = fallback_result.get("medium_blocked_files") or []
                summary["medium_blocked_reasons"] = fallback_result.get("medium_blocked_reasons") or []
                summary["medium_suggested_tests"] = fallback_result.get("medium_suggested_tests") or []
                summary["medium_reclassification_candidate"] = fallback_result.get("medium_reclassification_candidate", False)
                summary["final_status"] = "blocked"
                summary["run_status"] = "blocked"
                summary["integration_status"] = "blocked-batch-risk"
                summary["integration_risk_level"] = fallback_result.get("risk") or batch_risk
                summary["integration_blockers"] = [summary["blocked_batch_reason"]] if summary["blocked_batch_reason"] else ["batch-upstream-high-risk"]
                summary["next_step"] = f"Batch {batch['index']} split into commits and blocked on {fallback_result['fallback_blocked_commit']}."
                break

            if fallback_result.get("fallback_commits_merged"):
                summary["batches_merged"] += fallback_result.get("fallback_commits_merged", 0)
                summary["tests_run"] = fallback_result.get("tests_run") or summary["tests_run"]
                summary["final_validation"] = fallback_result.get("final_validation") or summary["final_validation"]
                summary["next_step"] = f"Batch {batch['index']} split into individual commits and processed successfully."
                continue

            summary["next_step"] = f"Batch {batch['index']} split into individual commits; no auto-merge was performed."
            summary["final_status"] = "pr-created"
            summary["run_status"] = "needs-integration"
            summary["integration_status"] = "batched-upstream"
            summary["integration_risk_level"] = "low"
            summary["integration_blockers"] = []
            continue

        if batch_risk != "low":
            summary["batches_blocked"] += 1
            summary["blocked_batch_reason"] = f"batch-risk:{batch_risk}"
            summary["blocked_batch_commits"] = batch["commits"]
            summary["blocked_batch_files"] = batch_files
            summary["final_status"] = "blocked"
            summary["run_status"] = "blocked"
            summary["integration_status"] = "blocked-batch-risk"
            summary["integration_risk_level"] = batch_risk
            summary["integration_blockers"] = ["batch-upstream-high-risk"]
            summary["next_step"] = f"Batch {batch['index']} blocked as {batch_risk}; no further batches were attempted."
            break

        if not request_pr:
            continue

        batch_result = _process_upstream_batch(
            report,
            root=root,
            batch=batch,
            request_pr=True,
            request_auto_merge_low_risk=request_auto_merge_low_risk,
            refresh_after_merge=False,
        )
        summary["pr_urls"].append(batch_result.get("pr_url"))
        if batch_result.get("status") == "blocked":
            summary["batches_blocked"] += 1
            summary["blocked_batch_reason"] = batch_result.get("reason") or "batch-processing-blocked"
            summary["blocked_batch_commits"] = batch_result.get("commits") or batch["commits"]
            summary["blocked_batch_files"] = batch_result.get("files") or batch_files
            summary["final_status"] = "blocked"
            summary["run_status"] = "blocked"
            summary["integration_status"] = "blocked-batch-risk"
            summary["integration_risk_level"] = batch_risk
            summary["integration_blockers"] = [summary["blocked_batch_reason"]] if summary["blocked_batch_reason"] else ["batch-upstream-blocked"]
            summary["next_step"] = f"Batch {batch['index']} blocked during application; no further batches were attempted."
            break

        if batch_result.get("merge_commit"):
            summary["batches_merged"] += 1
            summary["merge_commits"].append(batch_result["merge_commit"])
            refresh_checks = _refresh_main_and_validate(root)
            summary["tests_run"] = refresh_checks
            summary["final_validation"] = {"status": "passed", "checks": refresh_checks}
            summary["next_step"] = f"Batch {batch['index']} merged successfully."
            continue

        published = batch_result.get("published") or {}
        merge_status = published.get("merge_status")
        if merge_status == "blocked":
            summary["batches_blocked"] += 1
            summary["blocked_batch_reason"] = published.get("next_step") or "batch-auto-merge-blocked"
            summary["blocked_batch_commits"] = batch["commits"]
            summary["blocked_batch_files"] = batch_files
            summary["final_status"] = "blocked"
            summary["run_status"] = "blocked"
            summary["integration_status"] = "blocked-batch-risk"
            summary["integration_risk_level"] = batch_risk
            summary["integration_blockers"] = ["batch-auto-merge-blocked"]
            summary["next_step"] = f"Batch {batch['index']} PR was not mergeable/clean enough for auto-merge."
            break

        summary["next_step"] = f"Batch {batch['index']} PR created; auto-merge was not performed."
        summary["final_status"] = "pr-created"
        summary["run_status"] = "needs-integration"
        summary["integration_status"] = "batched-upstream"
        summary["integration_risk_level"] = "low"
        summary["integration_blockers"] = []

    if summary["batches_blocked"] == 0 and summary["batches_merged"] == summary["batches_total"]:
        summary["final_status"] = "completed"
        summary["run_status"] = "completed"
        summary["integration_status"] = "batched-completed"
        summary["integration_risk_level"] = "low"
        summary["integration_blockers"] = []
        summary["next_step"] = "All upstream batches were processed successfully."
    summary["material_changes_detected"] = summary["batches_merged"] > 0 or bool(summary["pr_urls"])
    return summary


def _with_mode(report: dict[str, Any], mode: str, *, repair: dict[str, Any] | None = None) -> dict[str, Any]:
    derived = dict(report)
    derived["tool"] = dict(report["tool"])
    derived["tool"]["mode"] = mode
    if repair is not None:
        derived["repair"] = repair
    return derived


def _format_list(title: str, items: Sequence[str], *, indent: str = "  ") -> list[str]:
    lines = [f"{indent}{title}:"]
    if not items:
        lines.append(f"{indent}  <none>")
        return lines
    lines.extend(f"{indent}  {item}" for item in items)
    return lines


def _build_text_report(report: dict[str, Any]) -> str:
    env = report["environment"]
    replay = report["replay"]
    conflict = report.get("conflict")
    lines = [
        "Hermes Update Doctor",
        f"Schema version: {report['schema_version']}",
        f"Timestamp: {report['timestamp']}",
        f"Project root: {env['project_root']}",
        f"Branch: {env['branch']}",
        f"Upstream: {env['upstream'] or '<none>'}",
        f"Remote fork fetch: {env['remotes']['fork']['fetch'] or '<missing>'}",
        f"Remote fork push:  {env['remotes']['fork']['push'] or '<missing>'}",
        f"Remote origin fetch: {env['remotes']['origin']['fetch'] or '<missing>'}",
        f"Remote origin push:  {env['remotes']['origin']['push'] or '<missing>'}",
        "",
        "Preflight",
        "Status:",
    ]
    lines.extend(f"  {line}" for line in env["status_before"])
    lines.append(f"Ahead/behind vs origin/main: ahead={env['ahead']} behind={env['behind']}")
    lines.append(f"Origin ahead count: {report.get('origin_ahead_count', env.get('origin_ahead_count', env['behind']))}")
    lines.append(f"Replay base: {replay['base']}")
    lines.append(f"Rescue ref: {replay['rescue_ref']}")
    lines.append(f"Replay candidates: {replay['candidate_count']}")
    if replay.get("skipped_duplicate_commits"):
        lines.append("Skipped duplicate commits:")
        for skipped in replay["skipped_duplicate_commits"]:
            lines.append(f"  - {skipped['commit']} {skipped['subject']} [{skipped['patch_id']}]")
    if replay.get("skipped_safe_commits"):
        lines.append("Skipped safe commits:")
        for skipped in replay["skipped_safe_commits"]:
            lines.append(f"  - {skipped['commit']} {skipped['subject']} [{skipped['bucket']}]")
    lines.append(f"Replay continued after safe skip: {str(bool(replay.get('replay_continued_after_skip'))).lower()}")
    lines.append("")
    lines.append("Replay simulation")
    lines.append(f"Result: {replay['result']}")
    if conflict:
        lines.append(f"Conflict commit: {conflict['commit']}")
        lines.append(f"Subject: {conflict['subject']}")
        lines.append(f"Classification bucket: {conflict['bucket']}")
        lines.append(f"Patch-id: {conflict['patch_id'] or '<unavailable>'}")
        lines.extend(_format_list("Files touched by commit", conflict["touched_files"], indent=""))
        lines.extend(_format_list("Conflicted files", conflict["conflicted_files"], indent=""))
        lines.extend(_format_list("Coverage refs", conflict["coverage_refs"], indent=""))
        lines.extend(_format_list("Branch contains commit", conflict["main_contains"], indent=""))
        lines.extend(_format_list("Remote branches contain commit", conflict["remote_contains"], indent=""))
        lines.extend(_format_list("Worktree status at conflict", conflict["worktree_status"], indent=""))
    else:
        lines.append(f"Applied commits: {replay['applied_count']}")
    lines.append("")
    lines.append("Structured report")
    lines.append(f"  result: {replay['result']}")
    lines.append(f"  origin untouched: {str(report['verification']['origin_untouched']).lower()}")
    lines.append(f"  main untouched: {str(report['verification']['main_untouched']).lower()}")
    if "run_status" in report:
        lines.append("")
        lines.append("Run")
        lines.append(f"  mode: {report['mode']}")
        lines.append(f"  result: {report['result']}")
        lines.append(f"  bucket: {report['bucket'] or '<none>'}")
        lines.append(f"  run status: {report['run_status']}")
        lines.append(f"  repair status: {report['repair_status']}")
        lines.append(f"  action taken: {report['action_taken']}")
        lines.append(f"  safety level: {report['safety_level']}")
        lines.append(f"  risk level: {report['risk_level']}")
        lines.append(f"  integration risk level: {report.get('integration_risk_level', 'low')}")
        blockers = report.get('integration_blockers') or []
        lines.append(f"  integration blockers: {', '.join(blockers) if blockers else '<none>'}")
        lines.append(f"  pr status: {report['pr_status']}")
        lines.append(f"  pr url: {report['pr_url'] or '<none>'}")
        lines.append(f"  merge status: {report['merge_status']}")
        lines.append(f"  merge commit: {report['merge_commit'] or '<none>'}")
        lines.append(f"  branch name: {report['branch_name']}")
        lines.append(f"  next step: {report['next_step']}")
        lines.append(f"  origin ahead count: {report.get('origin_ahead_count', report['environment'].get('origin_ahead_count', report['environment']['behind']))}")
        lines.append(f"  skipped safe commits: {len(report.get('skipped_safe_commits') or [])}")
        lines.append(f"  replay continued after safe skip: {str(bool(report.get('replay_continued_after_skip'))).lower()}")
        lines.append(f"  integration status: {report.get('integration_status', 'not-needed')}")
        lines.append(f"  material changes detected: {str(bool(report.get('material_changes_detected'))).lower()}")
        lines.append(f"  tests run: {', '.join(report['tests_run']) if report['tests_run'] else '<none>'}")
        final_validation = report.get('final_validation') or {'status': 'not-needed', 'checks': []}
        lines.append(f"  final validation: {final_validation['status']}")
        if final_validation.get('checks'):
            lines.append(f"    checks: {', '.join(final_validation['checks'])}")
    if report.get("batch_upstream"):
        lines.append("")
        lines.append("Batch upstream")
        lines.append(f"  batch size: {report.get('batch_size')}")
        lines.append(f"  batches total: {report.get('batches_total')}")
        lines.append(f"  batches processed: {report.get('batches_processed')}")
        lines.append(f"  batches merged: {report.get('batches_merged')}")
        lines.append(f"  batches blocked: {report.get('batches_blocked')}")
        lines.append(f"  batch fallback used: {bool(report.get('batch_fallback_used'))}")
        lines.append(f"  fallback from batch size: {report.get('fallback_from_batch_size') or '<none>'}")
        lines.append(f"  fallback commits processed: {report.get('fallback_commits_processed')}")
        lines.append(f"  fallback commits merged: {report.get('fallback_commits_merged')}")
        lines.append(f"  fallback blocked reason: {report.get('fallback_blocked_reason') or '<none>'}")
        lines.append(f"  fallback blocked commit: {report.get('fallback_blocked_commit') or '<none>'}")
        lines.append(f"  medium triage used: {bool(report.get('medium_triage_used'))}")
        lines.append(f"  medium blocked commit: {report.get('medium_blocked_commit') or '<none>'}")
        lines.append(f"  medium blocked subject: {report.get('medium_blocked_subject') or '<none>'}")
        lines.append(f"  medium blocked files: {len(report.get('medium_blocked_files') or [])}")
        if report.get("medium_blocked_reasons"):
            lines.append(f"  medium blocked reasons: {'; '.join(report['medium_blocked_reasons'])}")
        if report.get("medium_suggested_tests"):
            lines.append(f"  medium suggested tests: {'; '.join(report['medium_suggested_tests'])}")
        lines.append(f"  medium reclassification candidate: {bool(report.get('medium_reclassification_candidate'))}")
        lines.append(f"  blocked batch reason: {report.get('blocked_batch_reason') or '<none>'}")
        lines.append(f"  blocked batch commits: {len(report.get('blocked_batch_commits') or [])}")
        lines.append(f"  blocked batch files: {len(report.get('blocked_batch_files') or [])}")
        lines.append(f"  pr urls: {len(report.get('pr_urls') or [])}")
        lines.append(f"  merge commits: {len(report.get('merge_commits') or [])}")
        lines.append(f"  final status: {report.get('final_status')}")
    repair = report.get("repair")
    if repair:
        lines.append("")
        lines.append("Repair")
        lines.append(f"  mode: {repair['mode']}")
        lines.append(f"  result: {repair['result']}")
        lines.append(f"  bucket: {repair['bucket'] or '<none>'}")
        lines.append(f"  repair status: {repair['repair_status']}")
        lines.append(f"  repair action: {repair['repair_action']}")
        lines.append(f"  safety level: {repair['safety_level']}")
        lines.append(f"  next step: {repair['next_step']}")
    return "\n".join(lines)


def _stable_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _stable_yaml(report: dict[str, Any]) -> str:
    if yaml is None:  # pragma: no cover - dependency is expected to be available.
        raise SystemExit(f"✗ PyYAML is required for YAML output: {_YAML_IMPORT_ERROR}")
    return yaml.safe_dump(report, sort_keys=True, allow_unicode=True)


def _build_report(*, root: Path, replay_base: str = DEFAULT_REPLAY_BASE) -> dict[str, Any]:
    branch = _branch_current(root)
    upstream = _git_optional_output(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=root)
    status_before = _status_lines(root)
    ahead, behind = _counts_vs_origin(root)

    remotes = {
        "fork": {"fetch": _remote_url(root, "fork"), "push": _remote_url(root, "fork", push=True)},
        "origin": {"fetch": _remote_url(root, "origin"), "push": _remote_url(root, "origin", push=True)},
    }
    if not remotes["fork"]["fetch"]:
        raise SystemExit("✗ missing fork remote")
    if not remotes["origin"]["fetch"]:
        raise SystemExit("✗ missing origin remote")

    rescue_ref = f"refs/rescue/hermes-update-doctor-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    _run_git(["update-ref", rescue_ref, "HEAD"], cwd=root)

    candidates = _candidate_commits(root, replay_base=replay_base)
    candidate_count = len(candidates)

    worktree_parent = Path(tempfile.mkdtemp(prefix="hermes-update-doctor-"))
    worktree_path = worktree_parent / "worktree"
    skipped_duplicate_commits: list[dict[str, Any]] = []
    skipped_safe_commits: list[dict[str, Any]] = []
    replay_continued_after_skip = False
    applied_count = 0
    conflict_commit: str | None = None
    conflict_pid: str | None = None
    conflict_subject: str | None = None
    conflict_files: list[str] = []
    conflict_status: list[str] = []
    seen_patch_ids: set[str] = set()

    try:
        _run_git(["worktree", "add", "--detach", str(worktree_path), replay_base], cwd=root)
        for commit in candidates:
            patch_id = _commit_patch_id(root, commit)
            subject = _commit_subject(root, commit)
            files_touched = _commit_files(root, commit)
            if patch_id and patch_id in seen_patch_ids:
                skipped_duplicate_commits.append(
                    {
                        "commit": commit,
                        "subject": subject,
                        "patch_id": patch_id,
                        "bucket": ConflictBucket.PATCH_ID_DUPLICATE.value,
                    }
                )
                continue
            cherry = _run_git(["cherry-pick", "--no-edit", commit], cwd=worktree_path, check=False)
            if cherry.returncode != 0:
                conflict_bucket = classify_conflict(
                    patch_id=patch_id,
                    coverage_refs=_coverage_refs(root, commit),
                    touched_files=files_touched,
                    conflicted_files=_git_lines(["diff", "--name-only", "--diff-filter=U"], cwd=worktree_path),
                    subject=subject,
                    seen_patch_ids=seen_patch_ids,
                )
                if conflict_bucket.value in SAFE_SKIP_BUCKETS:
                    _run_git(["cherry-pick", "--abort"], cwd=worktree_path, check=False)
                    skipped_safe_commits.append(
                        {
                            "commit": commit,
                            "subject": subject,
                            "patch_id": patch_id,
                            "bucket": conflict_bucket.value,
                        }
                    )
                    replay_continued_after_skip = True
                    if patch_id:
                        seen_patch_ids.add(patch_id)
                    continue
                conflict_commit = commit
                conflict_pid = patch_id
                conflict_subject = subject
                conflict_files = _git_lines(["diff", "--name-only", "--diff-filter=U"], cwd=worktree_path)
                conflict_status = _status_lines(worktree_path)
                break
            applied_count += 1
            if patch_id:
                seen_patch_ids.add(patch_id)

        if conflict_commit is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "tool": {"name": TOOL_NAME, "mode": "analyze"},
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "environment": {
                    "project_root": str(root),
                    "branch": branch,
                    "upstream": upstream,
                    "remotes": remotes,
                    "status_before": status_before,
                    "ahead": ahead,
                    "behind": behind,
                    "origin_ahead_count": behind,
                    "origin_behind_count": ahead,
                },
                "replay": {
                    "base": replay_base,
                    "candidate_count": candidate_count,
                    "applied_count": applied_count,
                    "skipped_duplicate_commits": skipped_duplicate_commits,
                    "skipped_safe_commits": skipped_safe_commits,
                    "replay_continued_after_skip": replay_continued_after_skip,
                    "rescue_ref": rescue_ref,
                    "result": "passed",
                },
                "conflict": None,
                "verification": {"origin_untouched": True, "main_untouched": True},
            }

        coverage_refs = _coverage_refs(root, conflict_commit)
        conflict = {
            "commit": conflict_commit,
            "subject": conflict_subject,
            "patch_id": conflict_pid,
            "bucket": conflict_bucket.value,
            "touched_files": _commit_files(root, conflict_commit),
            "conflicted_files": conflict_files,
            "coverage_refs": coverage_refs,
            "main_contains": _parse_branch_contains(_git_optional_lines(["branch", "--contains", conflict_commit], cwd=root)),
            "remote_contains": _parse_branch_contains(_git_optional_lines(["branch", "-r", "--contains", conflict_commit], cwd=root)),
            "worktree_status": conflict_status,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": TOOL_NAME, "mode": "analyze"},
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "environment": {
                "project_root": str(root),
                "branch": branch,
                "upstream": upstream,
                "remotes": remotes,
                "status_before": status_before,
                "ahead": ahead,
                "behind": behind,
                "origin_ahead_count": behind,
                "origin_behind_count": ahead,
            },
            "replay": {
                "base": replay_base,
                "candidate_count": candidate_count,
                "applied_count": applied_count,
                "skipped_duplicate_commits": skipped_duplicate_commits,
                "skipped_safe_commits": skipped_safe_commits,
                "replay_continued_after_skip": replay_continued_after_skip,
                "rescue_ref": rescue_ref,
                "result": "conflict",
            },
            "conflict": conflict,
            "verification": {"origin_untouched": True, "main_untouched": True},
        }
    finally:
        _run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=root, check=False)
        shutil.rmtree(worktree_parent, ignore_errors=True)


def _coverage_refs(root: Path, commit: str) -> list[str]:
    refs = []
    for ref in ("refs/heads/main", "refs/remotes/fork/main", "refs/remotes/origin/main"):
        if _contains_commit(root, ref, commit):
            refs.append(ref)
    return refs


def build_report(*, cwd: Path | None = None, replay_base: str = DEFAULT_REPLAY_BASE) -> dict[str, Any]:
    root = _require_repo_root(cwd)
    return _build_report(root=root, replay_base=replay_base)


def render_report(report: dict[str, Any], output_format: str) -> str:
    output_format = output_format.lower()
    if output_format == "json":
        return _stable_json(report)
    if output_format == "yaml":
        return _stable_yaml(report)
    if output_format == "text":
        return _build_text_report(report) + "\n"
    raise SystemExit(f"✗ unsupported format: {output_format}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description="Diagnosis-first Hermes update replay doctor")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--analyze", action="store_true", help="Run the diagnosis-only replay analysis")
    mode.add_argument("--run", action="store_true", help="Run the one-command update doctor orchestration flow")
    mode.add_argument("--repair", action="store_true", help="Run the diagnosis-only safe-repair decision flow")
    parser.add_argument("--pr", action="store_true", help="Create a fork PR when a validated material change exists")
    parser.add_argument(
        "--auto-merge-low-risk",
        action="store_true",
        help="Automatically merge low-risk PRs after review metadata checks",
    )
    parser.add_argument(
        "--batch-upstream",
        action="store_true",
        help="Process broad upstream syncs in small low-risk batches",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_UPSTREAM_DEFAULT_SIZE,
        help="Batch size for upstream processing (default: 5)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "yaml"),
        default="text",
        help="Output format for the report",
    )
    parser.add_argument(
        "--replay-base",
        default=DEFAULT_REPLAY_BASE,
        help="Replay base ref to analyze (default: origin/main)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.auto_merge_low_risk and not args.pr:
        parser.error("--auto-merge-low-risk requires --pr")
    if args.pr and not args.run:
        parser.error("--pr is only supported with --run")
    if args.auto_merge_low_risk and not args.run:
        parser.error("--auto-merge-low-risk is only supported with --run")
    if args.batch_upstream and not args.run:
        parser.error("--batch-upstream is only supported with --run")
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")

    root = _require_repo_root()
    report = build_report(cwd=root, replay_base=args.replay_base)
    exit_code = 0 if report["replay"]["result"] == "passed" else 1

    if args.run:
        if args.batch_upstream and _origin_ahead_count(report) >= BROAD_UPSTREAM_SYNC_THRESHOLD:
            batch_summary = _batch_upstream_run(
                report,
                root=root,
                request_pr=args.pr,
                request_auto_merge_low_risk=args.auto_merge_low_risk,
                batch_size=args.batch_size,
            )
            report = _with_mode(report, "run")
            report.update(batch_summary)
            exit_code = 0 if batch_summary["final_status"] == "completed" else 1
        else:
            run = _run_summary(report)
            report = _with_mode(report, "run")
            report.update(run)
            exit_code = 0 if run["run_status"] in {"clean", "completed"} else 1
            if args.pr:
                published = _publish_run_artifacts(
                    report,
                    root=root,
                    request_pr=True,
                    request_auto_merge_low_risk=args.auto_merge_low_risk,
                )
                report.update(published)
    elif args.repair:
        repair = _repair_summary(report)
        report = _with_mode(report, "repair", repair=repair)
        exit_code = 0 if repair["repair_status"] in {"skip-safe", "not-needed"} else 1

    sys.stdout.write(render_report(report, args.format))
    return exit_code



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

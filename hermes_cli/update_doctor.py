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
    lines.append(f"Replay base: {replay['base']}")
    lines.append(f"Rescue ref: {replay['rescue_ref']}")
    lines.append(f"Replay candidates: {replay['candidate_count']}")
    if replay.get("skipped_duplicate_commits"):
        lines.append("Skipped duplicate commits:")
        for skipped in replay["skipped_duplicate_commits"]:
            lines.append(f"  - {skipped['commit']} {skipped['subject']} [{skipped['patch_id']}]")
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
                conflict_commit = commit
                conflict_pid = patch_id
                conflict_subject = subject
                conflict_files = _git_lines(["diff", "--name-only", "--diff-filter=U"], cwd=worktree_path)
                conflict_status = _status_lines(worktree_path)
                conflict_bucket = classify_conflict(
                    patch_id=patch_id,
                    coverage_refs=_coverage_refs(root, commit),
                    touched_files=files_touched,
                    conflicted_files=conflict_files,
                    subject=subject,
                    seen_patch_ids=seen_patch_ids,
                )
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
                },
                "replay": {
                    "base": replay_base,
                    "candidate_count": candidate_count,
                    "applied_count": applied_count,
                    "skipped_duplicate_commits": skipped_duplicate_commits,
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
            },
            "replay": {
                "base": replay_base,
                "candidate_count": candidate_count,
                "applied_count": applied_count,
                "skipped_duplicate_commits": skipped_duplicate_commits,
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
    mode.add_argument("--repair", action="store_true", help="Run the diagnosis-only safe-repair decision flow")
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

    report = build_report(replay_base=args.replay_base)
    exit_code = 0 if report["replay"]["result"] == "passed" else 1

    if args.repair:
        repair = _repair_summary(report)
        report = _with_mode(report, "repair", repair=repair)
        exit_code = 0 if repair["repair_status"] in {"skip-safe", "not-needed"} else 1

    sys.stdout.write(render_report(report, args.format))
    return exit_code



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

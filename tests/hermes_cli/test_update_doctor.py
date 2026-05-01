from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_cli.update_doctor import (
    ConflictBucket,
    classify_conflict,
    main as doctor_main,
    render_report,
    _classify_pr_risk,
    _publish_run_artifacts,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_status() -> str:
    return subprocess.run(
        ["git", "status", "--short", "--branch"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _sample_report(*, repair: dict | None = None, replay_result: str = "conflict", conflict: dict | None = None) -> dict:
    report = {
        "schema_version": 1,
        "tool": {"name": "hermes-update-doctor", "mode": "analyze"},
        "timestamp": "2026-04-30T00:00:00Z",
        "environment": {
            "project_root": "/opt/hermes-agent",
            "branch": "main",
            "upstream": "fork/main",
            "remotes": {
                "fork": {"fetch": "git@github.com:Vottam/hermes-agent.git", "push": "git@github.com:Vottam/hermes-agent.git"},
                "origin": {"fetch": "git@github.com:NousResearch/hermes-agent.git", "push": "git@github.com:NousResearch/hermes-agent.git"},
            },
            "status_before": ["## main...fork/main"],
            "ahead": 111,
            "behind": 99,
            "origin_ahead_count": 99,
            "origin_behind_count": 111,
        },
        "replay": {
            "base": "origin/main",
            "candidate_count": 104,
            "applied_count": 103,
            "skipped_duplicate_commits": [
                {
                    "commit": "deadbeef",
                    "subject": "fix(update): skip duplicate",
                    "patch_id": "patch-1",
                    "bucket": "patch-id-duplicate",
                }
            ],
            "skipped_safe_commits": [
                {
                    "commit": "cafebabe",
                    "subject": "fix(update): already covered",
                    "patch_id": "patch-2",
                    "bucket": "already-covered-in-fork-main",
                }
            ],
            "replay_continued_after_skip": False,
            "rescue_ref": "refs/rescue/hermes-update-doctor-20260430-000000",
            "result": replay_result,
        },
        "conflict": conflict
        if conflict is not None
        else {
            "commit": "7c7fedae2a3a2bf92cdffc9c67026286af1baf46",
            "subject": "fix(update): print final update safety report",
            "patch_id": "c270a715455b204a1ca445a906bf1e1e7f1c9c70",
            "bucket": "already-covered-in-fork-main",
            "touched_files": ["hermes_cli/main.py"],
            "conflicted_files": ["hermes_cli/main.py"],
            "coverage_refs": ["refs/heads/main", "refs/remotes/fork/main"],
            "main_contains": ["main"],
            "remote_contains": ["fork/main"],
            "worktree_status": ["UU hermes_cli/main.py"],
        },
        "verification": {"origin_untouched": True, "main_untouched": True},
    }
    if repair is not None:
        report["repair"] = repair
    return report


def test_formal_buckets_are_stable_and_unique() -> None:
    values = [bucket.value for bucket in ConflictBucket]
    assert len(values) == len(set(values))
    assert set(values) == {
        "patch-id-duplicate",
        "already-covered-in-fork-main",
        "obsolete",
        "test-desync",
        "single-file-hunk-conflict",
        "runtime-sensitive",
        "critical",
        "multi-file-or-runtime-conflict",
    }


def test_classify_conflict_prefers_coverage_over_runtime_risk() -> None:
    bucket = classify_conflict(
        patch_id="abc123",
        coverage_refs=["refs/remotes/fork/main"],
        touched_files=["hermes_cli/main.py", "tests/hermes_cli/test_update_final_report.py"],
        conflicted_files=["hermes_cli/main.py"],
        subject="fix(update): print final update safety report",
    )
    assert bucket is ConflictBucket.ALREADY_COVERED_IN_FORK_MAIN


def test_classify_conflict_covers_basic_buckets() -> None:
    assert (
        classify_conflict(
            patch_id="dup-1",
            coverage_refs=[],
            touched_files=["hermes_cli/main.py"],
            conflicted_files=["hermes_cli/main.py"],
            seen_patch_ids={"dup-1"},
        )
        is ConflictBucket.PATCH_ID_DUPLICATE
    )

    assert (
        classify_conflict(
            patch_id=None,
            coverage_refs=[],
            touched_files=["tests/hermes_cli/test_update_doctor.py"],
            conflicted_files=["tests/hermes_cli/test_update_doctor.py"],
        )
        is ConflictBucket.TEST_DESYNC
    )

    assert (
        classify_conflict(
            patch_id=None,
            coverage_refs=[],
            touched_files=["gateway/run.py"],
            conflicted_files=["gateway/run.py"],
        )
        is ConflictBucket.CRITICAL
    )

    assert (
        classify_conflict(
            patch_id=None,
            coverage_refs=[],
            touched_files=["hermes_cli/main.py"],
            conflicted_files=["hermes_cli/main.py"],
        )
        is ConflictBucket.SINGLE_FILE_HUNK_CONFLICT
    )


def test_render_report_json_and_yaml_round_trip() -> None:
    report = _sample_report()
    json_text = render_report(report, "json")
    yaml_text = render_report(report, "yaml")

    assert json.loads(json_text) == report
    assert yaml.safe_load(yaml_text) == report
    assert json.loads(json_text)["replay"]["skipped_duplicate_commits"][0]["bucket"] == "patch-id-duplicate"
    assert json.loads(json_text)["replay"]["skipped_safe_commits"][0]["bucket"] == "already-covered-in-fork-main"
    assert json.loads(json_text)["environment"]["origin_ahead_count"] == 99


def test_render_report_repair_round_trip() -> None:
    report = _sample_report(
        repair={
            "mode": "repair",
            "result": "skip-safe",
            "bucket": "already-covered-in-fork-main",
            "repair_status": "skip-safe",
            "repair_action": "no-op",
            "safety_level": "safe",
            "next_step": "Commit is already covered in fork/main or main; no repair was applied.",
        }
    )

    json_text = render_report(report, "json")
    yaml_text = render_report(report, "yaml")

    assert json.loads(json_text) == report
    assert yaml.safe_load(yaml_text) == report
    assert json.loads(json_text)["repair"]["repair_status"] == "skip-safe"


def test_render_report_run_round_trip() -> None:
    report = _sample_report(
        replay_result="passed",
        conflict=None,
    )
    report.update(
        {
            "mode": "run",
            "result": "clean",
            "bucket": None,
            "run_status": "clean",
            "repair_status": "not-needed",
            "action_taken": "no-op",
            "safety_level": "safe",
            "next_step": "Replay completed cleanly; no repair was needed.",
            "tests_run": [],
            "pr_url": None,
        }
    )

    json_text = render_report(report, "json")
    yaml_text = render_report(report, "yaml")

    assert json.loads(json_text) == report
    assert yaml.safe_load(yaml_text) == report
    assert json.loads(json_text)["run_status"] == "clean"


def test_repair_mode_skip_safe_and_preserves_working_tree(monkeypatch, capsys) -> None:
    report_before = _git_status()
    monkeypatch.setattr("hermes_cli.update_doctor.build_report", lambda **kwargs: _sample_report())

    exit_code = doctor_main(["--repair", "--format", "json"])
    out = capsys.readouterr().out
    report_after = _git_status()

    assert exit_code == 0
    assert report_before == report_after

    rendered = json.loads(out)
    assert rendered["tool"]["mode"] == "repair"
    assert rendered["repair"]["result"] == "skip-safe"
    assert rendered["repair"]["repair_status"] == "skip-safe"
    assert rendered["repair"]["repair_action"] == "no-op"
    assert rendered["repair"]["safety_level"] == "safe"
    assert rendered["repair"]["next_step"] == "Commit is already covered in fork/main or main; no repair was applied."


def test_analyze_mode_still_works(monkeypatch, capsys) -> None:
    monkeypatch.setattr("hermes_cli.update_doctor.build_report", lambda **kwargs: _sample_report())

    exit_code = doctor_main(["--analyze", "--format", "json"])
    out = capsys.readouterr().out

    assert exit_code == 1
    rendered = json.loads(out)
    assert rendered["tool"]["mode"] == "analyze"
    assert "repair" not in rendered
    assert "run_status" not in rendered



def test_run_mode_needs_integration_after_skip_safe_and_preserves_working_tree(monkeypatch, capsys) -> None:
    report_before = _git_status()
    report = _sample_report(replay_result="passed", conflict=None)
    report["replay"]["replay_continued_after_skip"] = True
    report["environment"]["behind"] = 19
    report["environment"]["origin_ahead_count"] = 19
    monkeypatch.setattr("hermes_cli.update_doctor.build_report", lambda **kwargs: report)

    exit_code = doctor_main(["--run", "--format", "json"])
    out = capsys.readouterr().out
    report_after = _git_status()

    assert exit_code == 1
    assert report_before == report_after

    rendered = json.loads(out)
    assert rendered["mode"] == "run"
    assert rendered["run_status"] == "needs-integration"
    assert rendered["result"] == "needs-integration"
    assert rendered["repair_status"] == "skip-safe"
    assert rendered["action_taken"] == "no-op"
    assert rendered["safety_level"] == "safe"
    assert rendered["risk_level"] == "low"
    assert rendered["pr_status"] == "no-pr-needed"
    assert rendered["merge_status"] == "not-needed"
    assert rendered["branch_name"] == "main"
    assert rendered["next_step"] == "Sandbox replay continued after safe skips, but origin/main is still 19 commits ahead; no integration was applied."
    assert rendered["tests_run"] == []
    assert rendered["pr_url"] is None
    assert rendered["merge_commit"] is None
    assert rendered["final_validation"]["status"] == "not-needed"
    assert rendered["origin_ahead_count"] == 19
    assert rendered["skipped_safe_commits"]
    assert rendered["replay_continued_after_skip"] is True
    assert rendered["integration_status"] == "needs-integration"
    assert rendered["material_changes_detected"] is False



def test_run_mode_clean_when_no_conflict(monkeypatch, capsys) -> None:
    report = _sample_report(replay_result="passed", conflict=None)
    report["conflict"] = None
    report["environment"]["behind"] = 0
    report["environment"]["origin_ahead_count"] = 0
    monkeypatch.setattr("hermes_cli.update_doctor.build_report", lambda **kwargs: report)

    exit_code = doctor_main(["--run", "--format", "json"])
    out = capsys.readouterr().out

    assert exit_code == 0
    rendered = json.loads(out)
    assert rendered["mode"] == "run"
    assert rendered["run_status"] == "clean"
    assert rendered["result"] == "clean"
    assert rendered["bucket"] is None
    assert rendered["repair_status"] == "not-needed"
    assert rendered["pr_status"] == "no-pr-needed"
    assert rendered["merge_status"] == "not-needed"
    assert rendered["risk_level"] == "low"
    assert rendered["final_validation"]["status"] == "not-needed"
    assert rendered["origin_ahead_count"] == 0


def test_auto_merge_requires_pr() -> None:
    with pytest.raises(SystemExit):
        doctor_main(["--run", "--auto-merge-low-risk", "--format", "json"])


def test_pr_and_auto_merge_noop_path_does_not_create_pr(monkeypatch, capsys) -> None:
    report = _sample_report()
    monkeypatch.setattr("hermes_cli.update_doctor.build_report", lambda **kwargs: report)
    monkeypatch.setattr("hermes_cli.update_doctor._create_pr", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PR should not be created")))
    monkeypatch.setattr("hermes_cli.update_doctor._merge_pr", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("merge should not run")))

    exit_code = doctor_main(["--run", "--pr", "--auto-merge-low-risk", "--format", "json"])
    out = capsys.readouterr().out

    assert exit_code == 1
    rendered = json.loads(out)
    assert rendered["run_status"] == "blocked"
    assert rendered["result"] == "blocked"
    assert rendered["integration_status"] == "blocked-high-risk"
    assert rendered["integration_risk_level"] == "high"
    assert rendered["integration_blockers"] == ["broad-upstream-sync"]
    assert rendered["pr_status"] == "not-created-risk"
    assert rendered["merge_status"] == "not-needed"
    assert rendered["pr_url"] is None
    assert rendered["origin_ahead_count"] == 99


def test_classify_pr_risk_handles_low_and_high_paths() -> None:
    docs_report = _sample_report()
    docs_report["repair"] = {
        "mode": "repair",
        "result": "changed",
        "bucket": "test-desync",
        "repair_status": "applied",
        "repair_action": "patch-applied",
        "safety_level": "safe",
        "next_step": "done",
        "changed_files": ["docs/notes.md", "docs/plans/2026-04-30-hermes-update-doctor.md"],
    }
    tests_report = _sample_report()
    tests_report["repair"] = {
        "mode": "repair",
        "result": "changed",
        "bucket": "test-desync",
        "repair_status": "applied",
        "repair_action": "patch-applied",
        "safety_level": "safe",
        "next_step": "done",
        "changed_files": ["tests/hermes_cli/test_update_doctor.py"],
    }
    runtime_report = _sample_report()
    runtime_report["repair"] = {
        "mode": "repair",
        "result": "changed",
        "bucket": "runtime-sensitive",
        "repair_status": "applied",
        "repair_action": "patch-applied",
        "safety_level": "guarded",
        "next_step": "done",
        "changed_files": ["hermes_cli/main.py"],
    }

    assert _classify_pr_risk(docs_report) == "low"
    assert _classify_pr_risk(tests_report) == "low"
    assert _classify_pr_risk(runtime_report) == "high"


def test_publish_artifacts_merges_low_risk_pr(monkeypatch) -> None:
    report = _sample_report()
    report["environment"]["behind"] = 19
    report["environment"]["origin_ahead_count"] = 19
    report["repair"] = {
        "mode": "repair",
        "result": "changed",
        "bucket": "test-desync",
        "repair_status": "applied",
        "repair_action": "patch-applied",
        "safety_level": "safe",
        "next_step": "done",
        "changed_files": ["docs/notes.md"],
    }
    monkeypatch.setattr("hermes_cli.update_doctor._ensure_fork_safe_publication", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.update_doctor._create_pr", lambda *args, **kwargs: ("https://github.com/Vottam/hermes-agent/pull/99", 99))
    monkeypatch.setattr("hermes_cli.update_doctor._review_pr_for_auto_merge", lambda *args, **kwargs: {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "baseRefName": "main", "headRefName": "update-doctor-pr-1", "url": "https://github.com/Vottam/hermes-agent/pull/99"})
    monkeypatch.setattr("hermes_cli.update_doctor._merge_pr", lambda *args, **kwargs: "merge-commit-sha")
    monkeypatch.setattr("hermes_cli.update_doctor._refresh_main_and_validate", lambda *args, **kwargs: ["./venv/bin/python -m pytest tests/hermes_cli/test_update_doctor.py -q", "hermes doctor", "hermes --version"])

    published = _publish_run_artifacts(report, root=REPO_ROOT, request_pr=True, request_auto_merge_low_risk=True)

    assert published["pr_status"] == "created"
    assert published["merge_status"] == "merged"
    assert published["merge_commit"] == "merge-commit-sha"
    assert published["pr_url"] == "https://github.com/Vottam/hermes-agent/pull/99"
    assert published["final_validation"]["status"] == "passed"
    assert published["final_validation"]["checks"]
    assert published["action_taken"] == "pr-created-and-merged"


def test_render_report_contains_pr_and_merge_fields() -> None:
    report = _sample_report()
    report.update(
        {
            "mode": "run",
            "result": "skip-safe",
            "bucket": "already-covered-in-fork-main",
            "run_status": "completed",
            "repair_status": "skip-safe",
            "action_taken": "no-op",
            "safety_level": "safe",
            "risk_level": "low",
            "pr_status": "no-pr-needed",
            "pr_url": None,
            "merge_status": "not-needed",
            "merge_commit": None,
            "branch_name": "main",
            "tests_run": [],
            "final_validation": {"status": "not-needed", "checks": []},
            "next_step": "Commit is already covered in fork/main or main; no repair was applied.",
        }
    )

    json_text = render_report(report, "json")
    rendered = json.loads(json_text)

    assert rendered["pr_status"] == "no-pr-needed"
    assert rendered["merge_status"] == "not-needed"
    assert rendered["final_validation"]["status"] == "not-needed"

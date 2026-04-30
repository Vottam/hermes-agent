from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from hermes_cli.update_doctor import ConflictBucket, classify_conflict, main as doctor_main, render_report


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



def test_run_mode_completed_skip_safe_and_preserves_working_tree(monkeypatch, capsys) -> None:
    report_before = _git_status()
    monkeypatch.setattr("hermes_cli.update_doctor.build_report", lambda **kwargs: _sample_report())

    exit_code = doctor_main(["--run", "--format", "json"])
    out = capsys.readouterr().out
    report_after = _git_status()

    assert exit_code == 0
    assert report_before == report_after

    rendered = json.loads(out)
    assert rendered["mode"] == "run"
    assert rendered["run_status"] == "completed"
    assert rendered["repair_status"] == "skip-safe"
    assert rendered["action_taken"] == "no-op"
    assert rendered["safety_level"] == "safe"
    assert rendered["next_step"] == "Commit is already covered in fork/main or main; no repair was applied."
    assert rendered["tests_run"] == []
    assert rendered["pr_url"] is None



def test_run_mode_clean_when_no_conflict(monkeypatch, capsys) -> None:
    report = _sample_report(replay_result="passed", conflict=None)
    report["conflict"] = None
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

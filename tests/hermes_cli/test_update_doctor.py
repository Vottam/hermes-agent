from __future__ import annotations

import json

import yaml

from hermes_cli.update_doctor import ConflictBucket, classify_conflict, render_report


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
            "result": "conflict",
        },
        "conflict": {
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

    json_text = render_report(report, "json")
    yaml_text = render_report(report, "yaml")

    assert json.loads(json_text) == report
    assert yaml.safe_load(yaml_text) == report
    assert json.loads(json_text)["replay"]["skipped_duplicate_commits"][0]["bucket"] == "patch-id-duplicate"

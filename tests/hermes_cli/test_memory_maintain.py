from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def maintain_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    memories = hermes_home / "memories"
    skills_root = hermes_home / "skills"
    memories.mkdir(parents=True)
    skills_root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    (hermes_home / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 100\n  user_char_limit: 100\n",
        encoding="utf-8",
    )
    (memories / "MEMORY.md").write_text("memory note\n", encoding="utf-8")
    (memories / "USER.md").write_text("user note\n", encoding="utf-8")

    for skill_name in ("alpha", "beta"):
        skill_dir = skills_root / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\n---\n", encoding="utf-8")

    def ts(days_ago: int) -> str:
        return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")

    memory_db = hermes_home / "memory_store.db"
    conn = sqlite3.connect(memory_db)
    conn.executescript(
        """
        CREATE TABLE facts (
            fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT 'general',
            tags TEXT DEFAULT '',
            trust_score REAL DEFAULT 0.5,
            retrieval_count INTEGER DEFAULT 0,
            helpful_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hrr_vector BLOB
        );
        CREATE TABLE entities (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT DEFAULT 'unknown',
            aliases TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE fact_entities (
            fact_id INTEGER,
            entity_id INTEGER,
            PRIMARY KEY (fact_id, entity_id)
        );
        """
    )
    conn.executemany(
        "INSERT INTO facts(content, category, tags, trust_score, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("ordinary preference", "general", "", 0.9, ts(0), ts(0)),
            ("api token sk-abcdefghijklmnopqrstuvwxyz1234", "general", "", 0.5, ts(0), ts(0)),
            ("current service endpoint version", "general", "provider,config,active", 0.8, ts(45), ts(45)),
            ("low confidence note", "general", "", 0.2, ts(1), ts(1)),
            ("entityless operational note", "general", "current,path,enabled", 0.6, ts(60), ts(60)),
        ],
    )
    conn.executemany(
        "INSERT INTO entities(name, entity_type, aliases) VALUES (?, ?, ?)",
        [("DocEntity", "project", ""), ("UserEntity", "person", "")],
    )
    conn.executemany(
        "INSERT INTO fact_entities(fact_id, entity_id) VALUES (?, ?)",
        [(1, 1), (2, 1), (3, 2), (4, 2)],
    )
    conn.commit()
    conn.close()

    state_db = hermes_home / "state.db"
    conn = sqlite3.connect(state_db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions(session_id, source, title) VALUES (?, ?, ?)",
        [("s1", "cli", "one")],
    )
    conn.executemany(
        "INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
        [("s1", "user", "hello"), ("s1", "assistant", "hi")],
    )
    conn.commit()
    conn.close()

    return hermes_home


def test_memory_maintain_builds_proposal_matrix_from_doctor_report(maintain_env):
    from hermes_cli.memory_maintain import build_memory_maintain_report

    report = build_memory_maintain_report()

    assert report["mode"] == "dry-run"
    assert report["summary"]["memory"]["status"] == "ok"
    assert report["summary"]["fact_store"]["sensitive_facts"] == 1
    assert report["summary"]["fact_store"]["stale_mutable_facts"] == 2
    assert report["summary"]["fact_store"]["facts_without_entity"] == 1
    assert report["summary"]["fact_store"]["low_trust_facts"] == 1
    actions = report["actions"]
    assert any(a["proposed_action"] == "REDACT_ON_OUTPUT_ONLY" for a in actions)
    assert any(a["proposed_action"] == "REVALIDATE" for a in actions)
    assert any(a["scope"] == "session_search" and a["proposed_action"] == "KEEP_IN_SESSION_SEARCH_ONLY" for a in actions)


def test_memory_maintain_redacts_sensitive_content(maintain_env, capsys):
    from hermes_cli.memory_maintain import cmd_memory_maintain

    report = cmd_memory_maintain(argparse.Namespace(dry_run=True, json=False))
    out = capsys.readouterr().out

    assert report["overall_status"] in {"ok", "warning", "critical"}
    assert "api token" not in out.lower()
    assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in out
    assert "ordinary preference" not in out
    assert "REDACT_ON_OUTPUT_ONLY" in out
    assert "no changes applied" in out.lower()


def test_memory_maintain_marks_stale_facts_as_revalidate(maintain_env):
    from hermes_cli.memory_maintain import build_memory_maintain_report

    report = build_memory_maintain_report()
    revalidate_targets = {
        action["target_id"]
        for action in report["actions"]
        if action["proposed_action"] == "REVALIDATE"
    }

    assert any(str(target).startswith("fact:") for target in revalidate_targets)
    assert any(action["reason"] == "stale_mutable_fact" for action in report["actions"])


def test_memory_maintain_marks_sensitive_facts_as_redact_only(maintain_env):
    from hermes_cli.memory_maintain import build_memory_maintain_report

    report = build_memory_maintain_report()
    redact_actions = [a for a in report["actions"] if a["proposed_action"] == "REDACT_ON_OUTPUT_ONLY"]

    assert redact_actions
    assert redact_actions[0]["scope"] == "fact_store"
    assert redact_actions[0]["target_type"] == "fact"
    assert redact_actions[0]["reason"] == "sensitive_fact"


def test_memory_maintain_keeps_session_search_only(maintain_env):
    from hermes_cli.memory_maintain import build_memory_maintain_report

    report = build_memory_maintain_report()
    actions = [a for a in report["actions"] if a["scope"] == "session_search"]

    assert actions
    assert all(a["proposed_action"] == "KEEP_IN_SESSION_SEARCH_ONLY" for a in actions)


def test_memory_maintain_json_shape(maintain_env, capsys):
    from hermes_cli.memory_maintain import cmd_memory_maintain

    cmd_memory_maintain(argparse.Namespace(dry_run=True, json=True))
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "dry-run"
    assert "summary" in payload
    assert isinstance(payload["actions"], list)
    assert isinstance(payload["warnings"], list)
    assert all("proposed_action" in action for action in payload["actions"])


def test_memory_maintain_cli_dispatch(maintain_env, monkeypatch, capsys):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", "memory", "maintain", "--dry-run"])
    main_mod.main()
    out = capsys.readouterr().out

    assert "Hermes memory maintain (dry-run)" in out
    assert "best next action" in out.lower()

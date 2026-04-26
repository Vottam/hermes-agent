from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

from hermes_cli.memory_doctor import build_memory_doctor_report

_ALLOWED_ACTIONS = {
    "KEEP",
    "REDACT_ON_OUTPUT_ONLY",
    "REVALIDATE",
    "MERGE_CANDIDATE",
    "DELETE_CANDIDATE",
    "MOVE_TO_SKILL_CANDIDATE",
    "KEEP_IN_SESSION_SEARCH_ONLY",
    "NOOP",
}


def _action(
    *,
    scope: str,
    target_type: str,
    target_id: str | int,
    proposed_action: str,
    reason: str,
    severity: str,
    confidence: str,
    age_days: int | None = None,
    keywords: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if proposed_action not in _ALLOWED_ACTIONS:
        raise ValueError(f"unsupported proposed action: {proposed_action}")
    item: dict[str, Any] = {
        "scope": scope,
        "target_type": target_type,
        "target_id": target_id,
        "proposed_action": proposed_action,
        "reason": reason,
        "severity": severity,
        "confidence": confidence,
    }
    if age_days is not None:
        item["age_days"] = age_days
    if keywords:
        item["keywords"] = keywords
    if evidence:
        item["evidence"] = evidence
    return item


def _severity_for_status(status: str) -> str:
    return {"ok": "low", "warning": "medium", "critical": "high"}.get(status, "low")


def _confidence_for_status(status: str) -> str:
    return {"ok": "high", "warning": "medium", "critical": "medium"}.get(status, "low")


def build_memory_maintain_report() -> dict[str, Any]:
    doctor = build_memory_doctor_report()
    fact_store = doctor.get("fact_store", {})
    memory = doctor.get("memory", {})
    user = doctor.get("user", {})
    session = doctor.get("session_search", {})
    skills = doctor.get("skills", {})

    actions: list[dict[str, Any]] = []

    def add(action: dict[str, Any]) -> None:
        actions.append(action)

    memory_status = str(memory.get("status", "ok"))
    add(
        _action(
            scope="memory",
            target_type="memory_file",
            target_id="MEMORY.md",
            proposed_action="NOOP" if memory_status == "ok" else "REVALIDATE",
            reason="below_threshold" if memory_status == "ok" else "review_warning",
            severity=_severity_for_status(memory_status),
            confidence=_confidence_for_status(memory_status),
            evidence={"status": memory_status, "percent_used": memory.get("percent_used", 0.0)},
        )
    )

    user_status = str(user.get("status", "ok"))
    add(
        _action(
            scope="user",
            target_type="memory_file",
            target_id="USER.md",
            proposed_action="NOOP" if user_status == "ok" else "REVALIDATE",
            reason="below_threshold" if user_status == "ok" else "review_warning",
            severity=_severity_for_status(user_status),
            confidence=_confidence_for_status(user_status),
            evidence={"status": user_status, "percent_used": user.get("percent_used", 0.0)},
        )
    )

    for item in fact_store.get("sensitive_facts", []):
        add(
            _action(
                scope="fact_store",
                target_type="fact",
                target_id=f"fact:{item.get('fact_id')}",
                proposed_action="REDACT_ON_OUTPUT_ONLY",
                reason="sensitive_fact",
                severity="high",
                confidence="high",
                keywords=[str(item.get("type", ""))],
                evidence={"category": item.get("category"), "trust_score": item.get("trust_score")},
            )
        )

    for bucket in fact_store.get("stale_mutable_facts", {}).get("buckets", []):
        label = str(bucket.get("bucket", ""))
        for item in bucket.get("items", []):
            add(
                _action(
                    scope="fact_store",
                    target_type="fact",
                    target_id=f"fact:{item.get('fact_id')}",
                    proposed_action="REVALIDATE",
                    reason="stale_mutable_fact",
                    severity="medium",
                    confidence="medium",
                    age_days=item.get("age_days"),
                    keywords=list(item.get("keywords", [])),
                    evidence={"bucket": label, "fields": item.get("fields", [])},
                )
            )

    if fact_store.get("facts_without_entity", 0):
        add(
            _action(
                scope="fact_store",
                target_type="fact_group",
                target_id="facts_without_entity",
                proposed_action="REVALIDATE",
                reason="missing_entity_links",
                severity="medium",
                confidence="medium",
                evidence={"count": fact_store.get("facts_without_entity", 0)},
            )
        )

    if fact_store.get("low_trust_facts", {}).get("count", 0):
        for item in fact_store.get("low_trust_facts", {}).get("items", []):
            add(
                _action(
                    scope="fact_store",
                    target_type="fact",
                    target_id=f"fact:{item.get('fact_id')}",
                    proposed_action="REVALIDATE",
                    reason="low_trust_fact",
                    severity="medium",
                    confidence="medium",
                    evidence={"trust_score": item.get("trust_score")},
                )
            )

    add(
        _action(
            scope="session_search",
            target_type="session_store",
            target_id="session_search",
            proposed_action="KEEP_IN_SESSION_SEARCH_ONLY",
            reason="historical_record",
            severity="low",
            confidence="high",
            evidence={"sessions": session.get("sessions", 0), "messages": session.get("messages", 0)},
        )
    )

    add(
        _action(
            scope="skills",
            target_type="skill_store",
            target_id="skills",
            proposed_action="NOOP",
            reason="not_analyzed_yet",
            severity="low",
            confidence="high",
            evidence={"skill_docs": skills.get("skill_docs", 0)},
        )
    )

    counts = Counter(action["proposed_action"] for action in actions)
    overall_status = doctor.get("overall_status", "ok")
    if any(action["severity"] == "high" for action in actions):
        overall_status = "critical"
    elif overall_status == "ok" and any(action["severity"] == "medium" for action in actions):
        overall_status = "warning"

    summary = {
        "memory": {"status": memory_status, "percent_used": memory.get("percent_used", 0.0)},
        "user": {"status": user_status, "percent_used": user.get("percent_used", 0.0)},
        "fact_store": {
            "sensitive_facts": len(fact_store.get("sensitive_facts", [])),
            "stale_mutable_facts": fact_store.get("stale_mutable_facts", {}).get("total", 0),
            "facts_without_entity": fact_store.get("facts_without_entity", 0),
            "low_trust_facts": fact_store.get("low_trust_facts", {}).get("count", 0),
            "top_entities": len(fact_store.get("top_entities", {}).get("items", [])),
        },
        "session_search": {
            "sessions": session.get("sessions", 0),
            "messages": session.get("messages", 0),
        },
        "skills": {"skill_docs": skills.get("skill_docs", 0)},
        "proposal_counts": dict(counts),
    }

    warnings = ["dry-run: no changes applied"]
    if fact_store.get("sensitive_facts"):
        warnings.append("sensitive facts are redacted in output only")

    return {
        "mode": "dry-run",
        "overall_status": overall_status,
        "summary": summary,
        "actions": actions,
        "warnings": warnings,
        "doctor": doctor,
    }


def format_memory_maintain_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("Hermes memory maintain (dry-run)")
    lines.append(f"Overall status: {report.get('overall_status', 'unknown')}")
    lines.append("")

    summary = report.get("summary", {})
    lines.append("Summary")
    memory = summary.get("memory", {})
    user = summary.get("user", {})
    fact_store = summary.get("fact_store", {})
    session = summary.get("session_search", {})
    skills = summary.get("skills", {})
    lines.append(f"  MEMORY.md: {memory.get('status', 'unknown')} ({memory.get('percent_used', 0.0):.2f}%)")
    lines.append(f"  USER.md: {user.get('status', 'unknown')} ({user.get('percent_used', 0.0):.2f}%)")
    lines.append(
        "  fact_store: sensitive={sensitive} stale={stale} missing_entity={missing} low_trust={low_trust} top_entities={top_entities}".format(
            sensitive=fact_store.get("sensitive_facts", 0),
            stale=fact_store.get("stale_mutable_facts", 0),
            missing=fact_store.get("facts_without_entity", 0),
            low_trust=fact_store.get("low_trust_facts", 0),
            top_entities=fact_store.get("top_entities", 0),
        )
    )
    lines.append(
        f"  session_search: sessions={session.get('sessions', 0)} messages={session.get('messages', 0)}"
    )
    lines.append(f"  skills: skill_docs={skills.get('skill_docs', 0)}")
    lines.append("")

    lines.append("Actions")
    for action in report.get("actions", []):
        extra = []
        if action.get("age_days") is not None:
            extra.append(f"age≈{action['age_days']}d")
        if action.get("keywords"):
            extra.append(f"keywords={','.join(action['keywords'])}")
        evidence = action.get("evidence")
        if evidence:
            extra.append(f"evidence={evidence}")
        extra_text = f" [{' ; '.join(extra)}]" if extra else ""
        lines.append(
            f"  - scope={action.get('scope')} target={action.get('target_id')} action={action.get('proposed_action')} reason={action.get('reason')} severity={action.get('severity')} confidence={action.get('confidence')}{extra_text}"
        )
    lines.append("")

    lines.append("Best next action")
    best = next((a for a in report.get("actions", []) if a.get("severity") == "high"), None)
    if best is None:
        best = next((a for a in report.get("actions", []) if a.get("proposed_action") != "NOOP"), None)
    if best:
        lines.append(
            f"  Review {best.get('scope')} proposal for {best.get('target_id')} ({best.get('proposed_action')})"
        )
    else:
        lines.append("  No changes needed")
    lines.append("")

    lines.append("No changes applied.")
    return "\n".join(lines)


def cmd_memory_maintain(args: argparse.Namespace) -> dict[str, Any]:
    report = build_memory_maintain_report()
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_memory_maintain_report(report))
    return report

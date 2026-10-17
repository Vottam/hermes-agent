#!/usr/bin/env python3
"""Canonical deterministic staging-environment builder (CUSTOM_14).

Single source of truth for the environment that Hindsight staging is launched
with. Mirrored verbatim into /usr/local/bin/hindsight-update-safe.sh by
reconcile.py; the shell copy is the runtime authority, this file is the
reviewed source of the same code.

Policy: EXPLICIT_WHITELIST_FAIL_CLOSED
  * one canonical source: the daemon's own systemd EnvironmentFile
  * parsed as a systemd-style env file, NEVER sourced
  * explicit whitelist, staging-owned keys always overridden
  * missing/empty required key fails BEFORE any provider contact
  * values are never returned to a log; only names/presence
"""

ALLOWLIST_PREFIX = "HINDSIGHT_API_"

# Keys the staging run owns; never taken from the canonical source.
STAGING_OWNED_KEYS = (
    "HINDSIGHT_API_DATABASE_URL",
    "HINDSIGHT_API_PORT",
    "HINDSIGHT_API_LOG_LEVEL",
)

# Keys whose absence would silently select a code default (openai/gpt-4o-mini),
# so their absence must stop the run instead of reaching the provider.
REQUIRED_LLM_KEYS = (
    "HINDSIGHT_API_LLM_API_KEY",
    "HINDSIGHT_API_LLM_PROVIDER",
    "HINDSIGHT_API_LLM_MODEL",
    "HINDSIGHT_API_LLM_1_PROVIDER",
    "HINDSIGHT_API_LLM_1_MODEL",
    "HINDSIGHT_API_LLM_2_PROVIDER",
    "HINDSIGHT_API_LLM_2_MODEL",
    "HINDSIGHT_API_LLM_2_API_KEY",
)


class EnvFileUnreadable(RuntimeError):
    """The canonical env file is missing or unreadable."""


def parse_env_file(path, prefix=ALLOWLIST_PREFIX):
    """Return {name: value} for whitelisted vars in a systemd-style env file.

    Never evaluates the file as shell. A single layer of matching surrounding
    quotes is stripped (systemd semantics) so JSON values survive intact; a
    value containing a newline is rejected rather than silently truncated.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise EnvFileUnreadable(str(exc)) from exc

    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name.startswith(prefix):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if "\n" in value or "\r" in value:
            continue
        out[name] = value
    return out


def build_staging_env(path, staging_db, staging_port):
    """Build the staging environment.

    Returns (assignments, report). ``assignments`` is empty when the precheck
    fails; the caller must then abort BEFORE launching anything, which is what
    keeps PROVIDER_CALLS at 0 on an incomplete configuration.
    """
    try:
        source = parse_env_file(path)
    except EnvFileUnreadable:
        return {}, {
            "STAGING_ENV_PRECHECK": "FAIL",
            "STAGING_ENV_SOURCE": "UNREADABLE",
            "MISSING_REQUIRED_ENV": "ENV_FILE",
        }

    carried = {k: v for k, v in source.items() if k not in STAGING_OWNED_KEYS}
    missing = [k for k in REQUIRED_LLM_KEYS if not carried.get(k)]

    report = {
        "STAGING_ENV_SOURCE": path,
        "STAGING_ENV_SOURCE_MODE": "EXPLICIT_WHITELIST_FAIL_CLOSED",
        "STAGING_ENV_WHITELIST_PREFIX": ALLOWLIST_PREFIX,
        "STAGING_ENV_CARRIED_KEYS": str(len(carried)),
        "STAGING_ENV_REQUIRED_KEYS": str(len(REQUIRED_LLM_KEYS)),
        "STAGING_ENV_INHERITED_FROM_CALLER": "no",
    }

    if missing:
        report["STAGING_ENV_PRECHECK"] = "FAIL"
        for name in missing:
            report["MISSING_REQUIRED_ENV:%s" % name] = "yes"
        return {}, report

    report["STAGING_ENV_PRECHECK"] = "PASS"
    report["STAGING_ENV_DEFAULT_PROVIDER_FALLBACK"] = "BLOCKED"

    assignments = {
        "HINDSIGHT_API_DATABASE_URL": "postgresql://hindsight@127.0.0.1:5432/%s" % staging_db,
        "HINDSIGHT_API_PORT": str(staging_port),
        "HINDSIGHT_API_LOG_LEVEL": "warning",
    }
    assignments.update(carried)
    return assignments, report


def launch_key_order(assignments):
    """Explicit key order for `env -i`, so nothing is inherited implicitly."""
    preferred = [
        "HINDSIGHT_API_DATABASE_URL",
        "HINDSIGHT_API_PORT",
        "HINDSIGHT_API_LOG_LEVEL",
        "HINDSIGHT_API_LLM_PROVIDER",
        "HINDSIGHT_API_LLM_MODEL",
        "HINDSIGHT_API_LLM_STRATEGY",
        "HINDSIGHT_API_LLM_API_KEY",
        "HINDSIGHT_API_LLM_1_PROVIDER",
        "HINDSIGHT_API_LLM_1_MODEL",
        "HINDSIGHT_API_LLM_2_PROVIDER",
        "HINDSIGHT_API_LLM_2_MODEL",
        "HINDSIGHT_API_LLM_2_API_KEY",
    ]
    ordered = [k for k in preferred if k in assignments]
    ordered += sorted(k for k in assignments if k not in preferred)
    return ordered

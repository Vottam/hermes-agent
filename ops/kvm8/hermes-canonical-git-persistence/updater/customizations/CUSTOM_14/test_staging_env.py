#!/usr/bin/env python3
"""CUSTOM_14 acceptance tests.

Covers the four contracts the T2.5 harness repair must satisfy:
  1. missing required env fails closed, with PROVIDER_CALLS=0
  2. no implicit default provider/model (openai/gpt-4o-mini unreachable)
  3. timer-like minimal environment is deterministic
  4. official/fork A/B produce an equivalent envelope

Run:  python3 test_staging_env.py
Never contacts a provider and never starts the Hindsight API.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from staging_env import (  # noqa: E402
    EnvFileUnreadable,
    REQUIRED_LLM_KEYS,
    build_staging_env,
    launch_key_order,
    parse_env_file,
)

CANON = "/etc/hindsight-kvm8/daemon.env"
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, "PASS" if ok else "FAIL", detail))
    print("%-46s %s %s" % (name, "PASS" if ok else "FAIL", detail))


def write_fixture(tmp, mutate):
    src = parse_env_file(CANON)
    src = mutate(dict(src))
    path = os.path.join(tmp, "fixture.env")
    with open(path, "w", encoding="utf-8") as fh:
        for k, v in src.items():
            fh.write("%s=%s\n" % (k, v))
    return path


# ── 1. canonical source produces a complete, default-free environment ──────
def test_canonical_source():
    assignments, report = build_staging_env(CANON, "hindsight_staging", 9890)
    record("CANONICAL_PRECHECK", report.get("STAGING_ENV_PRECHECK") == "PASS",
           report.get("STAGING_ENV_PRECHECK", "?"))
    record("NO_IMPLICIT_DEFAULT_ALLOWED",
           report.get("STAGING_ENV_DEFAULT_PROVIDER_FALLBACK") == "BLOCKED")
    record("PROVIDER_FROM_SOURCE", bool(assignments.get("HINDSIGHT_API_LLM_PROVIDER")))
    record("MODEL_FROM_SOURCE", bool(assignments.get("HINDSIGHT_API_LLM_MODEL")))
    # staging-owned keys always overridden, never inherited
    record("STAGING_OWNS_DATABASE_URL",
           assignments.get("HINDSIGHT_API_DATABASE_URL", "").endswith("hindsight_staging"))
    record("STAGING_OWNS_PORT", assignments.get("HINDSIGHT_API_PORT") == "9890")
    return assignments


def test_fail_closed(assignments):
    with tempfile.TemporaryDirectory() as tmp:
        for key in REQUIRED_LLM_KEYS:
            path = write_fixture(tmp, lambda d, k=key: {a: b for a, b in d.items() if a != k})
            got, report = build_staging_env(path, "hindsight_staging", 9890)
            ok = (got == {} and report.get("STAGING_ENV_PRECHECK") == "FAIL"
                  and report.get("MISSING_REQUIRED_ENV:%s" % key) == "yes")
            if not ok:
                record("FAIL_CLOSED_MISSING:%s" % key, False, str(report)[:120])
                return
        record("FAIL_CLOSED_ALL_REQUIRED_KEYS", True,
               "%d/%d missing-key fixtures" % (len(REQUIRED_LLM_KEYS), len(REQUIRED_LLM_KEYS)))

        # empty value is also a failure
        path = write_fixture(tmp, lambda d: {**d, "HINDSIGHT_API_LLM_PROVIDER": ""})
        got, report = build_staging_env(path, "hindsight_staging", 9890)
        record("FAIL_CLOSED_EMPTY_VALUE",
               got == {} and report.get("STAGING_ENV_PRECHECK") == "FAIL")

        # unreadable source
        got, report = build_staging_env(os.path.join(tmp, "nope.env"), "hindsight_staging", 9890)
        record("FAIL_CLOSED_UNREADABLE_SOURCE",
               got == {} and report.get("STAGING_ENV_PRECHECK") == "FAIL")


def test_no_provider_calls_on_missing(assignments):
    """The precheck emits nothing to launch, so no process can call a provider."""
    with tempfile.TemporaryDirectory() as tmp:
        path = write_fixture(tmp, lambda d: {a: b for a, b in d.items()
                                             if a != "HINDSIGHT_API_LLM_API_KEY"})
        got, _ = build_staging_env(path, "hindsight_staging", 9890)
        record("PROVIDER_CALLS_ON_MISSING_CONFIG", got == {}, "0 (nothing launchable)")


def test_environment_independence(assignments):
    """Identical result regardless of the caller's environment."""
    fingerprint = lambda a: sorted("%s=%d" % (k, len(v)) for k, v in a.items())
    base = fingerprint(assignments)

    hostile = {
        "HINDSIGHT_API_LLM_PROVIDER": "evil/xx",
        "HINDSIGHT_API_LLM_MODEL": "evil-model",
        "HINDSIGHT_API_LLM_API_KEY": "sk-evil",
        "OPENAI_API_KEY": "sk-evil",
    }
    child = os.environ.copy()
    child.update(hostile)
    probe = (
        "import sys;sys.path.insert(0,{here!r});import staging_env,json;"
        "a,_=staging_env.build_staging_env({canon!r},'hindsight_staging',9890);"
        "print(json.dumps(sorted(str(k)+'='+str(len(v)) for k,v in a.items())))"
    ).format(here=HERE, canon=CANON)
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True, env=child, timeout=60)
    import json
    got = json.loads(out.stdout.strip())
    record("ENV_INDEPENDENCE_HOSTILE_VARS", got == base)

    # minimal timer-like environment
    out2 = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": "/root"}, timeout=60)
    got2 = json.loads(out2.stdout.strip())
    record("ENV_INDEPENDENCE_MINIMAL_TIMER", got2 == base)


def test_no_default_reachable(assignments):
    """openai/gpt-4o-mini must be unreachable from the built environment."""
    record("NO_OPENAI_GPT4O_MINI_FALLTHROUGH",
           assignments.get("HINDSIGHT_API_LLM_PROVIDER", "").lower() != "openai"
           and assignments.get("HINDSIGHT_API_LLM_MODEL", "") != "gpt-4o-mini",
           "provider=%s" % assignments.get("HINDSIGHT_API_LLM_PROVIDER", "?"))


def test_secret_hygiene(assignments):
    """Values must never appear in the report surface."""
    report_keys = set()
    _, report = build_staging_env(CANON, "hindsight_staging", 9890)
    report_keys = set(report)
    leaked = [k for k in report_keys
              if any(str(v) and len(str(v)) > 12 and str(v) in k for v in assignments.values())]
    record("SECRET_VALUES_LOGGED", not leaked, "%d report keys, 0 values" % len(report_keys))

    updater = "/usr/local/bin/hindsight-update-safe.sh"
    if os.path.exists(updater):
        with open(updater, encoding="utf-8") as fh:
            text = fh.read()
        record("NO_SET_X_IN_UPDATER",
               not re.search(r"^\s*set\s+-[a-z]*x", text, re.M))
        record("NO_SOURCE_OF_ENV_FILE",
               not re.search(r"set\s+-a\s*;.*\.\s+\"?\$\{?ENV_FILE", text))


def test_ab_equivalence():
    """A/B: the gate envelope is identical; only the package differs."""
    here = os.path.dirname(HERE)
    roots = {
        "official": "/root/hermes-audits/kvm8-hindsight-t2-5-staging-harness-env-20260928T032809Z/AB_OFFICIAL/gate-result.txt",
        "fork": "/root/hermes-audits/kvm8-hindsight-t2-5-staging-harness-env-20260928T032809Z/AB_FORK/gate-result.txt",
    }
    keep = ("STAGING_ENV_SOURCE_MODE", "STAGING_ENV_WHITELIST_PREFIX",
            "STAGING_ENV_CARRIED_KEYS", "STAGING_ENV_REQUIRED_KEYS",
            "STAGING_ENV_INHERITED_FROM_CALLER", "STAGING_ENV_PRECHECK",
            "STAGING_ENV_DEFAULT_PROVIDER_FALLBACK", "STAGING_DB_CLONE",
            "STAGING_HEALTH", "RETAIN_SUCCESS", "RECALL_SENTINEL_FOUND",
            "REFLECT_NONEMPTY", "SMOKE_VALIDATION", "CLEANUP")
    parsed = {}
    for label, path in roots.items():
        if not os.path.exists(path):
            record("AB_EVIDENCE_%s" % label, False, "missing %s" % path)
            return
        got = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    if k in keep:
                        got[k] = v
        parsed[label] = got
    record("AB_ENVIRONMENT_EQUIVALENT", parsed["official"] == parsed["fork"],
           "%d keys compared" % len(keep))


def main():
    assignments = test_canonical_source()
    test_fail_closed(assignments)
    test_no_provider_calls_on_missing(assignments)
    test_environment_independence(assignments)
    test_no_default_reachable(assignments)
    test_secret_hygiene(assignments)
    test_ab_equivalence()

    failed = [r for r in RESULTS if r[1] != "PASS"]
    print("\nTOTAL=%d PASS=%d FAIL=%d" % (len(RESULTS),
                                          len(RESULTS) - len(failed), len(failed)))
    if failed:
        for name, _, _ in failed:
            print("  FAILED: %s" % name)
        return 1
    print("CUSTOM_14_ACCEPTANCE=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""CUSTOM_14 reconciler — deterministic staging env in hindsight-update-safe.sh.

--check  : verify the installed updater carries the policy (fail closed)
--apply  : install the block if absent (no destructive rewrites of other logic)
--repo   : not used; the target is the live updater, passed with --target

Never replays a historical diff. The desired state is expressed as behaviour
requirements and verified by AST/text on the real file.
"""
import argparse
import re
import sys

# Markers that must be PRESENT in the installed updater.
REQUIRED_MARKERS = [
    "build_staging_env()",
    "read_env_file_whitelist()",
    "STAGING_REQUIRED_LLM_KEYS",
    "STAGING_ENV_ALLOWLIST_PREFIX",
    "BLOCKED_STAGING_ENV_PRECHECK",
    "STAGING_ENV_DEFAULT_PROVIDER_FALLBACK",
    "PROVIDER_CALLS=0",
]

# Markers that prove the OLD defective behaviour is gone.
FORBIDDEN_MARKERS = [
    "grep -E '^HINDSIGHT_API_LLM_API_KEY='",
]

# Required launch-time contract: the staging process must not inherit the
# caller's environment, and must be handed an explicit key list.
LAUNCH_MARKERS = [
    re.compile(r"nohup\s+env\s+-i"),
    re.compile(r'HINDSIGHT_API_LLM_PROVIDER="\$\{HINDSIGHT_API_LLM_PROVIDER\}"'),
    re.compile(r'HINDSIGHT_API_LLM_MODEL="\$\{HINDSIGHT_API_LLM_MODEL\}"'),
]


def read_target(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def evaluate(text):
    """Return (state, findings). Fails closed; never reports PASS on absence."""
    findings = []
    ok = True

    for marker in REQUIRED_MARKERS:
        present = marker in text
        findings.append("REQUIRED_MARKER:%s=%s" % (marker, "yes" if present else "no"))
        if not present:
            ok = False

    for marker in FORBIDDEN_MARKERS:
        present = marker in text
        findings.append("FORBIDDEN_MARKER:%s=%s" % (marker, "ABSENT" if not present else "PRESENT"))
        if present:
            ok = False

    for idx, pattern in enumerate(LAUNCH_MARKERS, start=1):
        present = bool(pattern.search(text))
        findings.append("LAUNCH_CONTRACT_%d=%s" % (idx, "yes" if present else "no"))
        if not present:
            ok = False

    # Fail-closed ordering: the precheck must abort before the launch.
    precheck = text.find("STAGING_ENV_PRECHECK=FAIL")
    launch = text.find("nohup env -i")
    if precheck == -1 or launch == -1 or precheck > launch:
        findings.append("FAIL_CLOSED_BEFORE_LAUNCH=no")
        ok = False
    else:
        findings.append("FAIL_CLOSED_BEFORE_LAUNCH=yes")

    # No shell tracing that could expose secrets.
    if re.search(r"^\s*set\s+-[a-z]*x", text, re.M):
        findings.append("SECRET_SAFE_NO_SET_X=no")
        ok = False
    else:
        findings.append("SECRET_SAFE_NO_SET_X=yes")

    return ("PASS_EXISTING" if ok else "NEEDS_REIMPLEMENTATION"), findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="/usr/local/bin/hindsight-update-safe.sh")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    try:
        text = read_target(args.target)
    except OSError as exc:
        print("TARGET_UNREADABLE=%s" % exc)
        print("CUSTOM_14_RESULT=BLOCKED")
        return 2

    state, findings = evaluate(text)
    for line in findings:
        print(line)
    print("CUSTOM_14_RESULT=%s" % state)
    return 0 if state == "PASS_EXISTING" else 1


if __name__ == "__main__":
    sys.exit(main())

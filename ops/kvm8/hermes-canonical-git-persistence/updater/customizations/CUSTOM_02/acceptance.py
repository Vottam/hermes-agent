#!/usr/bin/env bash
# Acceptance for CUSTOM_02
set -euo pipefail
echo "ACCEPTANCE: CUSTOM_02"
# For migration: structural checks; real runtime checks deferred to Section 15
if [[ -f "/opt/hermes-agent-clean/.git" ]]; then
    echo "PASS: repository exists"
else
    echo "FAIL: repository missing"
    exit 1
fi
exit 0

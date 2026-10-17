#!/usr/bin/env bash
# Acceptance for CUSTOM_01
set -euo pipefail
echo "ACCEPTANCE: CUSTOM_01"
# Intended semantics (from this file itself): the Hermes runtime checkout must
# EXIST and be a real, usable git work tree. The previous check was
# `[[ -f "/opt/hermes-agent-clean/.git" ]]`, a file-type assumption: in a normal
# clone `.git` is a DIRECTORY, so the test failed on a healthy repository.
# Derive repository state through git itself so both layouts (directory `.git`
# and a `gitdir:` file) are accepted and a real repo is still required.
REPO="${1:-/opt/hermes-agent-clean}"

if [[ ! -e "$REPO" ]]; then
    echo "FAIL: repository path missing ($REPO)"
    exit 1
fi

# Git-semantic check: succeeds only inside a real work tree, whatever the
# on-disk shape of .git is.
if ! GIT_DIR_PATH="$(git -C "$REPO" rev-parse --absolute-git-dir 2>/dev/null)" \
   || [[ -z "$GIT_DIR_PATH" || ! -d "$GIT_DIR_PATH" ]]; then
    echo "FAIL: not a usable git repository ($REPO)"
    exit 1
fi

# A real work tree (not a bare repo, not a .git-only dir).
if [[ "$(git -C "$REPO" rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]]; then
    echo "FAIL: not inside a git work tree ($REPO)"
    exit 1
fi

# Must expose a readable commit (proves object store integrity, not just layout).
if ! HEAD_SHA="$(git -C "$REPO" rev-parse --verify HEAD 2>/dev/null)" || [[ -z "$HEAD_SHA" ]]; then
    echo "FAIL: HEAD not resolvable ($REPO)"
    exit 1
fi

echo "PASS: repository exists and is a valid git work tree"
echo "GIT_DIR=$GIT_DIR_PATH"
echo "HEAD=$HEAD_SHA"
exit 0

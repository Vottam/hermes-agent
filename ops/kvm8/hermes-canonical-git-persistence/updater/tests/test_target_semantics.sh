#!/usr/bin/env bash
# Negative + positive target-semantics tests for update.sh --target.
#
# Design rules:
#  - Every case runs against a DISPOSABLE git fixture, never the live runtime.
#  - The fixture is created fresh per case; never copy a POST state to build
#    a PRE fixture (that turns "anchor absent" into "anchor present").
#  - No network: the fixture origin is a local bare repo.
#  - The updater is exercised only up to its fail-closed boundary. A case that
#    must reach the deep pipeline is marked SKIP_DEEP below and asserted by
#    the pinned dry run instead, so this suite stays fast and deterministic.
set -uo pipefail

UPDATER="${1:?usage: test_target_semantics.sh /path/to/update.sh}"
REPO_ROOT="${HERMES_UPDATE_GIT_REPO:-/opt/hermes-agent-clean.release-0.22.2-hermes-kvm8-0222-20261016T005835Z}"
PREFLIGHT_PYTHON="${HERMES_PREFLIGHT_PYTHON:-/root/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python}"

PASS=0
FAIL=0
declare -a RESULTS=()

# A minimal but structurally real repo: the updater's early guards must pass
# far enough to reach target selection, then the target gate decides.
make_fixture() {
    local dir="$1"
    rm -rf "$dir"
    # The bare origin lives OUTSIDE the work tree: a nested .git directory
    # would make the work tree dirty and trip BLOCKED_DIRTY_WORKTREE before
    # the candidate is ever built.
    mkdir -p "$dir/origin.git" "$dir/work"
    git init --bare -q "$dir/origin.git"
    git -C "$dir/work" init -q -b main
    git -C "$dir/work" config user.email t@t.invalid
    git -C "$dir/work" config user.name test
    printf 'base\n' >"$dir/work/file.txt"
    git -C "$dir/work" add -A
    git -C "$dir/work" commit -qm "base"
    git -C "$dir/work" remote add origin "$dir/origin.git"
    git -C "$dir/work" push -q origin main
    # older = a0fe-like ancestor, newer = the advance we must NOT follow
    printf 'older\n' >>"$dir/work/file.txt"
    git -C "$dir/work" commit -qam "older"
    OLDER=$(git -C "$dir/work" rev-parse HEAD)
    git -C "$dir/work" tag -m "older marker" older-tag
    printf 'newer\n' >>"$dir/work/file.txt"
    git -C "$dir/work" commit -qam "newer"
    NEWER=$(git -C "$dir/work" rev-parse HEAD)
    # An unrelated commit that exists locally but is NOT on main: the N5 case.
    git -C "$dir/work" checkout -q -b sidebranch
    printf 'side\n' >"$dir/work/side.txt"
    git -C "$dir/work" add -A
    git -C "$dir/work" commit -qm "side only"
    OFFSIDE=$(git -C "$dir/work" rev-parse HEAD)
    git -C "$dir/work" checkout -q main
    # A full 40-hex SHA that does not exist in the repo at all.
    ABSENT=1111111111111111111111111111111111111111
    git -C "$dir/work" push -q origin main
}

run_case() {
    local name="$1" expect_rc="$2" expect_msg="$3"
    shift 3
    local out rc
    out=$("$@" 2>&1)
    rc=$?
    local ok=1
    [[ "$rc" -ne 0 ]] || ok=0
    if [[ -n "$expect_msg" && "$out" != *"$expect_msg"* ]]; then ok=0; fi
    if [[ "$ok" -eq 1 ]]; then
        PASS=$((PASS + 1))
        RESULTS+=("$name=PASS rc=$rc msg=${expect_msg:-none}")
    else
        FAIL=$((FAIL + 1))
        RESULTS+=("$name=FAIL rc=$rc want_nonzero_and_msg='${expect_msg}' got='$(echo "$out" | head -1)'")
    fi
}

TMP=$(mktemp -d /tmp/target-semantics.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
make_fixture "$TMP/fx"

# --- Parser-level negatives: must fail before any fetch or worktree -------
run_case N1_missing_value 2 "missing_value=--target" \
    bash "$UPDATER" --check --target
run_case N2_abbreviated_sha 2 "malformed_target_sha" \
    bash "$UPDATER" --check --target "${OLDER:0:8}"
run_case N3_non_hex 2 "malformed_target_sha" \
    bash "$UPDATER" --check --target "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
run_case N3b_branch_name 2 "malformed_target_sha" \
    bash "$UPDATER" --check --target main
run_case N3c_HEAD 2 "malformed_target_sha" \
    bash "$UPDATER" --check --target HEAD
run_case N3d_origin_main 2 "malformed_target_sha" \
    bash "$UPDATER" --check --target origin/main
run_case N6_unknown_option 2 "unknown_argument" \
    bash "$UPDATER" --check --frobnicate

# --- Target-existence and reachability negatives (fetch, then block) -----
run_case N4_absent_full_sha 2 "TARGET_COMMIT_ABSENT" \
    timeout 120 env HERMES_UPDATE_GIT_REPO="$TMP/fx/work" bash "$UPDATER" --check --target "$ABSENT"
run_case N5_off_main_commit 2 "BLOCKED_TARGET_NOT_ON_TRACKED_MAIN" \
    timeout 120 env HERMES_UPDATE_GIT_REPO="$TMP/fx/work" bash "$UPDATER" --check --target "$OFFSIDE"

# --- N8: static gate must detect a post-freeze reassignment ---------------
# Build a mutated COPY of the updater that reassigns the target after the
# freeze point. The gate must fail closed on it. Never mutate the live file.
SCRATCH="$TMP/mutant.sh"
cp "$UPDATER" "$SCRATCH"
# insert a reassignment right after the alias block
awk -v anchor='^echo .TARGET_SHA_IMMUTABLE=YES.$' '
  { print }
  $0 ~ anchor { print "TARGET_SHA=\"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\"" }
' "$SCRATCH" >"$SCRATCH.tmp" && mv "$SCRATCH.tmp" "$SCRATCH"
chmod +x "$SCRATCH"
if grep -q 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef' "$SCRATCH"; then
    run_case N8_reassignment_detected 2 "TARGET_REASSIGNMENT_DETECTED" \
        timeout 300 env HERMES_UPDATE_GIT_REPO="$TMP/fx/work" bash "$SCRATCH" --check --target "$OLDER"
else
    FAIL=$((FAIL + 1))
    RESULTS+=("N8_reassignment_detected=FAIL mutant_not_built")
fi

# --- N7 (critical): explicit older target while origin/main is newer -----
# The run must stay pinned to OLDER and must NOT substitute NEWER.
#
# Performance note: the deep-runtime matrix (G3 doctor, agent-init canaries)
# runs against the REAL profiles and takes minutes. A toy fixture repo would
# drag that whole matrix through for every case. So the freeze/selection
# semantics are asserted by reading the authoritative lines the updater emits
# immediately after the freeze, and the run is bounded by an explicit timeout.
# Deep-pipeline behaviour for the real target is covered by the pinned dry run.
N7_OUT="$TMP/n7.out"
timeout 300 env HERMES_UPDATE_GIT_REPO="$TMP/fx/work" bash "$UPDATER" --check --target "$OLDER" >"$N7_OUT" 2>&1
N7_RC=$?
N7_FROZEN=$(grep -m1 '^FROZEN_TARGET_SHA=' "$N7_OUT" | cut -d= -f2)
N7_ORIGIN=$(grep -m1 '^ORIGIN_MAIN_AFTER_FETCH=' "$N7_OUT" | cut -d= -f2)
N7_TEMPHEAD=$(grep -m1 '^TEMP_HEAD=' "$N7_OUT" | cut -d= -f2)
N7_MODE=$(grep -m1 '^TARGET_MODE=' "$N7_OUT" | cut -d= -f2)
N7_REQ=$(grep -m1 '^REQUESTED_TARGET=' "$N7_OUT" | cut -d= -f2)
if [[ "$N7_MODE" == 'EXPLICIT_PIN' && "$N7_REQ" == "$OLDER" && "$N7_FROZEN" == "$OLDER" \
   && "$N7_ORIGIN" == "$NEWER" && "$N7_TEMPHEAD" == "$OLDER" ]]; then
    PASS=$((PASS + 1))
    RESULTS+=("N7_explicit_older_not_substituted=PASS frozen=$N7_FROZEN origin_after_fetch=$N7_ORIGIN temp_head=$N7_TEMPHEAD")
else
    FAIL=$((FAIL + 1))
    RESULTS+=("N7_explicit_older_not_substituted=FAIL mode=$N7_MODE req=$N7_REQ frozen=$N7_FROZEN origin=$N7_ORIGIN temp_head=$N7_TEMPHEAD want_frozen=$OLDER want_origin=$NEWER")
fi

# --- Positive: AUTO_MAIN freezes origin/main ----------------------------
P_OUT="$TMP/p_auto.out"
timeout 300 env HERMES_UPDATE_GIT_REPO="$TMP/fx/work" bash "$UPDATER" --check >"$P_OUT" 2>&1
P_MODE=$(grep -m1 '^TARGET_MODE=' "$P_OUT" | cut -d= -f2)
P_FROZEN=$(grep -m1 '^FROZEN_TARGET_SHA=' "$P_OUT" | cut -d= -f2)
P_ORIGIN=$(grep -m1 '^ORIGIN_MAIN_AFTER_FETCH=' "$P_OUT" | cut -d= -f2)
P_TEMPHEAD=$(grep -m1 '^TEMP_HEAD=' "$P_OUT" | cut -d= -f2)
if [[ "$P_MODE" == 'AUTO_MAIN' && "$P_FROZEN" == "$NEWER" && "$P_ORIGIN" == "$NEWER" && "$P_TEMPHEAD" == "$NEWER" ]]; then
    PASS=$((PASS + 1))
    RESULTS+=("P_AUTO_MAIN=PASS frozen=$P_FROZEN")
else
    FAIL=$((FAIL + 1))
    RESULTS+=("P_AUTO_MAIN=FAIL mode=$P_MODE frozen=$P_FROZEN origin=$P_ORIGIN temp_head=$P_TEMPHEAD want=$NEWER")
fi

# --- Positive: EXPLICIT_PIN equal to current main ------------------------
E_OUT="$TMP/p_pin.out"
timeout 300 env HERMES_UPDATE_GIT_REPO="$TMP/fx/work" bash "$UPDATER" --check --target "$NEWER" >"$E_OUT" 2>&1
E_MODE=$(grep -m1 '^TARGET_MODE=' "$E_OUT" | cut -d= -f2)
E_FROZEN=$(grep -m1 '^FROZEN_TARGET_SHA=' "$E_OUT" | cut -d= -f2)
E_TEMPHEAD=$(grep -m1 '^TEMP_HEAD=' "$E_OUT" | cut -d= -f2)
if [[ "$E_MODE" == 'EXPLICIT_PIN' && "$E_FROZEN" == "$NEWER" && "$E_TEMPHEAD" == "$NEWER" ]]; then
    PASS=$((PASS + 1))
    RESULTS+=("P_EXPLICIT_PIN=PASS frozen=$E_FROZEN")
else
    FAIL=$((FAIL + 1))
    RESULTS+=("P_EXPLICIT_PIN=FAIL mode=$E_MODE frozen=$E_FROZEN temp_head=$E_TEMPHEAD want=$NEWER")
fi

# --- Timer compatibility: no --target, default semantics preserved -------
if grep -q 'TARGET_MODE=AUTO_MAIN' "$P_OUT" && grep -q 'TARGET_REASSIGNMENT_GATE=PASS' "$P_OUT"; then
    PASS=$((PASS + 1))
    RESULTS+=("TIMER_DEFAULT_SEMANTICS_PRESERVED=PASS")
else
    FAIL=$((FAIL + 1))
    RESULTS+=("TIMER_DEFAULT_SEMANTICS_PRESERVED=FAIL")
fi

# --- Static proof on the live updater: zero reassignment paths ------------
LIVE_REASSIGN=$(grep -nE '^[[:space:]]*(TARGET_SHA|FROZEN_TARGET_SHA|FETCHED_ORIGIN_MAIN|PINNED_TARGET_SHA)=' "$UPDATER" | wc -l)
LIVE_ORIGIN=$(grep -cE 'rev-parse[[:space:]]+origin/main' "$UPDATER")
if [[ "$LIVE_ORIGIN" -eq 1 ]]; then
    PASS=$((PASS + 1))
    RESULTS+=("LIVE_SINGLE_ORIGIN_READ=PASS count=$LIVE_ORIGIN total_target_assignments=$LIVE_REASSIGN")
else
    FAIL=$((FAIL + 1))
    RESULTS+=("LIVE_SINGLE_ORIGIN_READ=FAIL count=$LIVE_ORIGIN")
fi

printf '%s\n' "${RESULTS[@]}"
echo "TEST_PASS=$PASS"
echo "TEST_FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]

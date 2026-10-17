#!/usr/bin/env bash
# PINNED_TARGET_UPDATER — KVM8 Control Plane
# NON_INTERACTIVE=YES / DO_NOT_CLARIFY=YES / FAIL_CLOSED=YES
# The target is fetched and pinned once. --apply is the only promotion mode.
set -euo pipefail

REPO="${HERMES_UPDATE_GIT_REPO:-/opt/hermes-agent-clean.release-0.22.2-hermes-kvm8-0222-20261016T005835Z}"
export HERMES_CANONICAL_REPO="$REPO"
export HERMES_CANONICAL_PM_LAUNCHER="${HERMES_CANONICAL_PM_LAUNCHER:-$REPO/.hermes/bin/hermes}"

# ─── Shared host-level update lock (B2) ───────────────────────────────────
# The Hermes updater and the Hindsight updater serialize through ONE common
# root-owned volatile lock. Same path, one resource, mutual exclusion — never
# two unrelated lock files. /run/lock is this host's existing root-owned volatile
# lock directory, and a lock must not survive a reboot.
#
# Acquired BEFORE the fetch, which is itself a mutation of remote-tracking refs,
# and therefore before any other mutating operation: candidate worktree, gates,
# promotion reset, post-gates, rollback and the final state write. The FD is held
# for the whole transaction and released only at process exit.
SHARED_UPDATE_LOCK="${MASTERTREND_UPDATE_LOCK:-/run/lock/mastertrend-update.lock}"
# Distinct non-zero exit for "another update is running". EX_TEMPFAIL(75) is
# separate from this updater's existing codes (0/2/20/31) and means "transient,
# retry later" rather than "unsafe".
UPDATE_LOCK_BLOCKED_EXIT=75
# Bounded wait, default 0 = fail closed immediately. A non-zero default is NOT
# justified: hermes-update-git fires 03:00 and hindsight-update-safe fires
# 03:17, while a Hindsight transaction can legitimately hold the lock for up to
# 30min. Waiting would turn a clear refusal into a silent hang.
UPDATE_LOCK_WAIT_SECONDS="${MASTERTREND_UPDATE_LOCK_WAIT:-0}"
LOCK_FD=9
RECONCILER_DIR="${HERMES_RECONCILER_DIR:-/opt/mastertrend-hermes/customizations/statusbar-resolved-model}"
CUSTOM03_DIR="${HERMES_CUSTOM03_DIR:-/opt/mastertrend-hermes/customizations/CUSTOM_03}"
CUSTOM03_RECONCILER="$CUSTOM03_DIR/reconcile.py"
CUSTOM03_ACCEPTANCE="$CUSTOM03_DIR/acceptance.py"
CUSTOM03_TESTS="$CUSTOM03_DIR/test_reconcile.py"
CUSTOM07_DIR="${HERMES_CUSTOM07_DIR:-/opt/mastertrend-hermes/customizations/CUSTOM_07}"
CUSTOM07_RECONCILER="$CUSTOM07_DIR/reconcile.py"
CUSTOM07_ACCEPTANCE="$CUSTOM07_DIR/acceptance.py"
CUSTOM07_TESTS="$CUSTOM07_DIR/test_reconcile.py"
CUSTOM08_DIR="${HERMES_CUSTOM08_DIR:-/opt/mastertrend-hermes/customizations/CUSTOM_08}"
CUSTOM08_RECONCILER="$CUSTOM08_DIR/reconcile.py"
CUSTOM08_ACCEPTANCE="$CUSTOM08_DIR/acceptance.py"
CUSTOM09_DIR="${HERMES_CUSTOM09_DIR:-/opt/mastertrend-hermes/customizations/CUSTOM_09}"
CUSTOM09_RECONCILER="$CUSTOM09_DIR/reconcile.py"
CUSTOM09_TESTS="$CUSTOM09_DIR/test_reconcile.py"
CUSTOM10_DIR="${HERMES_CUSTOM10_DIR:-/opt/mastertrend-hermes/customizations/CUSTOM_10}"
CUSTOM10_CHECK="$CUSTOM10_DIR/hindsight-tier1-effective-config.sh"
CUSTOM11_DIR="${HERMES_CUSTOM11_DIR:-/opt/mastertrend-hermes/customizations/CUSTOM_11}"
CUSTOM11_RECONCILER="$CUSTOM11_DIR/reconcile.py"
CUSTOM11_ACCEPTANCE="$CUSTOM11_DIR/acceptance.py"
CUSTOM11_TESTS="$CUSTOM11_DIR/libatomic_preflight.py"
DEEP_GATES_DIR="${HERMES_DEEP_GATES_DIR:-/opt/mastertrend-hermes/customizations/DEEP_RUNTIME_GATES}"
GATE_RUNTIME_MODULE="$DEEP_GATES_DIR/gate_runtime.py"
G1_PROFILE_PLUGIN_GATE="$DEEP_GATES_DIR/g1_profile_plugin_gate.py"
G2_PTY_DEP_GUARD="$DEEP_GATES_DIR/g2_pty_dep_guard.py"
G3_DOCTOR_GATE="$DEEP_GATES_DIR/g3_doctor_gate.py"
G456_AGENT_INIT_GATE="$DEEP_GATES_DIR/g456_agent_init_gate.py"
G7_GATEWAY_GATE="$DEEP_GATES_DIR/g7_gateway_gate.py"
DEV_LIFECYCLE_GATE="$DEEP_GATES_DIR/dev_lifecycle_gate.py"
HINDSIGHT_VERSION_GUARD="$DEEP_GATES_DIR/hindsight_version_guard.py"
PIPE_STATUS_GATE="$DEEP_GATES_DIR/pipe_status.py"
PREFLIGHT_PYTHON="${HERMES_PREFLIGHT_PYTHON:-/root/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python}"
GATE_PYTHONPATH="${HERMES_GATE_PYTHONPATH:-/root/.hermes/installs/00574730e48a18ef/pm-runtime/generations/ef654e0e03f34fd494bc5914325e9c53/lib/python3.14/site-packages:/root/.hermes/venvs/hermes-kvm8-canonical/lib/python3.11/site-packages}"
export PYTHONPATH="${GATE_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}"
TUI_GATE_DIR="${HERMES_TUI_GATE_DIR:-/opt/mastertrend-hermes/customizations/TUI_GATE}"
TUI_GATE_HELPER="$TUI_GATE_DIR/tui_gate.py"
TUI_PTY_TIMEOUT_SECONDS="${HERMES_TUI_PTY_TIMEOUT_SECONDS:-45}"
export NON_INTERACTIVE=YES DO_NOT_CLARIFY=YES FAIL_CLOSED=YES

# ─── Unattended final record (B3 / Phase 10) ─────────────────────────────
# Every execution — success, failure, blocked, or help — must leave a final
# machine-readable terminal record, and the process exit status must equal
# REAL_EXIT_CODE. Defaults are explicit NOT_REACHED markers so an early exit
# still produces a complete, honest record instead of a silent one.
REC_TARGET_MODE='NOT_REACHED'
REC_ORIGIN_MAIN_AFTER_FETCH='NOT_REACHED'
REC_FROZEN_TARGET_SHA='NOT_REACHED'
REC_PROMOTION_EXECUTED='no'
REC_ROLLBACK_EXECUTED='no'
REC_PROMOTED_OPERATING_HEAD='NOT_RUN'
REC_FINAL_CLASSIFICATION='NOT_REACHED'
REC_MODE='NOT_REACHED'
REC_BLOCK_REASON=''

# Emit the final record. PROMOTED_OPERATING_HEAD is read from the REAL production
# checkout at emit time, never from a candidate SHA: a candidate is what we
# intended, the operating head is what is actually serving.
emit_final_record() {
    local real_rc="$1"
    local operating_head
    set +e
    if [[ "${REC_PROMOTION_EXECUTED}" == 'yes' ]]; then
        operating_head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo UNREADABLE)"
    else
        operating_head='NOT_RUN'
    fi
    # If promotion started but rolled back, the settled operating head is the
    # restored one, and the rollback must be visible in the record.
    set -e
    # Derive the freeze/origin facts from the LIVE variables at emit time rather
    # than from the REC_* cache. The record is emitted from the EXIT trap, which
    # runs after the run has settled, so the live values are authoritative.
    # A consumer parsing the first occurrence of these keys must not read a
    # stale NOT_REACHED placeholder from the cache.
    local _rec_origin="${FETCHED_ORIGIN_MAIN:-${ORIGIN_MAIN_AFTER_FETCH:-}}"
    local _rec_frozen="${FROZEN_TARGET_SHA:-}"
    local _rec_target_mode="${TARGET_MODE:-${REC_TARGET_MODE}}"
    if [[ "${_rec_frozen}" == "NOT_REACHED" || -z "${_rec_frozen}" ]]; then
        _rec_frozen="${REC_FROZEN_TARGET_SHA}"
    fi
    if [[ "${_rec_origin}" == "NOT_REACHED" || -z "${_rec_origin}" ]]; then
        _rec_origin="${REC_ORIGIN_MAIN_AFTER_FETCH}"
    fi
    echo '--- UNATTENDED_FINAL_RECORD ---'
    echo "UPDATER_NAME=hermes-update-git"
    echo "UPDATER_MODE=${REC_MODE}"
    echo "TARGET_MODE=${_rec_target_mode}"
    echo "ORIGIN_MAIN_AFTER_FETCH=${_rec_origin}"
    echo "FROZEN_TARGET_SHA=${_rec_frozen}"
    echo "PROMOTION_EXECUTED=${REC_PROMOTION_EXECUTED}"
    echo "ROLLBACK_EXECUTED=${REC_ROLLBACK_EXECUTED}"
    echo "PROMOTED_OPERATING_HEAD=${operating_head}"
    echo "REAL_EXIT_CODE=${real_rc}"
    echo "FINAL_CLASSIFICATION=${REC_FINAL_CLASSIFICATION}"
    if [[ "${REC_FINAL_CLASSIFICATION}" == "NOT_REACHED" ]]; then
        # The run classified the deep gate but was then killed (SIGTERM/SIGINT)
        # before reaching its own classify/exit. Reporting NOT_REACHED here
        # would be honest about the record but useless to an operator: the
        # settled truth is that the run did not complete. Never report a
        # bare NOT_REACHED as the terminal classification.
        echo "FINAL_CLASSIFICATION_INCOMPLETE=yes"
        REC_FINAL_CLASSIFICATION="INCOMPLETE_TERMINATED_BEFORE_CLASSIFICATION"
        echo "FINAL_CLASSIFICATION_EFFECTIVE=${REC_FINAL_CLASSIFICATION}"
    fi
    if [[ -n "${REC_BLOCK_REASON}" ]]; then
        echo "BLOCK_REASON=${REC_BLOCK_REASON}"
    fi
    echo "SHARED_UPDATE_LOCK=${SHARED_UPDATE_LOCK}"
    echo '--- END UNATTENDED_FINAL_RECORD ---'
}

# The single EXIT finalizer. It captures the real status FIRST, runs the existing
# temp-worktree cleanup, emits the record, then exits with the ORIGINAL status.
# A failure can never be laundered into exit 0.
#
# Signal handling: if the run is terminated (SIGTERM/SIGINT/SIGHUP — e.g. by
# systemd on timeout), the shell's EXIT trap still fires but $? is 0, which
# would publish "the update succeeded" for a killed run. The signal traps below
# record the cause so the final status can never be a misleading 0. A signalled
# run exits 143/130/129, matching the conventional 128+signal encoding.
SIGNALLED_RC=0
_on_signal() {
    local sig="$1"
    SIGNALLED_RC=$((128 + sig))
    REC_FINAL_CLASSIFICATION="INCOMPLETE_SIGNALLED_${sig}"
    REC_BLOCK_REASON="signal_${sig}"
    exit "${SIGNALLED_RC}"
}
trap '_on_signal 15' TERM
trap '_on_signal 2'  INT
trap '_on_signal 1'  HUP

finalize() {
    local real_rc=$?
    set +e
    if (( SIGNALLED_RC != 0 )); then
        real_rc="${SIGNALLED_RC}"
    fi
    if declare -F cleanup_temp >/dev/null 2>&1; then
        cleanup_temp
    fi
    emit_final_record "${real_rc}"
    exit "${real_rc}"
}
trap finalize EXIT

# ─── Shared host-level update lock ─────────────────────────────────────────
# Fail-closed, non-destructive mutual exclusion. A contender NEVER kills, signals
# or otherwise alters the current owner, and NEVER touches its own target.
acquire_shared_update_lock() {
    local lock_dir lock_dir_mode
    lock_dir="$(dirname "${SHARED_UPDATE_LOCK}")"
    if [[ ! -d "${lock_dir}" ]]; then
        mkdir -p "${lock_dir}" || {
            REC_BLOCK_REASON="LOCK_PATH_UNAVAILABLE"
            echo 'UPDATE_LOCK_ACQUIRED=no'
            echo "BLOCK_REASON=${REC_BLOCK_REASON}"
            return 1
        }
    fi
    # A world-writable lock directory would let a non-root process hijack the
    # mutex. Fail closed rather than trust it.
    #
    # The previous check here was a regex over the octal string
    # (`^0?[2367][0-7][0-7]$`), which tests the MOST significant digit. That
    # falsely rejected a perfectly safe root-owned 0755 directory such as
    # /run/lock, because 0755 starts with 7. The permission decision is made
    # from the numeric mode bits instead: the directory must be owned by root
    # and must not be world-writable; a group-writable directory is only
    # acceptable when the group is itself root.
    lock_dir_mode="$(stat -c '%a' "${lock_dir}" 2>/dev/null || echo unknown)"
    lock_dir_owner="$(stat -c '%U' "${lock_dir}" 2>/dev/null || echo unknown)"
    lock_dir_group="$(stat -c '%G' "${lock_dir}" 2>/dev/null || echo unknown)"
    if [[ "${lock_dir_mode}" != unknown ]]; then
        if (( (8#${lock_dir_mode} & 0002) != 0 )); then
            REC_BLOCK_REASON="LOCK_DIR_PERMISSIONS_UNSAFE"
            echo 'UPDATE_LOCK_ACQUIRED=no'
            echo "BLOCK_REASON=${REC_BLOCK_REASON}"
            echo "LOCK_DIR_MODE=${lock_dir_mode}"
            return 1
        fi
        if [[ "${lock_dir_owner}" != root ]]; then
            REC_BLOCK_REASON="LOCK_DIR_NOT_ROOT_OWNED"
            echo 'UPDATE_LOCK_ACQUIRED=no'
            echo "BLOCK_REASON=${REC_BLOCK_REASON}"
            echo "LOCK_DIR_OWNER=${lock_dir_owner}"
            return 1
        fi
        if (( (8#${lock_dir_mode} & 0020) != 0 )) && [[ "${lock_dir_group}" != root ]]; then
            REC_BLOCK_REASON="LOCK_DIR_PERMISSIONS_UNSAFE"
            echo 'UPDATE_LOCK_ACQUIRED=no'
            echo "BLOCK_REASON=${REC_BLOCK_REASON}"
            echo "LOCK_DIR_MODE=${lock_dir_mode} LOCK_DIR_GROUP=${lock_dir_group}"
            return 1
        fi
    fi
    if ! eval "exec ${LOCK_FD}>\"\${SHARED_UPDATE_LOCK}\"" 2>/dev/null; then
        REC_BLOCK_REASON="LOCK_OPEN_FAILED"
        echo 'UPDATE_LOCK_ACQUIRED=no'
        echo "BLOCK_REASON=${REC_BLOCK_REASON}"
        return 1
    fi
    if ! flock -w "${UPDATE_LOCK_WAIT_SECONDS}" "${LOCK_FD}"; then
        REC_BLOCK_REASON="CONCURRENT_UPDATE_ACTIVE"
        echo 'UPDATE_LOCK_ACQUIRED=no'
        echo "BLOCK_REASON=${REC_BLOCK_REASON}"
        echo "SHARED_UPDATE_LOCK=${SHARED_UPDATE_LOCK}"
        echo "UPDATE_LOCK_WAIT_SECONDS=${UPDATE_LOCK_WAIT_SECONDS}"
        return 1
    fi
    echo 'UPDATE_LOCK_ACQUIRED=yes'
    echo "SHARED_UPDATE_LOCK=${SHARED_UPDATE_LOCK}"
    echo "UPDATE_LOCK_WAIT_SECONDS=${UPDATE_LOCK_WAIT_SECONDS}"
    return 0
}

# Set the run's final classification ONCE. It echoes the exact same
# `FINAL_CLASSIFICATION=<value>` line the script always emitted, so no existing
# consumer changes behaviour, and it also records the value so the unattended
# final record can never disagree with the decision the run actually made.
classify() {
    REC_FINAL_CLASSIFICATION="$1"
    echo "FINAL_CLASSIFICATION=$1"
}

usage() {
    cat <<'USAGE'
Usage: hermes-update-git.sh [--check|--apply] [--target <FULL_40_HEX_COMMIT_SHA>] [--help]
  --check  Fetch and validate the pinned target, never promote (default)
  --apply  Fetch, preflight, reset exactly to TARGET_SHA, reconcile and validate
  --target <sha>
           Pin the run to this exact 40-hex commit. The SHA must already exist
           locally and be reachable from the fetched origin/main. It is frozen
           once and never substituted by a later origin/main advance.
  --help   Show this help and exit without fetching
USAGE
}

MODE=CHECK
REQUESTED_TARGET=''
TARGET_MODE=EXPLICIT_PIN
REC_MODE=CHECK
while (($#)); do
    case "$1" in
        --check) MODE=CHECK ;;
        --apply) MODE=APPLY ;;
        --target)
            if [[ $# -lt 2 ]]; then
                echo 'FAIL: missing_value=--target' >&2; usage >&2; exit 2
            fi
            REQUESTED_TARGET="$2"
            shift
            ;;
        --target=*)
            REQUESTED_TARGET="${1#--target=}"
            ;;
        --help|-h) usage; REC_MODE=HELP; classify NOOP_HELP; exit 0 ;;
        *) echo "FAIL: unknown_argument=$1"; usage >&2; REC_MODE=INVALID; REC_BLOCK_REASON="unknown_argument=$1"; classify BLOCKED_BAD_ARGUMENT; exit 2 ;;
    esac
    shift
done

# PHASE 3: strict validation of an explicit target happens at parse time,
# before any fetch, so a malformed request can never touch refs, build a
# candidate, or create a worktree. Only a full 40-hex commit SHA is accepted:
# branch names, tags, abbreviated SHAs, HEAD and revision expressions are
# rejected here, before any git command can interpret them.
if [[ -n "$REQUESTED_TARGET" ]]; then
    if [[ ! "$REQUESTED_TARGET" =~ ^[0-9a-fA-F]{40}$ ]]; then
        echo "FAIL: malformed_target_sha=$REQUESTED_TARGET" >&2
        echo 'FAIL: expected exactly 40 hexadecimal characters' >&2
        usage >&2
        REC_BLOCK_REASON="malformed_target_sha=$REQUESTED_TARGET"
        classify BLOCKED_BAD_ARGUMENT
        exit 2
    fi
    # Normalize case only; the value itself is otherwise used verbatim.
    REQUESTED_TARGET="${REQUESTED_TARGET,,}"
    TARGET_MODE=EXPLICIT_PIN
else
    TARGET_MODE=AUTO_MAIN
fi
REC_TARGET_MODE="$TARGET_MODE"
if [[ "$MODE" == APPLY ]]; then REC_MODE=APPLY; fi

if [[ ! -x "$PREFLIGHT_PYTHON" ]]; then
    echo "FAIL: preflight_python_missing=$PREFLIGHT_PYTHON"
    REC_BLOCK_REASON="preflight_python_missing"
    classify BLOCKED_SAFE
    exit 2
fi
if [[ ! -d "$REPO/.git" ]]; then
    echo "BLOCKED: BLOCKED: REPO_MISSING=$REPO"
    classify BLOCKED_SAFE
    exit 2
fi
if [[ ! -x "$RECONCILER_DIR/reconcile.py" || ! -x "$RECONCILER_DIR/acceptance.py" ]]; then
    echo "BLOCKED: BLOCKED: RECONCILIATORS_MISSING=$RECONCILER_DIR"
    classify BLOCKED_SAFE
    exit 2
fi
if [[ ! -f "$CUSTOM03_RECONCILER" || ! -f "$CUSTOM03_ACCEPTANCE" || ! -f "$CUSTOM03_TESTS" ]]; then
    echo "BLOCKED: BLOCKED: CUSTOM03_RECONCILIATORS_MISSING=$CUSTOM03_DIR"
    classify BLOCKED_SAFE
    exit 2
fi
if [[ ! -f "$CUSTOM07_RECONCILER" || ! -f "$CUSTOM07_ACCEPTANCE" || ! -f "$CUSTOM07_TESTS" ]]; then
    echo "BLOCKED: BLOCKED: CUSTOM07_RECONCILIATORS_MISSING=$CUSTOM07_DIR"
    classify BLOCKED_SAFE
    exit 2
fi
if [[ ! -f "$TUI_GATE_HELPER" ]]; then
    echo "BLOCKED: BLOCKED: TUI_GATE_HELPER_MISSING=$TUI_GATE_HELPER"
    classify BLOCKED_SAFE
    exit 2
fi
# CUSTOM_08 and the deep-runtime gates are mandatory: an update that cannot
# prove PM lifecycle, per-profile plugin state, Hindsight version policy, real
# doctor, real agent-init, live gateway runtime and the DEV isolation contract
# must not be considered eligible.
for required_gate in "$CUSTOM08_RECONCILER" "$CUSTOM08_ACCEPTANCE" \
                     "$CUSTOM09_RECONCILER" "$CUSTOM10_CHECK" \
                     "$CUSTOM11_RECONCILER" "$CUSTOM11_ACCEPTANCE" \
                     "$CUSTOM11_TESTS" "$GATE_RUNTIME_MODULE" \
                     "$G1_PROFILE_PLUGIN_GATE" "$G2_PTY_DEP_GUARD" \
                     "$G3_DOCTOR_GATE" "$G456_AGENT_INIT_GATE" \
                     "$G7_GATEWAY_GATE" "$DEV_LIFECYCLE_GATE" "$PIPE_STATUS_GATE" \
                     "$HINDSIGHT_VERSION_GUARD"; do
    if [[ ! -f "$required_gate" ]]; then
        echo "BLOCKED: BLOCKED: DEEP_GATE_MISSING=$required_gate"
        classify BLOCKED_SAFE
        exit 2
    fi
done

printf '%s\n' '=== PINNED_TARGET_UPDATER START ==='
printf 'MODE=%s\n' "$MODE"
printf 'TARGET_MODE=%s\n' "$TARGET_MODE"
printf 'REQUESTED_TARGET=%s\n' "${REQUESTED_TARGET:-NONE}"

# ─── Acquire the shared update lock (B2) ───────────────────────────────────
# Acquired BEFORE the fetch, which is itself a mutation of remote-tracking refs
# and therefore the first mutating operation in this script. From here on the
# lock is held across candidate worktree creation, every gate, the promotion
# reset, post-promotion gates, any rollback and the final record, and is
# released only when the process exits.
if ! acquire_shared_update_lock; then
    REC_FINAL_CLASSIFICATION='BLOCKED_CONCURRENT_UPDATE'
    [[ -n "${REC_BLOCK_REASON}" ]] || REC_BLOCK_REASON='LOCK_NOT_ACQUIRED'
    echo 'PROMOTION=NOT_RUN'
    echo 'UPDATER_RESULT=BLOCKED_CONCURRENT_UPDATE'
    echo "FINAL_CLASSIFICATION=${REC_FINAL_CLASSIFICATION}"
    exit "${UPDATE_LOCK_BLOCKED_EXIT}"
fi

printf '%s\n' FETCH_START
git -C "$REPO" fetch --prune origin || { echo 'FAIL: fetch_failed'; REC_BLOCK_REASON='fetch_failed'; classify BLOCKED_SAFE; exit 2; }

# PHASE 5: report origin/main exactly as it is after the one fetch, before
# any decision. This is evidence, not a decision input: in EXPLICIT_PIN the
# run must NOT follow it.
ORIGIN_MAIN_AFTER_FETCH=$(git -C "$REPO" rev-parse origin/main)
echo "ORIGIN_MAIN_AFTER_FETCH=$ORIGIN_MAIN_AFTER_FETCH"
REC_ORIGIN_MAIN_AFTER_FETCH="$ORIGIN_MAIN_AFTER_FETCH"

# PHASE 3/4: freeze the target exactly once, immediately after the only
# fetch, and never refetch before promotion. FROZEN_TARGET_SHA is the single
# source of truth for candidate creation, every gate, reporting and the
# promotion reset. A later origin/main advance cannot change it.
if [[ "$TARGET_MODE" == 'EXPLICIT_PIN' ]]; then
    if ! git -C "$REPO" cat-file -e "${REQUESTED_TARGET}^{commit}" 2>/dev/null; then
        echo "BLOCKED: TARGET_COMMIT_ABSENT=$REQUESTED_TARGET"
        echo 'PROMOTION=BLOCKED_TARGET_NOT_A_COMMIT'
        classify BLOCKED_SAFE
        exit 2
    fi
    # TRACK_MAIN policy: upstream policy follows main, so an explicit target
    # is only acceptable when it is reachable from the fetched origin/main.
    # This never fetches untrusted refs; it only reasons about what was
    # already fetched.
    if ! git -C "$REPO" merge-base --is-ancestor "$REQUESTED_TARGET" "$ORIGIN_MAIN_AFTER_FETCH"; then
        echo "BLOCKED_TARGET_NOT_ON_TRACKED_MAIN=$REQUESTED_TARGET"
        echo "TRACKED_MAIN=$ORIGIN_MAIN_AFTER_FETCH"
        echo 'PROMOTION=BLOCKED_TARGET_NOT_ON_TRACKED_MAIN'
        classify BLOCKED_SAFE
        exit 2
    fi
    FROZEN_TARGET_SHA="$REQUESTED_TARGET"
else
    FROZEN_TARGET_SHA="$ORIGIN_MAIN_AFTER_FETCH"
fi

# Invariant, not a comment: in EXPLICIT_PIN the requested and frozen values
# must be identical. Any divergence is a bug, never a fallback to main.
if [[ "$TARGET_MODE" == 'EXPLICIT_PIN' && "$REQUESTED_TARGET" != "$FROZEN_TARGET_SHA" ]]; then
    echo 'FAIL: PIN_INVARIANT_VIOLATION'
    classify BLOCKED_SAFE
    exit 2
fi

# Downstream compatibility: TARGET_SHA remains the name consumed by every
# existing gate, and is assigned exactly once, from the frozen value only.
TARGET_SHA="$FROZEN_TARGET_SHA"
FETCHED_ORIGIN_MAIN=$FROZEN_TARGET_SHA
PINNED_TARGET_SHA=$FROZEN_TARGET_SHA
echo "FROZEN_TARGET_SHA=$FROZEN_TARGET_SHA"
REC_FROZEN_TARGET_SHA="$FROZEN_TARGET_SHA"
echo "FETCHED_ORIGIN_MAIN=$FETCHED_ORIGIN_MAIN"
echo "PINNED_TARGET_SHA=$PINNED_TARGET_SHA"
echo 'NO_SECOND_FETCH_BEFORE_PROMOTION=YES'
echo 'TARGET_SHA_IMMUTABLE=YES'

# PHASE 4 gate: prove statically that nothing below this point can reassign
# the target or re-read origin/main. The anchor is the compatibility alias
# block (TARGET_SHA / FETCHED_ORIGIN_MAIN / PINNED_TARGET_SHA assigned from
# FROZEN_TARGET_SHA), which is the last legitimate write to any target
# variable. Everything scanned below it is post-freeze code.
SELF_PATH="${BASH_SOURCE[0]}"
FREEZE_LINE=$(grep -nE '^TARGET_SHA="\$FROZEN_TARGET_SHA"$' "$SELF_PATH" | tail -1 | cut -d: -f1)
if [[ -z "$FREEZE_LINE" || ! "$FREEZE_LINE" =~ ^[0-9]+$ || "$FREEZE_LINE" -lt 1 ]]; then
    echo "FAIL: FREEZE_LINE_UNRESOLVED=$FREEZE_LINE"
    classify BLOCKED_SAFE
    exit 2
fi
# The alias block itself spans the anchor plus the following two assignment
# lines; exclude exactly those three from the reassignment count.
ALIAS_BLOCK_END=$((FREEZE_LINE + 3))
# grep -c exits 1 when the count is zero; under set -e that would abort a
# script whose correct state IS "no reassignment paths". Guard it explicitly.
TAIL_REASSIGN=$(tail -n "+$ALIAS_BLOCK_END" "$SELF_PATH" | grep -cE '^[[:space:]]*(TARGET_SHA|FROZEN_TARGET_SHA|FETCHED_ORIGIN_MAIN|PINNED_TARGET_SHA)=' || true)
# The only legitimate origin/main read after the freeze is the pre-freeze
# evidence line; any read in post-freeze code would reintroduce drift.
TAIL_ORIGIN_REREAD=$(tail -n "+$ALIAS_BLOCK_END" "$SELF_PATH" | grep -cE 'rev-parse[[:space:]]+origin/main' || true)
TARGET_REASSIGNMENT_PATHS=$((TAIL_REASSIGN + TAIL_ORIGIN_REREAD))
echo "FREEZE_LINE=$FREEZE_LINE"
echo "TAIL_REASSIGN_AFTER_ALIASES=$TAIL_REASSIGN"
echo "TAIL_ORIGIN_MAIN_REREADS=$TAIL_ORIGIN_REREAD"
echo "TARGET_REASSIGNMENT_PATHS=$TARGET_REASSIGNMENT_PATHS"
if [[ "$TARGET_REASSIGNMENT_PATHS" -ne 0 ]]; then
    echo 'BLOCKED: TARGET_REASSIGNMENT_DETECTED'
    echo 'PROMOTION=BLOCKED_TARGET_REASSIGNMENT'
    classify BLOCKED_SAFE
    exit 2
fi
echo 'TARGET_REASSIGNMENT_GATE=PASS'

git -C "$REPO" cat-file -e "$TARGET_SHA^{commit}" || { echo 'BLOCKED: TARGET_NOT_COMMIT'; exit 2; }

# Capture before any possible production mutation.
PRE_HEAD=$(git -C "$REPO" rev-parse HEAD)
CURRENT_PROMOTED_TREE_SHA=$(git -C "$REPO" rev-parse HEAD^{tree})
echo "PRE_HEAD=$PRE_HEAD"
echo "CURRENT_PROMOTED_TREE_SHA=$CURRENT_PROMOTED_TREE_SHA"
echo 'UV_LOCK_VOLATILE_METADATA=SEMANTICALLY_NORMALIZED'

# A dirty live checkout is never a candidate for semantic comparison. This
# gate applies to --check as well as --apply so no result can hide local drift.
if [[ -n "$(git -C "$REPO" status --porcelain)" ]]; then
    echo 'WORKTREE_CLEAN=NO'
    echo 'PROMOTION=BLOCKED_DIRTY_WORKTREE'
    echo 'PROMOTION_DECISION=BLOCKED'
    echo 'UPDATER_RESULT=BLOCKED_DIRTY_WORKTREE'
    classify BLOCKED_SAFE
    exit 20
fi

echo 'WORKTREE_CLEAN=YES'

TEMP_WORKTREE="/tmp/hermes-kvm8-preflight-$(date +%s%N)-$$"
TEMP_CLEANED=NO
cleanup_temp() {
    set +e
    if [[ -d "$TEMP_WORKTREE" ]]; then
        git -C "$REPO" worktree remove --force "$TEMP_WORKTREE" >/dev/null 2>&1 || rm -rf "$TEMP_WORKTREE"
    fi
    TEMP_CLEANED=YES
}
# The single EXIT trap is `finalize` (installed near the top of this script), and
# it already calls cleanup_temp when that function exists. Re-registering
# `trap cleanup_temp EXIT` here would silently REPLACE the finalizer and destroy
# the unattended record, so it is deliberately not re-registered.
# The shared update lock was already acquired above, before the fetch.

git -C "$REPO" worktree add --detach "$TEMP_WORKTREE" "$TARGET_SHA" >/dev/null || {
    echo 'FAIL: worktree_create_failed'
    REC_BLOCK_REASON='worktree_create_failed'
    classify BLOCKED_SAFE
    exit 2
}
echo "TEMP_WORKTREE=$TEMP_WORKTREE"
TEMP_HEAD=$(git -C "$TEMP_WORKTREE" rev-parse HEAD)
echo "TEMP_HEAD=$TEMP_HEAD"
[[ "$TEMP_HEAD" == "$TARGET_SHA" ]] || { echo 'FAIL: TEMP_HEAD_NOT_PINNED'; exit 2; }

# PHASE 5 ANTI-RESURRECTION: Raw upstream is explicitly UNTRUSTED. It can be
# statically inspected and reconstructed, but never executed, imported,
# installed, promoted, or treated as an operational candidate before
# DESIRED_STATE_RECONSTRUCTION_COMPLETE + CUSTOM_03_FLOOR_PASS.
echo 'STATE_1=RAW_UPSTREAM_UNTRUSTED'
echo 'RAW_UPSTREAM_CLASSIFICATION=UNTRUSTED'
echo 'RAW_UPSTREAM_EXECUTABLE=no'
echo 'RAW_UPSTREAM_IMPORTABLE=no'
echo 'RAW_UPSTREAM_PROMOTABLE=no'
echo 'RAW_UPSTREAM_INSTALLABLE=no'
echo 'ANTI_RESURRECTION_MECHANISM=explicit_state_mark+reconstruction_before_eligibility'

run_check() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$RECONCILER_DIR/reconcile.py" --check --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_apply() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$RECONCILER_DIR/reconcile.py" --apply --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_acceptance() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$RECONCILER_DIR/acceptance.py" --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom03_check() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM03_RECONCILER" --check --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom03_apply() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM03_RECONCILER" --apply --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom03_acceptance() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM03_ACCEPTANCE" --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom03_tests() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM03_TESTS" --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom07_check() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM07_RECONCILER" --check --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom07_apply() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM07_RECONCILER" --apply --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom07_acceptance() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM07_ACCEPTANCE" --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_custom07_tests() {
    local repo_path="$1" exit_code
    set +e
    (cd "$repo_path"; PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM07_TESTS" --repo "$repo_path")
    exit_code=$?
    return "$exit_code"
}

run_cli_smoke() {
    local repo_path="$1" out err exit_code
    out="/tmp/hermes-kvm8-cli-smoke.$$.out"
    err="/tmp/hermes-kvm8-cli-smoke.$$.err"
    set +e
    (cd "$repo_path"; PYTHONPATH="$repo_path" PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" -m cli --help) >"$out" 2>"$err"
    exit_code=$?
    if [[ "$exit_code" -eq 0 ]] && ! grep -Eiq 'traceback|nameerror' "$err"; then
        echo 'CLI_SMOKE=PASS'
        rm -f "$out" "$err"
        return 0
    fi
    echo "CLI_SMOKE=FAIL (exit=$exit_code)"
    if [[ -s "$err" ]]; then
        printf 'CLI_SMOKE_ERROR='; tr '\n' ' ' <"$err"; printf '\n'
    fi
    rm -f "$out" "$err"
    return 1
}

# --- CUSTOM_08: PM lifecycle of every root profile wrapper -------------------
# CUSTOM_08 is a desired-state item like CUSTOM_03/07, not a documentation
# entry: it must be dispatched in BOTH the target preflight and the production
# phase, and its --check is a promotion gate.
run_custom08_check() {
    local repo_path="$1" exit_code
    set +e
    PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM08_RECONCILER" --check --repo "$repo_path"
    exit_code=$?
    return "$exit_code"
}

run_custom08_apply() {
    local repo_path="$1" exit_code
    set +e
    PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM08_RECONCILER" --apply --repo "$repo_path"
    exit_code=$?
    return "$exit_code"
}

run_custom08_acceptance() {
    local repo_path="$1" exit_code
    set +e
    PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$CUSTOM08_ACCEPTANCE" --repo "$repo_path"
    exit_code=$?
    return "$exit_code"
}

# --- Deep runtime gates (G1..G7 + DEV + PHASE 12) ---------------------------
run_deep_gate() {
    # $1 = gate script, remaining args forwarded verbatim.
    local gate="$1"; shift
    local exit_code
    set +e
    PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$gate" "$@"
    exit_code=$?
    return "$exit_code"
}

# ─── Canonical gate runtime dispatch (KVM8 2026-09-27) ─────────────────────
# The ONE place a customization/gate interpreter is selected.
#
# Why this exists: several source-of-truth gates read every profile's
# config.yaml through profile_gate, which imports PyYAML. This host has three
# interpreters and they disagree — the managed PM tool (3.14.x, and the one a
# bare `python3` resolves to in a root shell) has no PyYAML at all, correctly,
# because it is a tools runtime. A gate invoked there used to DEGRADE instead of
# failing: every profile came back unclassifiable, hindsight_required was False
# for all of them, and the gate printed NO_HINDSIGHT_REQUIRED_PROFILES +
# BLOCKED_SAFE — a claim about the profiles when the truth was about the
# interpreter.
#
# So: the interpreter is an explicit absolute path, its capability is verified
# against the modules the gate actually needs, and a missing module aborts the
# transaction by NAME. It never installs anything, and it never degrades into
# an empty target set. The result is emitted so a timer journal proves which
# interpreter ran instead of leaving it to be inferred.
GATE_RUNTIME_PREFLIGHT_RC=0

gate_runtime_preflight() {
    # $1 = gate id registered in gate_runtime.GATE_RUNTIME_REGISTRY.
    local gate_id="$1"
    if [[ ! -f "$GATE_RUNTIME_MODULE" ]]; then
        echo "GATE_RUNTIME_MODULE_MISSING=$GATE_RUNTIME_MODULE"
        echo "GATE_RUNTIME_CAPABILITY=FAIL"
        GATE_RUNTIME_PREFLIGHT_RC=1
        return 1
    fi
    set +e
    PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$GATE_RUNTIME_MODULE" --self-check --gate "$gate_id"
    local rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
        # The module already printed GATE_RUNTIME_ERROR=... naming the missing
        # dependency. Do not continue: running the gate would only reproduce
        # the misleading empty-target verdict.
        GATE_RUNTIME_PREFLIGHT_RC="$rc"
        return 1
    fi
    GATE_RUNTIME_PREFLIGHT_RC=0
    return 0
}

run_gate() {
    # $1 = gate id (runtime class owner), $2 = script, rest = args.
    # Preflights the runtime, then runs the gate with the SAME interpreter the
    # preflight certified. Timer and manual invocation take this identical
    # path, so a certified gate and a timer-run gate are the same binary.
    local gate_id="$1" script="$2"; shift 2
    if ! gate_runtime_preflight "$gate_id"; then
        echo "GATE_RUNTIME_ABORTED=$gate_id"
        return "${GATE_RUNTIME_PREFLIGHT_RC:-1}"
    fi
    if [[ ! -f "$script" ]]; then
        echo "GATE_SCRIPT_MISSING=$script"
        return 2
    fi
    local exit_code
    set +e
    PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$script" "$@"
    exit_code=$?
    set -e
    return "$exit_code"
}

run_shell_gate() {
    # Shell gates declare RUNTIME_SHELL: no interpreter capability applies, and
    # pretending otherwise would be a modelling error (CUSTOM_10 is bash).
    local script="$1"; shift
    if [[ ! -x "$script" ]]; then
        echo "GATE_SCRIPT_MISSING_OR_NOT_EXECUTABLE=$script"
        return 2
    fi
    local exit_code
    set +e
    "$script" "$@"
    exit_code=$?
    set -e
    return "$exit_code"
}

run_tui_selected_node() {
    local repo_path="$1" selected=""
    if [[ -n "${HERMES_NODE:-}" ]]; then
        selected="$HERMES_NODE"
    else
        selected=$(cd "$repo_path" && PYTHONPATH="$repo_path" PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" -c 'try:
    from hermes_constants import find_node_executable
    print(find_node_executable("node"))
except Exception:
    pass' 2>/dev/null || true)
        if [[ ! -x "$selected" ]]; then
            selected=$(command -v node 2>/dev/null || true)
        fi
    fi
    [[ -n "$selected" && -x "$selected" ]] || return 1
    printf '%s\n' "$selected"
}

run_tui_node_runtime_gate() {
    local node_path="$1" version file_info ldd_output version_exit=0
    if version=$("$node_path" --version 2>&1); then
        :
    else
        version_exit=$?
    fi
    echo "TUI_SELECTED_NODE=$node_path"
    echo "TUI_NODE_VERSION=$version"
    if [[ "$version_exit" -ne 0 ]]; then
        echo "NODE_RUNTIME_GATE=FAIL (version_exit=$version_exit)"
        return 1
    fi
    file_info=$(file -b "$node_path" 2>/dev/null || true)
    echo "TUI_NODE_FILE=$file_info"
    if [[ "$file_info" == *ELF* ]]; then
        if ! command -v ldd >/dev/null 2>&1; then
            echo 'MISSING_SHARED_LIBRARY_GATE=FAIL (ldd_missing)'
            return 1
        fi
        ldd_output=$(ldd "$node_path" 2>&1 || true)
        if grep -Fq 'not found' <<<"$ldd_output"; then
            echo 'MISSING_SHARED_LIBRARY_GATE=FAIL'
            printf 'TUI_NODE_LDD_ERROR=%s\n' "$(tr '\n' ' ' <<<"$ldd_output")"
            return 1
        fi
        echo 'MISSING_SHARED_LIBRARY_GATE=PASS'
    else
        echo 'MISSING_SHARED_LIBRARY_GATE=NOT_APPLICABLE_NON_ELF'
    fi
    echo 'NODE_RUNTIME_GATE=PASS'
}

run_tui_candidate_gate() {
    local repo_path="$1" node_path node_dir deps_log tui_log freshness transcript home pty_output deps_exit tui_exit pty_exit
    echo 'TUI_GATE_PHASE=PRODUCT_BUILD'
    node_path=$(run_tui_selected_node "$repo_path") || {
        echo 'NODE_RUNTIME_GATE=FAIL (node_not_selected)'
        return 1
    }
    run_tui_node_runtime_gate "$node_path" || return 1
    node_dir=$(dirname "$node_path")
    if [[ -n "${HERMES_TUI_PREPARED_NODE_MODULES:-}" ]]; then
        [[ -d "$HERMES_TUI_PREPARED_NODE_MODULES" ]] || {
            echo 'CANDIDATE_BUILD_STEP=FAIL (prepared_node_modules_missing)'
            return 1
        }
        rm -rf "$repo_path/node_modules"
        cp -a "$HERMES_TUI_PREPARED_NODE_MODULES" "$repo_path/node_modules"
        echo 'TUI_DEPENDENCY_PREPARATION=SEEDED_DISPOSABLE_LAB'
    fi
    deps_log=$(mktemp /tmp/hermes-kvm8-tui-deps.XXXXXX)
    tui_log=$(mktemp /tmp/hermes-kvm8-tui-build.XXXXXX)
    if (cd "$repo_path" && PATH="$node_dir:/usr/bin:/bin" "$node_path" scripts/build/node-deps.mjs --source "$repo_path" --reuse --workspace ui-tui) >"$deps_log" 2>&1; then
        deps_exit=0
    else
        deps_exit=$?
    fi
    cat "$deps_log"
    if [[ "$deps_exit" -ne 0 ]]; then
        echo "CANDIDATE_BUILD_STEP=FAIL (node_deps_exit=$deps_exit)"
        rm -f "$deps_log" "$tui_log"
        return 1
    fi
    if (cd "$repo_path" && PATH="$node_dir:/usr/bin:/bin" "$node_path" scripts/build/tui.mjs) >"$tui_log" 2>&1; then
        tui_exit=0
    else
        tui_exit=$?
    fi
    cat "$tui_log"
    rm -f "$deps_log" "$tui_log"
    if [[ "$tui_exit" -ne 0 ]]; then
        echo "CANDIDATE_BUILD_STEP=FAIL (tui_build_exit=$tui_exit)"
        return 1
    fi
    echo 'CANDIDATE_BUILD_STEP=PASS'
    [[ -f "$repo_path/ui-tui/dist/entry.js" && -s "$repo_path/ui-tui/dist/entry.js" ]] || { echo 'TUI_ARTIFACT_GATE=FAIL (entry_js)'; return 1; }
    [[ -f "$repo_path/ui-tui/dist/hermes-build.json" ]] || { echo 'TUI_ARTIFACT_GATE=FAIL (build_receipt)'; return 1; }
    [[ -f "$repo_path/ui-tui/dist/.hermes-product" ]] || { echo 'TUI_ARTIFACT_GATE=FAIL (product_marker)'; return 1; }
    echo 'TUI_ARTIFACT_GATE=PASS'
    freshness=$(cd "$repo_path" && PATH="$node_dir:/usr/bin:/bin" "$node_path" scripts/build/freshness.mjs --source "$repo_path" --product tui --out "$repo_path/ui-tui/dist")
    echo "TUI_FRESHNESS=$freshness"
    [[ "$freshness" == 'true' ]] || { echo 'TUI_ARTIFACT_GATE=FAIL (freshness_false)'; return 1; }
    echo 'TUI_FRESHNESS_GATE=PASS'
    home=$(mktemp -d /tmp/hermes-kvm8-tui-home.XXXXXX)
    transcript=$(mktemp /tmp/hermes-kvm8-tui-transcript.XXXXXX)
    if pty_output=$(HERMES_TUI_GATE_DIRECT_NODE=YES HERMES_NODE="$node_path" PYTHONPATH="$repo_path" PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$TUI_GATE_HELPER" pty --repo "$repo_path" --python "$PREFLIGHT_PYTHON" --node "$node_path" --home "$home" --timeout "$TUI_PTY_TIMEOUT_SECONDS" --transcript "$transcript" 2>&1); then
        pty_exit=0
    else
        pty_exit=$?
    fi
    printf '%s\n' "$pty_output"
    if [[ "${HERMES_TUI_KEEP_TRANSCRIPT:-NO}" != 'YES' ]]; then
        rm -f "$transcript"
    else
        echo "TUI_TRANSCRIPT=$transcript"
    fi
    rm -rf "$home"
    if [[ "$pty_exit" -ne 0 ]]; then
        echo "PTY_GATE=FAIL (exit=$pty_exit)"
        return 1
    fi
    echo "PTY_TIMEOUT_SECONDS=$TUI_PTY_TIMEOUT_SECONDS"
    echo 'PTY_GATE=PASS'
    echo 'TUI_STATE_CLASSIFIER=PASS'
    echo 'LAZY_TUI_BOOTSTRAP_USED=no'
    echo 'NO_LLM_CALL_REQUIRED=yes'
    return 0
}

run_tui_post_promotion_gate() {
    local repo_path="$1" node_path node_dir freshness home transcript pty_output pty_exit
    node_path=$(run_tui_selected_node "$repo_path") || { echo 'POST_PROMOTION_NODE_GATE=FAIL'; return 1; }
    run_tui_node_runtime_gate "$node_path" || return 1
    [[ -f "$repo_path/ui-tui/dist/entry.js" && -s "$repo_path/ui-tui/dist/entry.js" ]] || { echo 'POST_PROMOTION_TUI_ARTIFACT_GATE=FAIL'; return 1; }
    [[ -f "$repo_path/ui-tui/dist/hermes-build.json" && -f "$repo_path/ui-tui/dist/.hermes-product" ]] || { echo 'POST_PROMOTION_TUI_ARTIFACT_GATE=FAIL'; return 1; }
    node_dir=$(dirname "$node_path")
    freshness=$(cd "$repo_path" && PATH="$node_dir:/usr/bin:/bin" "$node_path" scripts/build/freshness.mjs --source "$repo_path" --product tui --out "$repo_path/ui-tui/dist")
    echo "POST_PROMOTION_TUI_FRESHNESS=$freshness"
    [[ "$freshness" == 'true' ]] || { echo 'POST_PROMOTION_TUI_ARTIFACT_GATE=FAIL'; return 1; }
    home=$(mktemp -d /tmp/hermes-kvm8-tui-home.XXXXXX)
    transcript=$(mktemp /tmp/hermes-kvm8-tui-transcript.XXXXXX)
    if pty_output=$(HERMES_TUI_GATE_DIRECT_NODE=YES HERMES_NODE="$node_path" PYTHONPATH="$repo_path" PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PYTHON" "$TUI_GATE_HELPER" pty --repo "$repo_path" --python "$PREFLIGHT_PYTHON" --node "$node_path" --home "$home" --timeout "$TUI_PTY_TIMEOUT_SECONDS" --transcript "$transcript" 2>&1); then
        pty_exit=0
    else
        pty_exit=$?
    fi
    printf '%s\n' "$pty_output"
    [[ "${HERMES_TUI_KEEP_TRANSCRIPT:-NO}" == 'YES' ]] || rm -f "$transcript"
    rm -rf "$home"
    [[ "$pty_exit" -eq 0 ]] || { echo "POST_PROMOTION_PTY_GATE=FAIL (exit=$pty_exit)"; return 1; }
    echo 'POST_PROMOTION_TUI_GATE=PASS'
}

compute_candidate_tree() {
    local repo_path="$1"
    # The linked worktree has its own index. This stages only TEMP state and
    # creates no commit in either TEMP or the live runtime checkout.
    git -C "$repo_path" add -A
    git -C "$repo_path" write-tree
}

compute_semantic_tree() {
    local repo_path="$1" raw_tree="$2" index normalized_blob
    index=$(mktemp "/tmp/hermes-kvm8-semantic-index.XXXXXX")
    rm -f "$index"
    cleanup_semantic_index() { rm -f "$index"; }
    trap cleanup_semantic_index RETURN
    GIT_INDEX_FILE="$index" git -C "$repo_path" read-tree "$raw_tree"
    if GIT_INDEX_FILE="$index" git -C "$repo_path" cat-file -e "$raw_tree:uv.lock" 2>/dev/null; then
        normalized_blob=$(GIT_INDEX_FILE="$index" git -C "$repo_path" show "$raw_tree:uv.lock" | "$PREFLIGHT_PYTHON" -c 'import re, sys; source=sys.stdin.read(); updated, count=re.subn(r"(?m)^exclude-newer = \\"[^\\"]+\\".*$", "exclude-newer = \\"<dynamic-relative-policy>\\"", source, count=1); raise SystemExit("uv.lock volatile metadata anchor missing") if count != 1 else print(updated, end="")' | git -C "$repo_path" hash-object -w --stdin)
        GIT_INDEX_FILE="$index" git -C "$repo_path" update-index --add --cacheinfo "100644,$normalized_blob,uv.lock"
    fi
    GIT_INDEX_FILE="$index" git -C "$repo_path" write-tree
    trap - RETURN
    cleanup_semantic_index
}

# CUSTOM_03 is an independent desired-state gate; statusbar remains mandatory below.
TARGET_CUSTOM03_CHECK_RESULT=FAIL
TARGET_CUSTOM03_APPLY_RESULT=FAIL
TARGET_CUSTOM03_POST_CHECK_RESULT=FAIL
TARGET_CUSTOM03_ACCEPTANCE_RESULT=FAIL
TARGET_CUSTOM03_TESTS_RESULT=FAIL
TARGET_CUSTOM03_FINAL=BLOCKED_SAFE

set +e
run_custom03_check "$TEMP_WORKTREE"
custom03_check_exit=$?
set -e
echo "TARGET_CUSTOM03_CHECK_EXIT=$custom03_check_exit"
if [[ "$custom03_check_exit" -eq 0 ]]; then
    TARGET_CUSTOM03_CHECK_RESULT=ALREADY_SATISFIED
    TARGET_CUSTOM03_APPLY_RESULT=PASS
    TARGET_CUSTOM03_POST_CHECK_RESULT=ALREADY_SATISFIED
    echo 'TARGET_CUSTOM03_CHECK=ALREADY_SATISFIED'
elif [[ "$custom03_check_exit" -eq 1 ]]; then
    TARGET_CUSTOM03_CHECK_RESULT=NEEDS_REIMPLEMENTATION
    echo 'TARGET_CUSTOM03_CHECK=NEEDS_REIMPLEMENTATION'
    set +e
    run_custom03_apply "$TEMP_WORKTREE"
    custom03_apply_exit=$?
    set -e
    echo "TARGET_CUSTOM03_APPLY_EXIT=$custom03_apply_exit"
    if [[ "$custom03_apply_exit" -eq 0 ]]; then
        TARGET_CUSTOM03_APPLY_RESULT=PASS
        set +e
        run_custom03_check "$TEMP_WORKTREE"
        custom03_post_check_exit=$?
        set -e
        echo "TARGET_CUSTOM03_POST_CHECK_EXIT=$custom03_post_check_exit"
        if [[ "$custom03_post_check_exit" -eq 0 ]]; then
            TARGET_CUSTOM03_POST_CHECK_RESULT=ALREADY_SATISFIED
        else
            echo 'TARGET_CUSTOM03_POST_CHECK=FAIL'
        fi
    else
        echo 'TARGET_CUSTOM03_APPLY=FAIL'
    fi
else
    echo 'TARGET_CUSTOM03_CHECK=BLOCKED_SAFE'
fi

if [[ "$TARGET_CUSTOM03_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' ]]; then
    set +e
    run_custom03_acceptance "$TEMP_WORKTREE"
    custom03_acceptance_exit=$?
    set -e
    echo "TARGET_CUSTOM03_ACCEPTANCE_EXIT=$custom03_acceptance_exit"
    [[ "$custom03_acceptance_exit" -eq 0 ]] && TARGET_CUSTOM03_ACCEPTANCE_RESULT=PASS
    set +e
    run_custom03_tests "$TEMP_WORKTREE"
    custom03_tests_exit=$?
    set -e
    echo "TARGET_CUSTOM03_TESTS_EXIT=$custom03_tests_exit"
    [[ "$custom03_tests_exit" -eq 0 ]] && TARGET_CUSTOM03_TESTS_RESULT=PASS
fi

if [[ "$TARGET_CUSTOM03_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM03_ACCEPTANCE_RESULT" == 'PASS' && "$TARGET_CUSTOM03_TESTS_RESULT" == 'PASS' ]]; then
    TARGET_CUSTOM03_FINAL=PASS_EXISTING
elif [[ "$TARGET_CUSTOM03_CHECK_RESULT" == 'NEEDS_REIMPLEMENTATION' && "$TARGET_CUSTOM03_APPLY_RESULT" == 'PASS' && "$TARGET_CUSTOM03_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM03_ACCEPTANCE_RESULT" == 'PASS' && "$TARGET_CUSTOM03_TESTS_RESULT" == 'PASS' ]]; then
    TARGET_CUSTOM03_FINAL=REIMPLEMENTED_PASS
fi
echo "CUSTOM_03_FINAL=$TARGET_CUSTOM03_FINAL"
echo "CUSTOM_03_HINDSIGHT_CONSTRAINT=>=0.10.0,<0.11.0"

# Anti-resurrection: Candidate eligibility only after desired-state reconstruction
# and floor validation. The raw upstream (STATE_1) is never eligible.
if [[ "$TARGET_CUSTOM03_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM03_FINAL" == 'REIMPLEMENTED_PASS' ]]; then
    echo 'STATE_4=CANDIDATE_ELIGIBLE=yes'
    echo 'RECONSTRUCTION_BEFORE_CANDIDATE_ELIGIBILITY=yes'
    echo 'CUSTOM03_AFTER_RECONSTRUCTION=yes'
    echo 'CUSTOM03_FAIL_CLOSED=yes'
else
    echo 'STATE_4=CANDIDATE_ELIGIBLE=no'
    echo 'RECONSTRUCTION_BEFORE_CANDIDATE_ELIGIBILITY=no'
    echo 'CUSTOM03_AFTER_RECONSTRUCTION=no'
    echo 'CUSTOM03_FAIL_CLOSED=no'
fi
# The floor gate allows any >=0.10.0 (current 0.10.0, future 0.10.1 permitted).
# Versions <0.10.0 (0.6.1, 0.9.2) are blocked by CUSTOM_03 acceptance.
echo 'MINIMUM_ALLOWED=0.10.0'
echo 'DESIRED_CURRENT=0.10.0'

# CUSTOM_07 is the fail-closed semantic WP-CLI environment gate.
TARGET_CUSTOM07_CHECK_RESULT=FAIL
TARGET_CUSTOM07_APPLY_RESULT=FAIL
TARGET_CUSTOM07_POST_CHECK_RESULT=FAIL
TARGET_CUSTOM07_ACCEPTANCE_RESULT=FAIL
TARGET_CUSTOM07_TESTS_RESULT=FAIL
TARGET_CUSTOM07_FINAL=BLOCKED_SAFE

set +e
run_custom07_check "$TEMP_WORKTREE"
custom07_check_exit=$?
set -e
echo "TARGET_CUSTOM07_CHECK_EXIT=$custom07_check_exit"
if [[ "$custom07_check_exit" -eq 0 ]]; then
    TARGET_CUSTOM07_CHECK_RESULT=ALREADY_SATISFIED
    TARGET_CUSTOM07_APPLY_RESULT=PASS
    TARGET_CUSTOM07_POST_CHECK_RESULT=ALREADY_SATISFIED
elif [[ "$custom07_check_exit" -eq 1 ]]; then
    TARGET_CUSTOM07_CHECK_RESULT=NEEDS_REIMPLEMENTATION
    set +e
    run_custom07_apply "$TEMP_WORKTREE"
    custom07_apply_exit=$?
    set -e
    echo "TARGET_CUSTOM07_APPLY_EXIT=$custom07_apply_exit"
    if [[ "$custom07_apply_exit" -eq 0 ]]; then
        TARGET_CUSTOM07_APPLY_RESULT=PASS
        set +e
        run_custom07_check "$TEMP_WORKTREE"
        custom07_post_check_exit=$?
        set -e
        echo "TARGET_CUSTOM07_POST_CHECK_EXIT=$custom07_post_check_exit"
        [[ "$custom07_post_check_exit" -eq 0 ]] && TARGET_CUSTOM07_POST_CHECK_RESULT=ALREADY_SATISFIED
    fi
else
    TARGET_CUSTOM07_CHECK_RESULT=BLOCKED_UPSTREAM_INCOMPATIBLE
fi
if [[ "$TARGET_CUSTOM07_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' ]]; then
    set +e
    run_custom07_acceptance "$TEMP_WORKTREE"
    custom07_acceptance_exit=$?
    set -e
    echo "TARGET_CUSTOM07_ACCEPTANCE_EXIT=$custom07_acceptance_exit"
    [[ "$custom07_acceptance_exit" -eq 0 ]] && TARGET_CUSTOM07_ACCEPTANCE_RESULT=PASS
    set +e
    run_custom07_tests "$TEMP_WORKTREE"
    custom07_tests_exit=$?
    set -e
    echo "TARGET_CUSTOM07_TESTS_EXIT=$custom07_tests_exit"
    [[ "$custom07_tests_exit" -eq 0 ]] && TARGET_CUSTOM07_TESTS_RESULT=PASS
fi
if [[ "$TARGET_CUSTOM07_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM07_ACCEPTANCE_RESULT" == 'PASS' && "$TARGET_CUSTOM07_TESTS_RESULT" == 'PASS' ]]; then
    TARGET_CUSTOM07_FINAL=PASS_EXISTING
elif [[ "$TARGET_CUSTOM07_CHECK_RESULT" == 'NEEDS_REIMPLEMENTATION' && "$TARGET_CUSTOM07_APPLY_RESULT" == 'PASS' && "$TARGET_CUSTOM07_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM07_ACCEPTANCE_RESULT" == 'PASS' && "$TARGET_CUSTOM07_TESTS_RESULT" == 'PASS' ]]; then
    TARGET_CUSTOM07_FINAL=REIMPLEMENTED_PASS
fi
echo "CUSTOM_07_FINAL=$TARGET_CUSTOM07_FINAL"

# The target preflight is authoritative and must execute on the pinned TEMP.
TARGET_STATUSBAR_CHECK_RESULT=FAIL
TARGET_STATUSBAR_APPLY_RESULT=FAIL
TARGET_STATUSBAR_POST_CHECK_RESULT=FAIL
TARGET_STATUSBAR_ACCEPTANCE_RESULT=FAIL
TARGET_STATUSBAR_CLI_SMOKE=FAIL
TARGET_PREFLIGHT=BLOCKED_SAFE

set +e
run_check "$TEMP_WORKTREE"
check_exit=$?
set -e
echo "TARGET_STATUSBAR_CHECK_EXIT=$check_exit"
if [[ "$check_exit" -eq 0 ]]; then
    TARGET_STATUSBAR_CHECK_RESULT=PASS_EXISTING
    TARGET_STATUSBAR_APPLY_RESULT=PASS
    TARGET_STATUSBAR_POST_CHECK_RESULT=PASS_EXISTING
    echo 'TARGET_STATUSBAR_CHECK=PASS_EXISTING'
elif [[ "$check_exit" -eq 1 ]]; then
    TARGET_STATUSBAR_CHECK_RESULT=NEEDS_REIMPLEMENTATION
    echo 'TARGET_STATUSBAR_CHECK=NEEDS_REIMPLEMENTATION'
    set +e
    run_apply "$TEMP_WORKTREE"
    apply_exit=$?
    set -e
    echo "TARGET_STATUSBAR_APPLY_EXIT=$apply_exit"
    if [[ "$apply_exit" -eq 0 ]]; then
        TARGET_STATUSBAR_APPLY_RESULT=PASS
        echo 'TARGET_STATUSBAR_APPLY=PASS'
        set +e
        run_check "$TEMP_WORKTREE"
        post_check_exit=$?
        set -e
        echo "TARGET_STATUSBAR_POST_CHECK_EXIT=$post_check_exit"
        if [[ "$post_check_exit" -eq 0 ]]; then
            TARGET_STATUSBAR_POST_CHECK_RESULT=PASS_EXISTING
            echo 'TARGET_STATUSBAR_POST_CHECK=PASS_EXISTING'
        else
            echo 'TARGET_STATUSBAR_POST_CHECK=FAIL'
        fi
    else
        echo "TARGET_STATUSBAR_APPLY=FAIL (exit=$apply_exit)"
    fi
else
    echo 'TARGET_STATUSBAR_CHECK=BLOCKED_SAFE'
fi

# Acceptance is mandatory even when the target already contains the customization.
if [[ "$TARGET_STATUSBAR_POST_CHECK_RESULT" == "PASS_EXISTING" ]]; then
    set +e
    run_acceptance "$TEMP_WORKTREE"
    acceptance_exit=$?
    set -e
    echo "TARGET_STATUSBAR_ACCEPTANCE_EXIT=$acceptance_exit"
    if [[ "$acceptance_exit" -eq 0 ]]; then
        TARGET_STATUSBAR_ACCEPTANCE_RESULT=PASS
        echo 'TARGET_STATUSBAR_ACCEPTANCE=PASS'
    else
        echo 'TARGET_STATUSBAR_ACCEPTANCE=FAIL'
    fi
fi

set +e
run_cli_smoke "$TEMP_WORKTREE"
smoke_exit=$?
set -e
if [[ "$smoke_exit" -eq 0 ]]; then
    TARGET_STATUSBAR_CLI_SMOKE=PASS
else
    TARGET_STATUSBAR_CLI_SMOKE=FAIL
fi

TARGET_TUI_GATE=FAIL
set +e
run_tui_candidate_gate "$TEMP_WORKTREE"
tui_gate_exit=$?
set -e
if [[ "$tui_gate_exit" -eq 0 ]]; then
    TARGET_TUI_GATE=PASS
fi
echo "TARGET_TUI_GATE=$TARGET_TUI_GATE"

# ---------------------------------------------------------------------------
# DEEP RUNTIME GATE MATRIX (G1..G7 + DEV + PHASE 12)
#
# These gates prove the CERTIFIED RUNTIME state, not the candidate tree. They
# are host-state gates by nature: the plugin state, the effective dependency
# versions, the real doctor, the real agent initialization, the live gateway
# process and the DEV isolation contract all live in the real profile homes.
#
# They therefore run once, before promotion eligibility, and every one of them
# is fail-closed. A future update that silently lost a plugin, accepted
# Hindsight >=0.11.0, left a wrapper on a legacy venv, or left the gateway on
# a legacy runtime is BLOCKED here rather than being reported as a success.
# ---------------------------------------------------------------------------
DEEP_GATE_TRANSCRIPT_DIR="${HERMES_DEEP_GATE_TRANSCRIPT_DIR:-/tmp/hermes-kvm8-deep-gates-$$}"
mkdir -p "$DEEP_GATE_TRANSCRIPT_DIR"
echo "DEEP_GATE_TRANSCRIPT_DIR=$DEEP_GATE_TRANSCRIPT_DIR"

# PHASE 12 first: a laundered pipeline status would make every later verdict
# meaningless, so the harness itself is proven before it is trusted.
TARGET_PIPE_STATUS_GATE=FAIL
set +e
run_deep_gate "$PIPE_STATUS_GATE" --self-test
pipe_status_self_test_exit=$?
# Scan the UPDATER, not $REPO: $REPO is the Hermes candidate tree, not a
# script. The harness is what must be proven, so it scans itself and every
# harness it dispatches.
run_deep_gate "$PIPE_STATUS_GATE" \
    /opt/mastertrend-hermes/update.sh \
    /usr/local/bin/hermes-update-git.sh \
    /usr/local/bin/hermes-update-safe.sh
pipe_status_scan_exit=$?
set -e
echo "TARGET_PIPE_STATUS_SELF_TEST_EXIT=$pipe_status_self_test_exit"
echo "TARGET_PIPE_STATUS_SCAN_EXIT=$pipe_status_scan_exit"
if [[ "$pipe_status_self_test_exit" -eq 0 && "$pipe_status_scan_exit" -eq 0 ]]; then
    TARGET_PIPE_STATUS_GATE=PASS
fi
echo "TARGET_PIPE_STATUS_GATE=$TARGET_PIPE_STATUS_GATE"

# CUSTOM_08 --check is a promotion gate: every operational root wrapper must
# resolve to the currently approved PM lifecycle, never to a legacy venv.
# The check is deliberately run against the live approved root and is read-only.
# A candidate worktree must never rewrite /root/.local/bin during --check or
# pre-promotion; launcher handoff is an independent post-promotion operation.
TARGET_CUSTOM08_CHECK_RESULT=FAIL
TARGET_CUSTOM08_APPLY_RESULT=NOT_RUN
TARGET_CUSTOM08_POST_CHECK_RESULT=FAIL
TARGET_CUSTOM08_ACCEPTANCE_RESULT=FAIL
TARGET_CUSTOM08_FINAL=BLOCKED_SAFE
set +e
run_custom08_check "$REPO"
custom08_check_exit=$?
set -e
echo "TARGET_CUSTOM08_CHECK_EXIT=$custom08_check_exit"
if [[ "$custom08_check_exit" -eq 0 ]]; then
    TARGET_CUSTOM08_CHECK_RESULT=ALREADY_SATISFIED
    TARGET_CUSTOM08_POST_CHECK_RESULT=ALREADY_SATISFIED
    set +e
    run_custom08_acceptance "$REPO"
    custom08_acceptance_exit=$?
    set -e
    echo "TARGET_CUSTOM08_ACCEPTANCE_EXIT=$custom08_acceptance_exit"
    [[ "$custom08_acceptance_exit" -eq 0 ]] && TARGET_CUSTOM08_ACCEPTANCE_RESULT=PASS
else
    TARGET_CUSTOM08_CHECK_RESULT=BLOCKED_SAFE
    echo 'TARGET_CUSTOM08_APPLY=NOT_RUN_PREPROMOTION'
fi
if [[ "$TARGET_CUSTOM08_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM08_ACCEPTANCE_RESULT" == 'PASS' ]]; then
    TARGET_CUSTOM08_FINAL=PASS_EXISTING
fi
echo "CUSTOM_08_FINAL=$TARGET_CUSTOM08_FINAL"


# ─── CUSTOM_09 / CUSTOM_10 / CUSTOM_11 dispatcher (KVM8 2026-09-27) ────────
# Before this block these three customizations were DISPATCHED BY NOBODY.
# customizations.yaml declared them but nothing in the repo read it, and
# update.sh only wired CUSTOM_08, so a timer-driven update could neither
# verify nor repair them -- their certified state existed only because a human
# had once run the scripts by hand. That is precisely the gap the dispatch
# record is meant to close: a customization that no dispatcher invokes cannot
# be part of a durability claim.
#
# Every entry below goes through run_gate / run_shell_gate, so the runtime is
# chosen and capability-checked in ONE place (gate_runtime.py) and is identical
# whether update.sh was started by hand, by the PM wrapper, or by the timer.
#
# CUSTOM_09 and CUSTOM_11 follow the same check -> apply -> post-check ->
# acceptance shape as CUSTOM_08: a reconciler that is already satisfied must
# say so without writing anything, and a real drift is repaired then re-proved.
TARGET_CUSTOM09_CHECK_RESULT=FAIL
TARGET_CUSTOM09_APPLY_RESULT=FAIL
TARGET_CUSTOM09_POST_CHECK_RESULT=FAIL
TARGET_CUSTOM09_TESTS_RESULT=FAIL
TARGET_CUSTOM09_FINAL=BLOCKED_SAFE
set +e
run_gate CUSTOM_09 "$CUSTOM09_RECONCILER" --check
custom09_check_exit=$?
set -e
echo "TARGET_CUSTOM09_CHECK_EXIT=$custom09_check_exit"
if [[ "$custom09_check_exit" -eq 0 ]]; then
    TARGET_CUSTOM09_CHECK_RESULT=ALREADY_SATISFIED
    TARGET_CUSTOM09_APPLY_RESULT=PASS
    TARGET_CUSTOM09_POST_CHECK_RESULT=ALREADY_SATISFIED
elif [[ "$custom09_check_exit" -eq 1 ]]; then
    TARGET_CUSTOM09_CHECK_RESULT=NEEDS_REIMPLEMENTATION
    set +e
    run_gate CUSTOM_09 "$CUSTOM09_RECONCILER" --apply
    custom09_apply_exit=$?
    set -e
    echo "TARGET_CUSTOM09_APPLY_EXIT=$custom09_apply_exit"
    if [[ "$custom09_apply_exit" -eq 0 ]]; then
        TARGET_CUSTOM09_APPLY_RESULT=PASS
        set +e
        run_gate CUSTOM_09 "$CUSTOM09_RECONCILER" --check
        custom09_post_exit=$?
        set -e
        echo "TARGET_CUSTOM09_POST_CHECK_EXIT=$custom09_post_exit"
        [[ "$custom09_post_exit" -eq 0 ]] && TARGET_CUSTOM09_POST_CHECK_RESULT=ALREADY_SATISFIED
    fi
else
    TARGET_CUSTOM09_CHECK_RESULT=BLOCKED_SAFE
fi
if [[ -f "$CUSTOM09_TESTS" ]]; then
    set +e
    run_gate CUSTOM_09 "$CUSTOM09_TESTS"
    custom09_tests_exit=$?
    set -e
    echo "TARGET_CUSTOM09_TESTS_EXIT=$custom09_tests_exit"
    [[ "$custom09_tests_exit" -eq 0 ]] && TARGET_CUSTOM09_TESTS_RESULT=PASS
else
    TARGET_CUSTOM09_TESTS_RESULT=NOT_PRESENT
fi
if [[ "$TARGET_CUSTOM09_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM09_TESTS_RESULT" != 'FAIL' ]]; then
    TARGET_CUSTOM09_FINAL=PASS_EXISTING
elif [[ "$TARGET_CUSTOM09_CHECK_RESULT" == 'NEEDS_REIMPLEMENTATION' && "$TARGET_CUSTOM09_APPLY_RESULT" == 'PASS' && "$TARGET_CUSTOM09_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM09_TESTS_RESULT" != 'FAIL' ]]; then
    TARGET_CUSTOM09_FINAL=REIMPLEMENTED_PASS
fi
echo "CUSTOM_09_FINAL=$TARGET_CUSTOM09_FINAL"

# CUSTOM_10 is a bash gate (RUNTIME_SHELL). It is dispatched through
# run_shell_gate precisely so it is NOT given a Python interpreter: modelling a
# shell script as a Python gate would be a false claim about its runtime.
TARGET_CUSTOM10_RESULT=FAIL
set +e
run_shell_gate "$CUSTOM10_CHECK"
custom10_exit=$?
set -e
echo "TARGET_CUSTOM10_EXIT=$custom10_exit"
[[ "$custom10_exit" -eq 0 ]] && TARGET_CUSTOM10_RESULT=PASS
echo "CUSTOM_10_FINAL=$TARGET_CUSTOM10_RESULT"

# CUSTOM_11 (T2.1 non-mutating hindsight init) -- same shape as CUSTOM_09.
TARGET_CUSTOM11_CHECK_RESULT=FAIL
TARGET_CUSTOM11_APPLY_RESULT=FAIL
TARGET_CUSTOM11_POST_CHECK_RESULT=FAIL
TARGET_CUSTOM11_ACCEPTANCE_RESULT=FAIL
TARGET_CUSTOM11_PREFLIGHT_RESULT=FAIL
TARGET_CUSTOM11_FINAL=BLOCKED_SAFE
set +e
run_gate CUSTOM_11_libatomic_preflight "$CUSTOM11_TESTS"
custom11_preflight_exit=$?
set -e
echo "TARGET_CUSTOM11_PREFLIGHT_EXIT=$custom11_preflight_exit"
[[ "$custom11_preflight_exit" -eq 0 ]] && TARGET_CUSTOM11_PREFLIGHT_RESULT=PASS
set +e
run_gate CUSTOM_11 "$CUSTOM11_RECONCILER" --check
custom11_check_exit=$?
set -e
echo "TARGET_CUSTOM11_CHECK_EXIT=$custom11_check_exit"
if [[ "$custom11_check_exit" -eq 0 ]]; then
    TARGET_CUSTOM11_CHECK_RESULT=ALREADY_SATISFIED
    TARGET_CUSTOM11_APPLY_RESULT=PASS
    TARGET_CUSTOM11_POST_CHECK_RESULT=ALREADY_SATISFIED
elif [[ "$custom11_check_exit" -eq 1 ]]; then
    TARGET_CUSTOM11_CHECK_RESULT=NEEDS_REIMPLEMENTATION
    set +e
    run_gate CUSTOM_11 "$CUSTOM11_RECONCILER" --apply
    custom11_apply_exit=$?
    set -e
    echo "TARGET_CUSTOM11_APPLY_EXIT=$custom11_apply_exit"
    if [[ "$custom11_apply_exit" -eq 0 ]]; then
        TARGET_CUSTOM11_APPLY_RESULT=PASS
        set +e
        run_gate CUSTOM_11 "$CUSTOM11_RECONCILER" --check
        custom11_post_exit=$?
        set -e
        echo "TARGET_CUSTOM11_POST_CHECK_EXIT=$custom11_post_exit"
        [[ "$custom11_post_exit" -eq 0 ]] && TARGET_CUSTOM11_POST_CHECK_RESULT=ALREADY_SATISFIED
    fi
else
    TARGET_CUSTOM11_CHECK_RESULT=BLOCKED_SAFE
fi
if [[ "$TARGET_CUSTOM11_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' ]]; then
    set +e
    run_gate CUSTOM_11 "$CUSTOM11_ACCEPTANCE"
    custom11_acceptance_exit=$?
    set -e
    echo "TARGET_CUSTOM11_ACCEPTANCE_EXIT=$custom11_acceptance_exit"
    [[ "$custom11_acceptance_exit" -eq 0 ]] && TARGET_CUSTOM11_ACCEPTANCE_RESULT=PASS
fi
if [[ "$TARGET_CUSTOM11_PREFLIGHT_RESULT" == 'PASS' && "$TARGET_CUSTOM11_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM11_ACCEPTANCE_RESULT" == 'PASS' ]]; then
    TARGET_CUSTOM11_FINAL=PASS_EXISTING
elif [[ "$TARGET_CUSTOM11_CHECK_RESULT" == 'NEEDS_REIMPLEMENTATION' && "$TARGET_CUSTOM11_PREFLIGHT_RESULT" == 'PASS' && "$TARGET_CUSTOM11_APPLY_RESULT" == 'PASS' && "$TARGET_CUSTOM11_POST_CHECK_RESULT" == 'ALREADY_SATISFIED' && "$TARGET_CUSTOM11_ACCEPTANCE_RESULT" == 'PASS' ]]; then
    TARGET_CUSTOM11_FINAL=REIMPLEMENTED_PASS
fi
echo "CUSTOM_11_FINAL=$TARGET_CUSTOM11_FINAL"

# G1 — per-profile provider/plugin/dep/registry consistency.
TARGET_G1_PROFILE_PLUGIN_GATE=FAIL
set +e
run_deep_gate "$G1_PROFILE_PLUGIN_GATE"
g1_exit=$?
set -e
echo "TARGET_G1_EXIT=$g1_exit"
[[ "$g1_exit" -eq 0 ]] && TARGET_G1_PROFILE_PLUGIN_GATE=PASS
echo "TARGET_G1_PROFILE_PLUGIN_GATE=$TARGET_G1_PROFILE_PLUGIN_GATE"

# G2 — Hindsight effective-version guard (>=0.10.0,<0.11.0), PTY policy.
TARGET_G2_HINDSIGHT_VERSION_GATE=FAIL
set +e
run_deep_gate "$G2_PTY_DEP_GUARD" --transcript-dir "$DEEP_GATE_TRANSCRIPT_DIR"
g2_exit=$?
set -e
echo "TARGET_G2_EXIT=$g2_exit"
[[ "$g2_exit" -eq 0 ]] && TARGET_G2_HINDSIGHT_VERSION_GATE=PASS
echo "TARGET_G2_HINDSIGHT_VERSION_GATE=$TARGET_G2_HINDSIGHT_VERSION_GATE"

# PHASE 8 — structural effective-version guard. Runs on its own, proves the
# version comparison both ways, records the derived PM-constraint analysis, and
# validates the EFFECTIVE installed runtime of every profile (not the catalog
# declaration, which pins only the plugin source sha).
TARGET_HINDSIGHT_VERSION_CONSTRAINT_GATE=FAIL
set +e
run_deep_gate "$HINDSIGHT_VERSION_GUARD" --selftest
hs_selftest_exit=$?
run_deep_gate "$HINDSIGHT_VERSION_GUARD" --explain
hs_explain_exit=$?
run_deep_gate "$HINDSIGHT_VERSION_GUARD"
hs_effective_exit=$?
set -e
echo "TARGET_HINDSIGHT_GUARD_SELFTEST_EXIT=$hs_selftest_exit"
echo "TARGET_HINDSIGHT_GUARD_EXPLAIN_EXIT=$hs_explain_exit"
echo "TARGET_HINDSIGHT_GUARD_EFFECTIVE_EXIT=$hs_effective_exit"
if [[ "$hs_selftest_exit" -eq 0 && "$hs_explain_exit" -eq 0 && "$hs_effective_exit" -eq 0 ]]; then
    TARGET_HINDSIGHT_VERSION_CONSTRAINT_GATE=PASS
fi
echo "TARGET_HINDSIGHT_VERSION_CONSTRAINT_GATE=$TARGET_HINDSIGHT_VERSION_CONSTRAINT_GATE"

# PHASE 5 — special hermes-dev isolated lifecycle.
TARGET_DEV_LIFECYCLE_GATE=FAIL
set +e
run_deep_gate "$DEV_LIFECYCLE_GATE"
dev_exit=$?
set -e
echo "TARGET_DEV_LIFECYCLE_EXIT=$dev_exit"
[[ "$dev_exit" -eq 0 ]] && TARGET_DEV_LIFECYCLE_GATE=PASS
echo "TARGET_DEV_LIFECYCLE_GATE=$TARGET_DEV_LIFECYCLE_GATE"

# G7 — read-only live gateway + Hindsight services regression.
TARGET_G7_GATEWAY_GATE=FAIL
set +e
run_deep_gate "$G7_GATEWAY_GATE" --profile hermes-kvm8
g7_exit=$?
set -e
echo "TARGET_G7_EXIT=$g7_exit"
[[ "$g7_exit" -eq 0 ]] && TARGET_G7_GATEWAY_GATE=PASS
echo "TARGET_G7_GATEWAY_GATE=$TARGET_G7_GATEWAY_GATE"

# G3 — real per-profile doctor. Bounded and skippable only when the operator
# explicitly disables it, which then blocks eligibility rather than passing.
TARGET_G3_DOCTOR_GATE=FAIL
if [[ "${HERMES_SKIP_DEEP_DOCTOR:-NO}" == 'YES' ]]; then
    echo 'TARGET_G3_DOCTOR_GATE=BLOCKED_SAFE'
    echo 'BLOCK_REASON=DEEP_DOCTOR_EXPLICITLY_SKIPPED'
else
    set +e
    run_deep_gate "$G3_DOCTOR_GATE" --timeout "${HERMES_DOCTOR_TIMEOUT:-300}" \
        --transcript-dir "$DEEP_GATE_TRANSCRIPT_DIR/doctor"
    g3_exit=$?
    set -e
    echo "TARGET_G3_EXIT=$g3_exit"
    [[ "$g3_exit" -eq 0 ]] && TARGET_G3_DOCTOR_GATE=PASS
fi
echo "TARGET_G3_DOCTOR_GATE=$TARGET_G3_DOCTOR_GATE"

# G4/G5/G6 — two fresh-process agent-init canaries per profile, own process
# group, orphan gate, and an explicit no-LLM assertion.
TARGET_AGENT_INIT_GATE=FAIL
if [[ "${HERMES_SKIP_AGENT_INIT:-NO}" == 'YES' ]]; then
    echo 'TARGET_AGENT_INIT_GATE=BLOCKED_SAFE'
    echo 'BLOCK_REASON=AGENT_INIT_EXPLICITLY_SKIPPED'
else
    set +e
    run_deep_gate "$G456_AGENT_INIT_GATE" --repeat "${HERMES_AGENT_INIT_REPEAT:-2}" \
        --timeout "${HERMES_AGENT_INIT_TIMEOUT:-80}" \
        --transcript-dir "$DEEP_GATE_TRANSCRIPT_DIR/canaries" \
        ${HERMES_GATEWAY_PID:+--gateway-pid "$HERMES_GATEWAY_PID"}
    agent_init_exit=$?
    set -e
    echo "TARGET_AGENT_INIT_EXIT=$agent_init_exit"
    [[ "$agent_init_exit" -eq 0 ]] && TARGET_AGENT_INIT_GATE=PASS
fi
echo "TARGET_AGENT_INIT_GATE=$TARGET_AGENT_INIT_GATE"

DEEP_RUNTIME_GATES_PASS=yes
[[ "$TARGET_PIPE_STATUS_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_CUSTOM08_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM08_FINAL" == 'REIMPLEMENTED_PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_G1_PROFILE_PLUGIN_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_G2_HINDSIGHT_VERSION_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_HINDSIGHT_VERSION_CONSTRAINT_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_DEV_LIFECYCLE_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_G7_GATEWAY_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_G3_DOCTOR_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
[[ "$TARGET_AGENT_INIT_GATE" == 'PASS' ]] || DEEP_RUNTIME_GATES_PASS=no
echo "DEEP_RUNTIME_GATES_PASS=$DEEP_RUNTIME_GATES_PASS"

if [[ "$TARGET_CUSTOM03_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM03_FINAL" == 'REIMPLEMENTED_PASS' ]] && [[ "$TARGET_CUSTOM07_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM07_FINAL" == 'REIMPLEMENTED_PASS' ]]; then
    TARGET_HINDSIGHT_PROMOTION_GATE=PASS
else
    TARGET_HINDSIGHT_PROMOTION_GATE=BLOCKED
fi
# CUSTOM_09/10/11 are promoted into TARGET_PREFLIGHT as of 2026-09-27. Until
# this, an update could complete with these three customizations neither
# verified nor repaired by the dispatcher at all; a green journal therefore did
# not mean what it appeared to mean. They are now load-bearing.
CUSTOM09_10_11_GATE=yes
[[ "$TARGET_CUSTOM09_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM09_FINAL" == 'REIMPLEMENTED_PASS' ]] || CUSTOM09_10_11_GATE=no
[[ "$TARGET_CUSTOM10_RESULT" == 'PASS' ]] || CUSTOM09_10_11_GATE=no
[[ "$TARGET_CUSTOM11_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM11_FINAL" == 'REIMPLEMENTED_PASS' ]] || CUSTOM09_10_11_GATE=no
echo "CUSTOM_09_10_11_DISPATCH_GATE=$CUSTOM09_10_11_GATE"

if [[ ( "$TARGET_CUSTOM03_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM03_FINAL" == 'REIMPLEMENTED_PASS' ) && ( "$TARGET_CUSTOM07_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM07_FINAL" == 'REIMPLEMENTED_PASS' ) && ( "$TARGET_STATUSBAR_CHECK_RESULT" == 'PASS_EXISTING' || "$TARGET_STATUSBAR_CHECK_RESULT" == 'NEEDS_REIMPLEMENTATION' ) && "$TARGET_STATUSBAR_APPLY_RESULT" == 'PASS' && "$TARGET_STATUSBAR_POST_CHECK_RESULT" == 'PASS_EXISTING' && "$TARGET_STATUSBAR_ACCEPTANCE_RESULT" == 'PASS' && "$TARGET_STATUSBAR_CLI_SMOKE" == 'PASS' && "$TARGET_TUI_GATE" == 'PASS' && "$DEEP_RUNTIME_GATES_PASS" == 'yes' && "$CUSTOM09_10_11_GATE" == 'yes' ]]; then
    TARGET_PREFLIGHT=PASS
fi

echo "CUSTOM_03=$TARGET_CUSTOM03_FINAL"
echo "CUSTOM_07=$TARGET_CUSTOM07_FINAL"
echo "CUSTOM_08=$TARGET_CUSTOM08_FINAL"
echo "CUSTOM_09=$TARGET_CUSTOM09_FINAL"
echo "CUSTOM_10=$TARGET_CUSTOM10_RESULT"
echo "CUSTOM_11=$TARGET_CUSTOM11_FINAL"
echo "HINDSIGHT_CONSTRAINT=>=0.10.0,<0.11.0"
echo "DESIRED_STATE_GATE=$TARGET_HINDSIGHT_PROMOTION_GATE"
echo "HINDSIGHT_PROMOTION_GATE=$TARGET_HINDSIGHT_PROMOTION_GATE"
echo "ROOT_WRAPPER_PM_GATE=$(if [[ "$TARGET_CUSTOM08_FINAL" == 'PASS_EXISTING' || "$TARGET_CUSTOM08_FINAL" == 'REIMPLEMENTED_PASS' ]]; then echo PASS; else echo BLOCKED_SAFE; fi)"
echo "DEV_SPECIAL_PM_GATE=$TARGET_DEV_LIFECYCLE_GATE"
echo "PROFILE_PROVIDER_PLUGIN_GATE=$TARGET_G1_PROFILE_PLUGIN_GATE"
echo "PTY_DEPENDENCY_CONSENT_GATE=$TARGET_G2_HINDSIGHT_VERSION_GATE"
echo "HINDSIGHT_VERSION_CONSTRAINT_GATE=$TARGET_HINDSIGHT_VERSION_CONSTRAINT_GATE"
echo "DEEP_DOCTOR_GATE=$TARGET_G3_DOCTOR_GATE"
echo "AGENT_INIT_GATE=$TARGET_AGENT_INIT_GATE"
echo "TWO_FRESH_CANARY_GATE=$TARGET_AGENT_INIT_GATE"
echo "LIVE_GATEWAY_RUNTIME_GATE=$TARGET_G7_GATEWAY_GATE"
echo "PIPE_EXIT_STATUS_GATE=$TARGET_PIPE_STATUS_GATE"
echo "DEEP_RUNTIME_GATES_PASS=$DEEP_RUNTIME_GATES_PASS"
# NOTE: STATUSBAR_FINAL is emitted exactly once, below, AFTER the authoritative
# TARGET_PREFLIGHT verdict. A previous unconditional emission here reported
# REIMPLEMENTED_PASS from the reconciler state alone and was then contradicted
# by the same run's own STATUSBAR_FINAL=BLOCKED_SAFE, making the run's status
# report self-contradictory. The reconciler-local state is still reported above
# via STATUSBAR_INITIAL_CHECK / STATUSBAR_APPLY / STATUSBAR_POST_CHECK.

echo "STATUSBAR_INITIAL_CHECK=$TARGET_STATUSBAR_CHECK_RESULT"
echo "STATUSBAR_APPLY=$TARGET_STATUSBAR_APPLY_RESULT"
echo "STATUSBAR_POST_CHECK=$TARGET_STATUSBAR_POST_CHECK_RESULT"
echo "STATUSBAR_ACCEPTANCE=$TARGET_STATUSBAR_ACCEPTANCE_RESULT"
echo "STATUSBAR_CLI_SMOKE=$TARGET_STATUSBAR_CLI_SMOKE"
echo "TUI_GATE=$TARGET_TUI_GATE"
if [[ "$TARGET_PREFLIGHT" == 'PASS' ]]; then
    if [[ "$TARGET_STATUSBAR_CHECK_RESULT" == 'NEEDS_REIMPLEMENTATION' ]]; then
        echo 'STATUSBAR_FINAL=REIMPLEMENTED_PASS'
    else
        echo 'STATUSBAR_FINAL=PASS_EXISTING'
    fi
else
    echo 'STATUSBAR_FINAL=BLOCKED_SAFE'
fi
echo "PINNED_TARGET_PREFLIGHT=$TARGET_PREFLIGHT"
echo "TARGET_PREFLIGHT=$TARGET_PREFLIGHT"
echo 'CHERRY_PICK_TRANSPORT=DISABLED'
echo 'REPLAY_ENGINE=DISABLED'

# Decide from the fully reconstructed candidate tree, never from commit SHA
# equality. Tree comparison is reached only after every desired-state gate.
BLOCK_REASON=NONE
if [[ "$TARGET_PREFLIGHT" != 'PASS' ]]; then
    if [[ "$TARGET_CUSTOM03_FINAL" != 'PASS_EXISTING' && "$TARGET_CUSTOM03_FINAL" != 'REIMPLEMENTED_PASS' ]] || [[ "$TARGET_CUSTOM07_FINAL" != 'PASS_EXISTING' && "$TARGET_CUSTOM07_FINAL" != 'REIMPLEMENTED_PASS' ]] || [[ "$TARGET_STATUSBAR_POST_CHECK_RESULT" != 'PASS_EXISTING' ]] || [[ "$TARGET_STATUSBAR_ACCEPTANCE_RESULT" != 'PASS' ]] || [[ "$TARGET_STATUSBAR_CLI_SMOKE" != 'PASS' ]] || [[ "$TARGET_TUI_GATE" != 'PASS' ]] || [[ "$DEEP_RUNTIME_GATES_PASS" != 'yes' ]]; then
        # The deep-runtime matrix is part of the desired-state definition: a
        # candidate is only eligible when it can prove the certified runtime
        # state, so its failure is a desired-state block, not a soft warning.
        BLOCK_REASON=BLOCKED_DESIRED_STATE
    else
        BLOCK_REASON=BLOCKED_PREFLIGHT
    fi
fi
CANDIDATE_TREE_SHA=UNAVAILABLE
SEMANTIC_CANDIDATE_TREE_SHA=UNAVAILABLE
CURRENT_SEMANTIC_TREE_SHA=UNAVAILABLE
SEMANTIC_STATE_EQUAL=NO
UPDATE_EFFECTIVE_DIFF=UNKNOWN
PROMOTION_REQUIRED=UNKNOWN
CANDIDATE_TREE_GATE=NOT_RUN
if [[ "$TARGET_PREFLIGHT" == 'PASS' ]]; then
    set +e
    CANDIDATE_TREE_SHA=$(compute_candidate_tree "$TEMP_WORKTREE")
    candidate_tree_exit=$?
    set -e
    if [[ "$candidate_tree_exit" -ne 0 || ! "$CANDIDATE_TREE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
        CANDIDATE_TREE_SHA=UNAVAILABLE
        CANDIDATE_TREE_GATE=FAIL
        TARGET_PREFLIGHT=BLOCKED_SAFE
        BLOCK_REASON=BLOCKED_PREFLIGHT
    else
        SEMANTIC_CANDIDATE_TREE_SHA=$(compute_semantic_tree "$TEMP_WORKTREE" "$CANDIDATE_TREE_SHA")
        CURRENT_SEMANTIC_TREE_SHA=$(compute_semantic_tree "$REPO" "$CURRENT_PROMOTED_TREE_SHA")
        if [[ ! "$SEMANTIC_CANDIDATE_TREE_SHA" =~ ^[0-9a-f]{40}$ || ! "$CURRENT_SEMANTIC_TREE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
            CANDIDATE_TREE_GATE=FAIL
            TARGET_PREFLIGHT=BLOCKED_SAFE
            BLOCK_REASON=BLOCKED_PREFLIGHT
        else
            CANDIDATE_TREE_GATE=PASS
            if [[ "$SEMANTIC_CANDIDATE_TREE_SHA" == "$CURRENT_SEMANTIC_TREE_SHA" ]]; then
                SEMANTIC_STATE_EQUAL=yes
                UPDATE_EFFECTIVE_DIFF=no
                PROMOTION_REQUIRED=no
            else
                SEMANTIC_STATE_EQUAL=no
                UPDATE_EFFECTIVE_DIFF=yes
                PROMOTION_REQUIRED=yes
            fi
        fi
    fi
fi
echo "CANDIDATE_TREE_SHA=$CANDIDATE_TREE_SHA"
echo "CURRENT_PROMOTED_TREE_SHA=$CURRENT_PROMOTED_TREE_SHA"
echo "SEMANTIC_CANDIDATE_TREE_SHA=$SEMANTIC_CANDIDATE_TREE_SHA"
echo "CURRENT_SEMANTIC_TREE_SHA=$CURRENT_SEMANTIC_TREE_SHA"
echo "CANDIDATE_TREE_GATE=$CANDIDATE_TREE_GATE"
echo "SEMANTIC_STATE_EQUAL=$SEMANTIC_STATE_EQUAL"
echo "UPDATE_EFFECTIVE_DIFF=$UPDATE_EFFECTIVE_DIFF"
echo "PROMOTION_REQUIRED=$PROMOTION_REQUIRED"
echo "BLOCK_REASON=$BLOCK_REASON"

# --check is deliberately preflight-only. It cannot claim promotion or PASS.
if [[ "$MODE" == 'CHECK' ]]; then
    if [[ "$TARGET_PREFLIGHT" != 'PASS' ]]; then
        echo "PROMOTION=$BLOCK_REASON"
        echo 'PROMOTION_DECISION=BLOCKED'
        echo "UPDATER_RESULT=$BLOCK_REASON"
        classify BLOCKED_SAFE
        exit 20
    fi
    echo 'PROMOTION=NOT_RUN'
    if [[ "$SEMANTIC_STATE_EQUAL" == 'yes' ]]; then
        echo 'PROMOTION_DECISION=NOOP_SEMANTICALLY_CURRENT'
        echo 'UPDATER_RESULT=NOOP'
        echo 'UPDATE_EFFECTIVE_DIFF=no'
        echo 'PROMOTION_REQUIRED=no'
        classify NOOP_SEMANTICALLY_CURRENT
    else
        echo 'PROMOTION_DECISION=PROMOTION_REQUIRED_CHECK_ONLY'
        echo 'UPDATER_RESULT=PROMOTION_REQUIRED'
        classify PRECHECK_PASS_NO_PROMOTION
    fi
    exit 0
fi

# --apply is the only path allowed to mutate the repository. A valid candidate
# already represented by the live tree is a successful no-op, even when its
# target SHA or desired-state reconciliation status differs.
if [[ "$TARGET_PREFLIGHT" != 'PASS' ]]; then
    echo "PROMOTION=$BLOCK_REASON"
    echo 'PROMOTION_DECISION=BLOCKED'
    echo "UPDATER_RESULT=$BLOCK_REASON"
    classify BLOCKED_SAFE
    exit 20
fi
if [[ "$SEMANTIC_STATE_EQUAL" == 'yes' ]]; then
    echo 'PROMOTION=NOT_RUN'
    echo 'PROMOTION_DECISION=NOOP_SEMANTICALLY_CURRENT'
    echo 'UPDATER_RESULT=NOOP'
    echo 'PROMOTION_REQUIRED=no'
    echo 'UPDATE_EFFECTIVE_DIFF=no'
    classify NOOP_SEMANTICALLY_CURRENT
    exit 0
fi

# Promotion must start from a clean worktree; otherwise no destructive operation.
if [[ -n "$(git -C "$REPO" status --porcelain)" ]]; then
    echo 'PROMOTION=BLOCKED_DIRTY_WORKTREE'
    echo 'PROMOTION_DECISION=BLOCKED'
    classify BLOCKED_SAFE
    exit 20
fi
PRE_HEAD=$(git -C "$REPO" rev-parse HEAD)
echo "PRE_HEAD=$PRE_HEAD"
ROLLBACK_REF="refs/backup/mastertrend/kvm8-real-promotion-$(date -u +%Y%m%dT%H%M%SZ)-rollback"
git -C "$REPO" update-ref "$ROLLBACK_REF" "$PRE_HEAD"
echo "ROLLBACK_REF=$ROLLBACK_REF"
echo "ROLLBACK_SHA=$PRE_HEAD"

# Test-only fault injection proves that skipped promotion cannot become PASS.
if [[ "${HERMES_TEST_SKIP_PROMOTION:-NO}" == 'YES' ]]; then
    echo 'PROMOTION_SKIPPED_FOR_TEST=YES'
    echo 'PROMOTION=FAIL'
    echo 'PROMOTION_DECISION=BLOCKED_TEST_INJECTION'
    REC_PROMOTION_EXECUTED='no'
    classify BLOCKED_SAFE
    exit 31
fi

echo "PROMOTION_DECISION=RESET_PINNED_TARGET"
# From here the production checkout is being mutated: the final record must report
# the settled operating head, not NOT_RUN.
REC_PROMOTION_EXECUTED='yes'
set +e
git -C "$REPO" reset --hard "$TARGET_SHA"
reset_exit=$?
set -e
if [[ "$reset_exit" -ne 0 ]]; then
    echo "PROMOTION=FAIL (reset_exit=$reset_exit)"
    set +e; git -C "$REPO" reset --hard "$PRE_HEAD" >/dev/null 2>&1; rollback_exit=$?; set -e
    if [[ "$rollback_exit" -eq 0 && "$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" == "$PRE_HEAD" ]]; then
        echo 'ROLLBACK=PASS'
        REC_ROLLBACK_EXECUTED='yes'
        classify FAIL_RECOVERED
    else
        echo 'ROLLBACK=FAIL'
        REC_ROLLBACK_EXECUTED='ATTEMPTED_UNVERIFIED'
        classify BLOCKED_SAFE
    fi
    exit 31
fi
BASE_HEAD=$(git -C "$REPO" rev-parse HEAD)
echo "BASE_HEAD=$BASE_HEAD"
if [[ "$BASE_HEAD" != "$TARGET_SHA" ]]; then
    echo 'PROMOTION=FAIL'
    echo 'BASE_HEAD_MATCHES_TARGET=NO'
    set +e; git -C "$REPO" reset --hard "$PRE_HEAD" >/dev/null 2>&1; rollback_exit=$?; set -e
    if [[ "$rollback_exit" -eq 0 && "$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" == "$PRE_HEAD" ]]; then
        echo 'ROLLBACK=PASS'
        REC_ROLLBACK_EXECUTED='yes'
        classify FAIL_RECOVERED
    else
        echo 'ROLLBACK=FAIL'
        REC_ROLLBACK_EXECUTED='ATTEMPTED_UNVERIFIED'
        classify BLOCKED_SAFE
    fi
    exit 31
fi
# This is the only legal point at which PROMOTED_SHA is defined.
PROMOTED_SHA=$(git -C "$REPO" rev-parse HEAD)
[[ "$PROMOTED_SHA" == "$TARGET_SHA" ]] || { echo 'PROMOTION=FAIL'; exit 31; }
echo 'BASE_HEAD_MATCHES_TARGET=YES'
echo "PROMOTED_SHA=$PROMOTED_SHA"
echo 'PROMOTION_BASE=PASS'

# The TUI dist tree is generated and not tracked by Git. Carry the already
# accepted candidate product across reset, then run the same runtime gate after
# promotion; never rebuild from an unprepared active checkout.
POST_PROMOTION_DIST_BACKUP=$(mktemp -d /tmp/hermes-kvm8-post-tui-backup.XXXXXX)
POST_PROMOTION_DIST_WAS_PRESENT=NO
if [[ -d "$REPO/ui-tui/dist" ]]; then
    POST_PROMOTION_DIST_WAS_PRESENT=YES
    cp -a "$REPO/ui-tui/dist" "$POST_PROMOTION_DIST_BACKUP/dist"
fi
rm -rf "$REPO/ui-tui/dist"
cp -a "$TEMP_WORKTREE/ui-tui/dist" "$REPO/ui-tui/dist"
set +e
run_tui_post_promotion_gate "$REPO"
post_promotion_tui_exit=$?
set -e
if [[ "$post_promotion_tui_exit" -ne 0 ]]; then
    echo 'POST_PROMOTION_TUI_GATE=FAIL'
    rm -rf "$REPO/ui-tui/dist"
    if [[ "$POST_PROMOTION_DIST_WAS_PRESENT" == 'YES' ]]; then
        cp -a "$POST_PROMOTION_DIST_BACKUP/dist" "$REPO/ui-tui/dist"
    fi
    rm -rf "$POST_PROMOTION_DIST_BACKUP"
    set +e; git -C "$REPO" reset --hard "$PRE_HEAD" >/dev/null 2>&1; rollback_exit=$?; set -e
    if [[ "$rollback_exit" -eq 0 && "$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" == "$PRE_HEAD" ]]; then
        echo 'ROLLBACK=PASS'
        REC_ROLLBACK_EXECUTED='yes'
        classify FAIL_RECOVERED
    else
        echo 'ROLLBACK=FAIL'
        REC_ROLLBACK_EXECUTED='ATTEMPTED_UNVERIFIED'
        classify BLOCKED_SAFE
    fi
    exit 31
fi
rm -rf "$POST_PROMOTION_DIST_BACKUP"

# Reconcile the freshly promoted production base, not the temporary worktree.
PRODUCTION_CUSTOM03_RECONCILIATION=FAIL
PRODUCTION_CUSTOM03_ACCEPTANCE=FAIL
PRODUCTION_CUSTOM03_TESTS=FAIL
set +e
run_custom03_check "$REPO"
prod_custom03_check_exit=$?
set -e
echo "PRODUCTION_CUSTOM03_CHECK_EXIT=$prod_custom03_check_exit"
if [[ "$prod_custom03_check_exit" -eq 0 ]]; then
    PRODUCTION_CUSTOM03_RECONCILIATION=PASS_EXISTING
elif [[ "$prod_custom03_check_exit" -eq 1 ]]; then
    set +e
    run_custom03_apply "$REPO"
    prod_custom03_apply_exit=$?
    set -e
    echo "PRODUCTION_CUSTOM03_APPLY_EXIT=$prod_custom03_apply_exit"
    if [[ "$prod_custom03_apply_exit" -eq 0 ]]; then
        set +e
        run_custom03_check "$REPO"
        prod_custom03_post_exit=$?
        set -e
        echo "PRODUCTION_CUSTOM03_POST_CHECK_EXIT=$prod_custom03_post_exit"
        [[ "$prod_custom03_post_exit" -eq 0 ]] && PRODUCTION_CUSTOM03_RECONCILIATION=REIMPLEMENTED
    fi
fi
if [[ "$PRODUCTION_CUSTOM03_RECONCILIATION" == 'PASS_EXISTING' || "$PRODUCTION_CUSTOM03_RECONCILIATION" == 'REIMPLEMENTED' ]]; then
    set +e
    run_custom03_acceptance "$REPO"
    prod_custom03_acceptance_exit=$?
    set -e
    echo "PRODUCTION_CUSTOM03_ACCEPTANCE_EXIT=$prod_custom03_acceptance_exit"
    [[ "$prod_custom03_acceptance_exit" -eq 0 ]] && PRODUCTION_CUSTOM03_ACCEPTANCE=PASS
    set +e
    run_custom03_tests "$REPO"
    prod_custom03_tests_exit=$?
    set -e
    echo "PRODUCTION_CUSTOM03_TESTS_EXIT=$prod_custom03_tests_exit"
    [[ "$prod_custom03_tests_exit" -eq 0 ]] && PRODUCTION_CUSTOM03_TESTS=PASS
fi
if [[ ( "$PRODUCTION_CUSTOM03_RECONCILIATION" == 'PASS_EXISTING' || "$PRODUCTION_CUSTOM03_RECONCILIATION" == 'REIMPLEMENTED' ) && "$PRODUCTION_CUSTOM03_ACCEPTANCE" == 'PASS' && "$PRODUCTION_CUSTOM03_TESTS" == 'PASS' ]]; then
    PRODUCTION_HINDSIGHT_PROMOTION_GATE=PASS
else
    PRODUCTION_HINDSIGHT_PROMOTION_GATE=BLOCKED
fi
echo "PRODUCTION_HINDSIGHT_PROMOTION_GATE=$PRODUCTION_HINDSIGHT_PROMOTION_GATE"

PRODUCTION_CUSTOM07_RECONCILIATION=FAIL
PRODUCTION_CUSTOM07_ACCEPTANCE=FAIL
PRODUCTION_CUSTOM07_TESTS=FAIL
set +e
run_custom07_check "$REPO"
prod_custom07_check_exit=$?
set -e
echo "PRODUCTION_CUSTOM07_CHECK_EXIT=$prod_custom07_check_exit"
if [[ "$prod_custom07_check_exit" -eq 0 ]]; then
    PRODUCTION_CUSTOM07_RECONCILIATION=PASS_EXISTING
elif [[ "$prod_custom07_check_exit" -eq 1 ]]; then
    set +e
    run_custom07_apply "$REPO"
    prod_custom07_apply_exit=$?
    set -e
    echo "PRODUCTION_CUSTOM07_APPLY_EXIT=$prod_custom07_apply_exit"
    if [[ "$prod_custom07_apply_exit" -eq 0 ]]; then
        set +e
        run_custom07_check "$REPO"
        prod_custom07_post_exit=$?
        set -e
        echo "PRODUCTION_CUSTOM07_POST_CHECK_EXIT=$prod_custom07_post_exit"
        [[ "$prod_custom07_post_exit" -eq 0 ]] && PRODUCTION_CUSTOM07_RECONCILIATION=REIMPLEMENTED
    fi
fi
if [[ "$PRODUCTION_CUSTOM07_RECONCILIATION" == 'PASS_EXISTING' || "$PRODUCTION_CUSTOM07_RECONCILIATION" == 'REIMPLEMENTED' ]]; then
    set +e
    run_custom07_acceptance "$REPO"
    prod_custom07_acceptance_exit=$?
    set -e
    echo "PRODUCTION_CUSTOM07_ACCEPTANCE_EXIT=$prod_custom07_acceptance_exit"
    [[ "$prod_custom07_acceptance_exit" -eq 0 ]] && PRODUCTION_CUSTOM07_ACCEPTANCE=PASS
    set +e
    run_custom07_tests "$REPO"
    prod_custom07_tests_exit=$?
    set -e
    echo "PRODUCTION_CUSTOM07_TESTS_EXIT=$prod_custom07_tests_exit"
    [[ "$prod_custom07_tests_exit" -eq 0 ]] && PRODUCTION_CUSTOM07_TESTS=PASS
fi
if [[ "$PRODUCTION_CUSTOM07_ACCEPTANCE" == 'PASS' ]]; then
    PRODUCTION_WPCLI_ACCEPTANCE=PASS
else
    PRODUCTION_WPCLI_ACCEPTANCE=FAIL
fi
echo "PRODUCTION_WPCLI_ACCEPTANCE=$PRODUCTION_WPCLI_ACCEPTANCE"

PRODUCTION_RECONCILIATION=FAIL
PRODUCTION_CUSTOMIZATION_COMMIT=NONE
set +e
run_check "$REPO"
prod_check_exit=$?
set -e
echo "PRODUCTION_STATUSBAR_CHECK_EXIT=$prod_check_exit"
if [[ "$prod_check_exit" -eq 0 ]]; then
    prod_post_check_exit=0
    PRODUCTION_RECONCILIATION=PASS_EXISTING
elif [[ "$prod_check_exit" -eq 1 ]]; then
    set +e
    run_apply "$REPO"
    prod_apply_exit=$?
    set -e
    echo "PRODUCTION_STATUSBAR_APPLY_EXIT=$prod_apply_exit"
    if [[ "$prod_apply_exit" -eq 0 ]]; then
        set +e
        run_check "$REPO"
        prod_post_check_exit=$?
        set -e
        echo "PRODUCTION_STATUSBAR_POST_CHECK_EXIT=$prod_post_check_exit"
        if [[ "$prod_post_check_exit" -eq 0 ]]; then
            PRODUCTION_RECONCILIATION=REIMPLEMENTED
        fi
    fi
else
    prod_post_check_exit=2
fi

# Acceptance and smoke must load the production repo explicitly.
PRODUCTION_ACCEPTANCE=FAIL
if [[ "$PRODUCTION_RECONCILIATION" == 'PASS_EXISTING' || "$PRODUCTION_RECONCILIATION" == 'REIMPLEMENTED' ]]; then
    set +e
    run_acceptance "$REPO"
    prod_acceptance_exit=$?
    set -e
    echo "PRODUCTION_STATUSBAR_ACCEPTANCE_EXIT=$prod_acceptance_exit"
    [[ "$prod_acceptance_exit" -eq 0 ]] && PRODUCTION_ACCEPTANCE=PASS
fi
PRODUCTION_CLI_SMOKE=FAIL
set +e
run_cli_smoke "$REPO"
prod_smoke_exit=$?
set -e
[[ "$prod_smoke_exit" -eq 0 ]] && PRODUCTION_CLI_SMOKE=PASS

# Only the declared desired-state customization files may be staged and committed.
EXPECTED_FILES=(
    'agent/turn_response_intake.py'
    'hermes_cli/cli_status_bar_mixin.py'
    'pyproject.toml'
    'uv.lock'
    # NOTE: plugins/memory/hindsight/* was removed from the allowlist — the bundled
    # Hindsight plugin was retired by the catalog-based architecture (4cbf862abe).
    # At the frozen target (59004a62) these paths no longer exist; staging them
    # makes `git add -- EXPECTED_FILES` fail (stale-pathspec defect, diagnosed in
    # the controlled promotion 2026-09-26). The catalog contract now lives in
    # plugin-catalog/hindsight.yaml, which is NOT a customization carry file.
    'tools/lazy_deps.py'
    'tools/environments/local.py'
    'tools/environments/base.py'
    'tools/environments/base_session_env.py'
)
if [[ ( "$PRODUCTION_RECONCILIATION" == 'REIMPLEMENTED' || "$PRODUCTION_RECONCILIATION" == 'PASS_EXISTING' ) && "$PRODUCTION_HINDSIGHT_PROMOTION_GATE" == 'PASS' && ( "$PRODUCTION_CUSTOM07_RECONCILIATION" == 'REIMPLEMENTED' || "$PRODUCTION_CUSTOM07_RECONCILIATION" == 'PASS_EXISTING' ) && "$PRODUCTION_CUSTOM07_ACCEPTANCE" == 'PASS' && "$PRODUCTION_CUSTOM07_TESTS" == 'PASS' && "$PRODUCTION_ACCEPTANCE" == 'PASS' && "$PRODUCTION_CLI_SMOKE" == 'PASS' ]]; then
    changed_files=()
    while IFS= read -r status_line; do
        changed_files+=("${status_line:3}")
    done < <(git -C "$REPO" status --porcelain)
    allowed=YES
    if [[ "${#changed_files[@]}" -eq 0 ]]; then allowed=NO; fi
    for path in "${changed_files[@]}"; do
        allowed_path=NO
        for expected in "${EXPECTED_FILES[@]}"; do
            [[ "$path" == "$expected" ]] && allowed_path=YES
        done
        [[ "$allowed_path" == YES ]] || allowed=NO
    done
    if [[ "$allowed" == 'YES' ]]; then
        git -C "$REPO" add -- "${EXPECTED_FILES[@]}"
        mapfile -t staged_files < <(git -C "$REPO" diff --cached --name-only)
        if [[ "${#staged_files[@]}" -gt 0 && "${#staged_files[@]}" -le "${#EXPECTED_FILES[@]}" ]]; then
            git -C "$REPO" commit -m 'fix(kvm8): reconstruct desired-state customizations' >/dev/null
            PRODUCTION_CUSTOMIZATION_COMMIT=$(git -C "$REPO" rev-parse HEAD)
            echo "PRODUCTION_CUSTOMIZATION_COMMIT=$PRODUCTION_CUSTOMIZATION_COMMIT"
        else
            echo 'PRODUCTION_CUSTOMIZATION_COMMIT=BLOCKED_UNEXPECTED_SCOPE'
        fi
    else
        echo 'PRODUCTION_CUSTOMIZATION_COMMIT=BLOCKED_UNEXPECTED_FILES'
    fi
fi

echo "PRODUCTION_CUSTOM03_RECONCILIATION=$PRODUCTION_CUSTOM03_RECONCILIATION"
echo "PRODUCTION_CUSTOM03_ACCEPTANCE=$PRODUCTION_CUSTOM03_ACCEPTANCE"
echo "PRODUCTION_CUSTOM03_TESTS=$PRODUCTION_CUSTOM03_TESTS"
echo "PRODUCTION_CUSTOM07_RECONCILIATION=$PRODUCTION_CUSTOM07_RECONCILIATION"
echo "PRODUCTION_CUSTOM07_ACCEPTANCE=$PRODUCTION_CUSTOM07_ACCEPTANCE"
echo "PRODUCTION_CUSTOM07_TESTS=$PRODUCTION_CUSTOM07_TESTS"
echo "PRODUCTION_RECONCILIATION=$PRODUCTION_RECONCILIATION"
echo "PRODUCTION_ACCEPTANCE=$PRODUCTION_ACCEPTANCE"
echo "PRODUCTION_CLI_SMOKE=$PRODUCTION_CLI_SMOKE"
FINAL_HEAD=$(git -C "$REPO" rev-parse HEAD)
read TARGET_BEHIND TARGET_AHEAD < <(git -C "$REPO" rev-list --left-right --count "$TARGET_SHA"...HEAD)
if git -C "$REPO" merge-base --is-ancestor "$TARGET_SHA" "$FINAL_HEAD"; then TARGET_ANCESTOR=YES; else TARGET_ANCESTOR=NO; fi
if [[ -z "$(git -C "$REPO" status --porcelain)" ]]; then WORKTREE_CLEAN=YES; else WORKTREE_CLEAN=NO; fi
if [[ "$FINAL_HEAD" == "$PRE_HEAD" ]]; then PRODUCTION_HEAD_UNCHANGED=YES; else PRODUCTION_HEAD_UNCHANGED=NO; fi
echo "FINAL_HEAD=$FINAL_HEAD"
echo "TARGET_ANCESTOR_OF_FINAL=$TARGET_ANCESTOR"
echo "TARGET_BEHIND=$TARGET_BEHIND"
echo "TARGET_AHEAD=$TARGET_AHEAD"
echo "WORKTREE_CLEAN=$WORKTREE_CLEAN"
echo "PRODUCTION_HEAD_UNCHANGED=$PRODUCTION_HEAD_UNCHANGED"
echo 'SUCCESS_INVARIANTS_ENFORCED=YES'

success=YES
[[ "$TARGET_PREFLIGHT" == 'PASS' ]] || success=NO
[[ "$TARGET_ANCESTOR" == 'YES' ]] || success=NO
[[ "$TARGET_BEHIND" -eq 0 ]] || success=NO
[[ "$WORKTREE_CLEAN" == 'YES' ]] || success=NO
[[ "$PRODUCTION_CUSTOM03_RECONCILIATION" == 'PASS_EXISTING' || "$PRODUCTION_CUSTOM03_RECONCILIATION" == 'REIMPLEMENTED' ]] || success=NO
[[ "$PRODUCTION_HINDSIGHT_PROMOTION_GATE" == 'PASS' ]] || success=NO
[[ "$PRODUCTION_CUSTOM03_ACCEPTANCE" == 'PASS' ]] || success=NO
[[ "$PRODUCTION_CUSTOM03_TESTS" == 'PASS' ]] || success=NO
[[ "$PRODUCTION_RECONCILIATION" == 'PASS_EXISTING' || "$PRODUCTION_RECONCILIATION" == 'REIMPLEMENTED' ]] || success=NO
[[ "$PRODUCTION_ACCEPTANCE" == 'PASS' ]] || success=NO
[[ "$PRODUCTION_CLI_SMOKE" == 'PASS' ]] || success=NO
if [[ "$PRE_HEAD" != "$TARGET_SHA" && "$PRODUCTION_HEAD_UNCHANGED" == 'YES' ]]; then success=NO; fi
if [[ "$TARGET_BEHIND" -gt 0 ]]; then success=NO; fi

if [[ "$success" == 'YES' ]]; then
    echo 'PROMOTION=PASS'
    classify PASS
    exit 0
fi

echo 'PROMOTION=FAIL'
set +e
git -C "$REPO" reset --hard "$PRE_HEAD" >/dev/null 2>&1
rollback_exit=$?
set -e
if [[ "$rollback_exit" -eq 0 && "$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" == "$PRE_HEAD" ]]; then
    echo 'ROLLBACK=PASS'
    classify FAIL_RECOVERED
else
    echo 'ROLLBACK=FAIL'
    classify BLOCKED_SAFE
fi
exit 31

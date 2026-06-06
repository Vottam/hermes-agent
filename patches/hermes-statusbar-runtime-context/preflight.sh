#!/usr/bin/env bash
# Hermes Status Bar Runtime Context — Preflight validation
# Purpose: validates environment before applying the patch
# Safety: read-only, does not modify any files
# Does not modify: secrets, auth, Hindsight, gateways, provider routing
# Usage: ./preflight.sh
set -euo pipefail

PASS=0
FAIL=0
WARN=0

ok()  { echo "  [PASS] $1"; PASS=$((PASS+1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN+1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=== Preflight: hermes-statusbar-runtime-context ==="
echo ""

# 1. Hermes instalado
echo "-- Hermes --"
if command -v hermes &>/dev/null; then
    HERMES_PATH=$(readlink -f "$(which hermes)")
    ok "Hermes encontrado: ${HERMES_PATH}"
else
    fail "Hermes não encontrado no PATH"
fi

# 2. Versão do Hermes
echo "-- Versão --"
HERMES_VERSION=$(hermes --version 2>&1 | head -1)
echo "  ${HERMES_VERSION}"
if echo "${HERMES_VERSION}" | grep -qE "v0\.(1[6-9]|[2-9][0-9])\."; then
    ok "Versão v0.16.0+ detectada"
elif echo "${HERMES_VERSION}" | grep -qE "v0\.1[0-5]\."; then
    warn "Versão < v0.16.0 — patch pode precisar ajustes manuais"
else
    fail "Versão não reconhecida"
fi

# 3. Diretório do projeto
echo "-- Diretório --"
HERMES_DIR=""
if [ -n "${HERMES_PATH:-}" ]; then
    # Resolve symlink chain: ~/.local/bin/hermes -> venv/bin/hermes -> project root
    # Need 3x dirname: venv/bin/hermes -> venv/bin -> venv -> project
    HERMES_DIR=$(dirname "$(dirname "$(dirname "${HERMES_PATH}")")")
fi
if [ -d "${HERMES_DIR}/agent" ] && [ -f "${HERMES_DIR}/cli.py" ]; then
    ok "Diretório do projeto: ${HERMES_DIR}"
else
    fail "Diretório do projeto não encontrado (procurando agent/ e cli.py)"
fi

# 4. Git disponível
echo "-- Git --"
if command -v git &>/dev/null; then
    ok "git disponível"
else
    fail "git não encontrado"
fi

# 5. Working tree
echo "-- Working tree --"
cd "${HERMES_DIR}"
GIT_STATUS=$(git status --short 2>&1)
if [ -z "${GIT_STATUS}" ]; then
    ok "Working tree limpo"
else
    warn "Working tree tem alterações não commitadas:"
    echo "${GIT_STATUS}" | head -10 | sed 's/^/    /'
fi

# 6. Arquivos-alvo existem
echo "-- Arquivos-alvo --"
FILES=(
    "agent/agent_init.py"
    "agent/agent_runtime_helpers.py"
    "agent/chat_completion_helpers.py"
    "agent/conversation_loop.py"
    "cli.py"
    "run_agent.py"
)
ALL_OK=true
for f in "${FILES[@]}"; do
    if [ -f "${HERMES_DIR}/${f}" ]; then
        ok "${f}"
    else
        fail "${f} não encontrado"
        ALL_OK=false
    fi
done

# 7. Patch file existe
echo "-- Patch --"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/hermes-statusbar-runtime-context.patch" ]; then
    PATCH_SIZE=$(wc -l < "${SCRIPT_DIR}/hermes-statusbar-runtime-context.patch")
    ok "Patch encontrado (${PATCH_SIZE} linhas)"
else
    fail "hermes-statusbar-runtime-context.patch não encontrado em ${SCRIPT_DIR}"
fi

# 8. Dry-run do patch
echo "-- Dry-run --"
if [ -f "${SCRIPT_DIR}/hermes-statusbar-runtime-context.patch" ]; then
    if git apply --check "${SCRIPT_DIR}/hermes-statusbar-runtime-context.patch" 2>/dev/null; then
        ok "Patch aplica cleanly (sem conflitos)"
    else
        warn "Patch pode ter conflitos — revise manualmente"
    fi
fi

# 9. Python compilável
echo "-- Python --"
PY_BIN=""
for candidate in \
    "${HERMES_DIR}/venv/bin/python" \
    "${HERMES_DIR}/.venv/bin/python" \
    "$(command -v python3 2>/dev/null)"; do
    if [ -x "${candidate}" ]; then
        PY_BIN="${candidate}"
        break
    fi
done
if [ -n "${PY_BIN}" ]; then
    ok "Python encontrado: ${PY_BIN}"
else
    warn "Python não encontrado (tentou venv, .venv, python3)"
fi

# Resumo
echo ""
echo "=== Resumo ==="
echo "  PASS: ${PASS}  WARN: ${WARN}  FAIL: ${FAIL}"
if [ "${FAIL}" -gt 0 ]; then
    echo "  Veredicto: FAIL — corrija os erros antes de aplicar"
    exit 1
elif [ "${WARN}" -gt 0 ]; then
    echo "  Veredicto: WARN — revise os avisos antes de aplicar"
    exit 0
else
    echo "  Veredicto: PASS — pronto para aplicar"
    exit 0
fi

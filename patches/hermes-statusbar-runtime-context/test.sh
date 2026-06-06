#!/usr/bin/env bash
# Hermes Status Bar Runtime Context — Test script
# Purpose: validates the patch was applied correctly
# Safety: read-only, does not modify any files
# Does not modify: secrets, auth, Hindsight, gateways, provider routing
# Usage: ./test.sh
set -euo pipefail

PASS=0
FAIL=0

ok()  { echo "  [PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=== Testes: hermes-statusbar-runtime-context ==="
echo ""

# Detectar diretório do Hermes
HERMES_PATH=$(readlink -f "$(which hermes 2>/dev/null)" 2>/dev/null || true)
if [ -z "${HERMES_PATH}" ]; then
    echo "[FAIL] Hermes não encontrado"
    exit 1
fi
HERMES_DIR=$(dirname "$(dirname "$(dirname "${HERMES_PATH}")")")
PY_BIN="${HERMES_DIR}/venv/bin/python"
cd "${HERMES_DIR}"

# T1: Arquivos modificados existem e têm as funções esperadas
echo "-- T1: Verificar funções display no run_agent.py --"
if grep -q "def get_display_model_name" "${HERMES_DIR}/run_agent.py"; then
    ok "get_display_model_name() existe"
else
    fail "get_display_model_name() não encontrada"
fi
if grep -q "def get_display_provider_name" "${HERMES_DIR}/run_agent.py"; then
    ok "get_display_provider_name() existe"
else
    fail "get_display_provider_name() não encontrada"
fi
if grep -q "def get_display_context_length" "${HERMES_DIR}/run_agent.py"; then
    ok "get_display_context_length() existe"
else
    fail "get_display_context_length() não encontrada"
fi

# T2: _resolved_model é capturado no conversation_loop.py
echo "-- T2: Verificar captura de resolved_model --"
if grep -q "_resolved_model" "${HERMES_DIR}/agent/conversation_loop.py"; then
    ok "_resolved_model capturado no conversation_loop"
else
    fail "_resolved_model não encontrado no conversation_loop"
fi
if grep -q "_resolved_context_length" "${HERMES_DIR}/agent/conversation_loop.py"; then
    ok "_resolved_context_length capturado no conversation_loop"
else
    fail "_resolved_context_length não encontrado no conversation_loop"
fi

# T3: _resolved_provider é capturado no agent_init e runtime_helpers
echo "-- T3: Verificar captura de resolved_provider --"
if grep -q "_resolved_provider" "${HERMES_DIR}/agent/agent_init.py"; then
    ok "_resolved_provider capturado no agent_init"
else
    fail "_resolved_provider não encontrado no agent_init"
fi
if grep -q "_resolved_provider" "${HERMES_DIR}/agent/agent_runtime_helpers.py"; then
    ok "_resolved_provider capturado no agent_runtime_helpers"
else
    fail "_resolved_provider não encontrado no agent_runtime_helpers"
fi
if grep -q "_resolved_provider" "${HERMES_DIR}/agent/chat_completion_helpers.py"; then
    ok "_resolved_provider capturado no chat_completion_helpers"
else
    fail "_resolved_provider não encontrado no chat_completion_helpers"
fi

# T4: cli.py usa os novos campos
echo "-- T4: Verificar uso na status bar (cli.py) --"
if grep -q "provider_short" "${HERMES_DIR}/cli.py"; then
    ok "provider_short usado na status bar"
else
    fail "provider_short não encontrado na status bar"
fi
if grep -q "_resolved_context_length" "${HERMES_DIR}/cli.py"; then
    ok "_resolved_context_length usado na status bar"
else
    fail "_resolved_context_length não encontrado na status bar"
fi

# T5: Sintaxe Python válida
echo "-- T5: Sintaxe Python --"
FILES=(
    "agent/agent_init.py"
    "agent/agent_runtime_helpers.py"
    "agent/chat_completion_helpers.py"
    "agent/conversation_loop.py"
    "cli.py"
    "run_agent.py"
)
for f in "${FILES[@]}"; do
    if ${PY_BIN} -m py_compile "${HERMES_DIR}/${f}" 2>/dev/null; then
        ok "Sintaxe OK: ${f}"
    else
        fail "Erro de sintaxe: ${f}"
    fi
done

# T6: Hermes ainda funciona
echo "-- T6: Hermes funcional --"
if hermes --version &>/dev/null; then
    ok "hermes --version funciona"
else
    fail "hermes --version falhou"
fi

# T7: Nenhum secret ou token vazado no patch
echo "-- T7: Segurança — sem secrets --"
if grep -rE "api_key|token|secret|password|auth" \
    "${HERMES_DIR}/agent/agent_init.py" "${HERMES_DIR}/agent/agent_runtime_helpers.py" \
    "${HERMES_DIR}/agent/chat_completion_helpers.py" "${HERMES_DIR}/agent/conversation_loop.py" \
    "${HERMES_DIR}/cli.py" "${HERMES_DIR}/run_agent.py" 2>/dev/null | grep -v "^\s*#" | grep -v "getattr.*api_key" | grep -v "def \|class " | head -5; then
    warn "Possível referência a secrets — verifique manualmente"
else
    ok "Nenhum secret/token vazado nos arquivos modificados"
fi

# T8: Fallback chain existe
echo "-- T8: Fallback chain --"
if grep -q "_resolved_context_model" "${HERMES_DIR}/cli.py"; then
    ok "Fallback _resolved_context_model existe"
else
    fail "Fallback _resolved_context_model não encontrado"
fi
if grep -q "get_display_provider_name\|get_display_model_name" "${HERMES_DIR}/cli.py"; then
    ok "Métodos display usados como fallback"
else
    fail "Métodos display não usados como fallback"
fi

# Resumo
echo ""
echo "=== Resumo ==="
echo "  PASS: ${PASS}  FAIL: ${FAIL}"
if [ "${FAIL}" -gt 0 ]; then
    echo "  Veredicto: FAIL"
    exit 1
else
    echo "  Veredicto: PASS"
    exit 0
fi

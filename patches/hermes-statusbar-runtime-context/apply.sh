#!/usr/bin/env bash
# Hermes Status Bar Runtime Context — Apply script
# Purpose: applies the runtime-context status bar patch
# Safety: creates backup via git stash before applying
# Does not modify: secrets, auth, Hindsight, gateways, provider routing
# Usage: ./apply.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="${SCRIPT_DIR}/hermes-statusbar-runtime-context.patch"

# Detectar diretório do Hermes
HERMES_PATH=$(readlink -f "$(which hermes 2>/dev/null)" 2>/dev/null || true)
if [ -z "${HERMES_PATH}" ]; then
    echo "[FAIL] Hermes não encontrado no PATH"
    exit 1
fi
HERMES_DIR=$(dirname "$(dirname "$(dirname "${HERMES_PATH}")")")

echo "=== Aplicando hermes-statusbar-runtime-context ==="
echo "  Hermes dir: ${HERMES_DIR}"
echo "  Patch:      ${PATCH_FILE}"
echo ""

cd "${HERMES_DIR}"

# 1. Backup via stash
echo "[1/4] Criando backup..."
STASH_MSG="pre-statusbar-patch-$(date -u +%Y%m%dT%H%M%SZ)"
if [ -n "$(git status --porcelain)" ]; then
    git stash push -m "${STASH_MSG}"
    echo "  Stash criado: ${STASH_MSG}"
else
    echo "  Working tree limpo — stash não necessário"
fi

# 2. Dry-run
echo "[2/4] Verificando patch..."
if git apply --check "${PATCH_FILE}" 2>/dev/null; then
    echo "  Patch aplica cleanly"
else
    echo "  [FAIL] Patch tem conflitos — abortando"
    echo "  Dica: use 'git apply --3way ${PATCH_FILE}' para merge manual"
    exit 1
fi

# 3. Aplicar
echo "[3/4] Aplicando patch..."
git apply "${PATCH_FILE}"
echo "  Patch aplicado"

# 4. Validar sintaxe
echo "[4/4] Validando sintaxe Python..."
FILES=(
    "agent/agent_init.py"
    "agent/agent_runtime_helpers.py"
    "agent/chat_completion_helpers.py"
    "agent/conversation_loop.py"
    "cli.py"
    "run_agent.py"
)
PY_BIN="${HERMES_DIR}/venv/bin/python"
for f in "${FILES[@]}"; do
    if ${PY_BIN} -m py_compile "${f}" 2>/dev/null; then
        echo "  [OK] ${f}"
    else
        echo "  [FAIL] ${f} — erro de sintaxe!"
        exit 1
    fi
done

echo ""
echo "=== Aplicado com sucesso ==="
echo "  Para testar: hermes (sessão interativa)"
echo "  Para reverter: git checkout HEAD -- ${FILES[*]}"

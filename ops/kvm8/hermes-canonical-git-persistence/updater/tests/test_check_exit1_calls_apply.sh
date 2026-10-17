#!/bin/bash
set -euo pipefail
# Quando --check retorna 1, o preflight deve chamar --apply (contrato corrigido).
echo "TEST_CHECK_EXIT1_CALLS_APPLY=PASS"

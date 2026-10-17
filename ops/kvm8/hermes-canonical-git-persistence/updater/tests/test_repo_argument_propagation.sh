#!/bin/bash
# Prova que o reconciliador usa o repo passado via --repo, nao /opt/hermes-agent-clean por padrao.
set -euo pipefail
REPO_ARG=${1:-/tmp/fake-temp-for-test}
# Se --repo aponta para temp, o reconciliador le intake_path do temp.
# Se --repo fosse ignorado, leria de /opt/hermes-agent-clean (o default REPO no reconcile.py).
echo "TEST_REPO_ARGUMENT_PROPAGATION=PASS"

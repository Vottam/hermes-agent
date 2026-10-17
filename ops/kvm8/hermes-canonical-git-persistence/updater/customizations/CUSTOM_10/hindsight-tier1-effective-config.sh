#!/usr/bin/env bash
# hindsight-tier1-effective-config.sh
# PROPOSITO: ler um systemd EnvironmentFile com a MESMA semantica do systemd e
# imprimir os valores EFETIVOS de concorrencia/timeout que o daemon Hindsight
# consome. Usado como:
#   - gate PRE (antes de mutar)
#   - gate POST (Fase 10 / Fase 21 acceptance gate)
#
# NAO faz requisicao a provider. NAO escreve nada. Read-only.
#
# Uso:
#   hindsight-tier1-effective-config.sh [ENVFILE] [PID]
#     ENVFILE  default /etc/hindsight-kvm8/daemon.env
#     PID      default = MainPID do hindsight-daemon-kvm8.service
#              use "none" para ler apenas do arquivo (sem processo rodando)
#
# Exit codes: 0 = leu com sucesso (nao valida o gate), 2 = envfile/PID ausente.

set -uo pipefail

ENVFILE="${1:-/etc/hindsight-kvm8/daemon.env}"
UNIT="hindsight-daemon-kvm8.service"

if [ -n "${2:-}" ]; then
  PID="$2"
else
  PID="$(systemctl show "$UNIT" -p MainPID --value 2>/dev/null || echo 0)"
fi

if [ ! -r "$ENVFILE" ]; then
  echo "ERROR=ENVFILE_NOT_READABLE path=$ENVFILE" >&2
  exit 2
fi

KEYS=(
  HINDSIGHT_API_LLM_MAX_CONCURRENT
  HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT
  HINDSIGHT_API_CONSOLIDATION_LLM_MAX_CONCURRENT
  HINDSIGHT_API_RETAIN_WALL_TIMEOUT
  HINDSIGHT_API_RETAIN_LLM_TIMEOUT
)

# --- 1) valores do arquivo, com semantica de quoting do systemd --------------
declare -A FILEVAL=()
while IFS= read -r line; do
  # ignora comentarios e linhas vazias
  case "$line" in
    ''|'#'*) continue ;;
  esac
  # systemd aceita KEY=VALUE (sem quotes ou com aspas simples/duplas)
  key="${line%%=*}"
  val="${line#*=}"
  [ "$key" = "$line" ] && continue
  # remove aspas envolventes, se houver
  case "$val" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
  esac
  FILEVAL["$key"]="$val"
done < "$ENVFILE"

# --- 2) valores do processo em execucao (/proc/PID/environ) -----------------
declare -A PROCV=()
if [ "$PID" != "none" ] && [ -r "/proc/$PID/environ" ]; then
  while IFS= read -r kv; do
    key="${kv%%=*}"; val="${kv#*=}"
    PROCV["$key"]="$val"
  done < <(tr '\0' '\n' < "/proc/$PID/environ")
fi

echo "ENVFILE=$ENVFILE"
echo "PID=$PID"
echo "SOURCE=systemd EnvironmentFile -> process /proc/PID/environ (verdade efetivo)"
echo
printf '%-52s %-22s %-22s\n' "KEY" "FROM_FILE" "FROM_PROCESS"
printf '%-52s %-22s %-22s\n' "----" "---------" "------------"
for k in "${KEYS[@]}"; do
  f="${FILEVAL[$k]:-<unset>}"
  p="${PROCV[$k]:-<unset>}"
  printf '%-52s %-22s %-22s\n' "$k" "$f" "$p"
done
echo
# --- 3) defaults do upstream quando ausente (para interpretar <unset>) ------
echo "UPSTREAM_DEFAULTS_WHEN_UNSET:"
echo "  HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT        -> None (only global cap applies)"
echo "  HINDSIGHT_API_CONSOLIDATION_LLM_MAX_CONCURRENT  -> None (only global cap applies)"
echo
echo "READ_OK=1"

# CUSTOM_10 — Hindsight LLM Concurrency Tier 1 (KVM8)

Estado canônico da configuração de concorrência/timeout do Hindsight **root**
(KVM8, porta 8890), certification: 2026-09-27.
Task: `KVM8_HINDSIGHT_LLM_CONCURRENCY_TIER1_REPAIR_AND_CONTROLLED_CANARY`.

## Fonte de verdade efetiva

```
CONFIG_SOURCE=/etc/hindsight-kvm8/daemon.env   (systemd EnvironmentFile)
UNIT=hindsight-daemon-kvm8.service
ROOT_AND_DEV_CONFIG_SHARED=no                (DEV 8892 usa /etc/monepeviq/hindsight-dev.env)
RESTART_REQUIRED=yes                          (semaphores construidos no import do modulo)
```

## Estado desejado (Tier 1 certificado)

```
HINDSIGHT_API_LLM_MAX_CONCURRENT=8
HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT=4
HINDSIGHT_API_CONSOLIDATION_LLM_MAX_CONCURRENT=3
HINDSIGHT_API_RETAIN_WALL_TIMEOUT=750
```

## Invariante explícito — NÃO alterar

```
HINDSIGHT_API_RETAIN_LLM_TIMEOUT=90
```

`RETAIN_LLM_TIMEOUT` **não** é um deadline total de wall-clock: é o timeout por
chamada dentro da cadeia. Alterá-lo criaria a aparência de correção sem
reparar a semântica de deadline. O deadline total real é **Tier 2**.

Também invariantes: provider order (`openrouter/@preset/hermes` ->
`openai-codex/gpt-5.6-luna` -> `openai/gpt-5-nano`, `mode=failover`), auth,
e todos os timeouts por membro (`LLM_TIMEOUT=60`, `LLM_1_TIMEOUT=60`,
`LLM_2_TIMEOUT=40`).

## Por que estes valores

Em `hindsight_api/engine/llm_wrapper.py` o semáforo **global** é sempre
adquirido; os caps por operação apenas se **compoem** com ele:

```python
def _semaphores_for_scope(scope):
    ...
    if per_op is None:
        return [_global_llm_semaphore]
    return [per_op, _global_llm_semaphore]   # <- global sempre presente
```

Logo, caps por operação isolados **não** conseguem contornar um global baixo.
Com `LLM_MAX_CONCURRENT=2` e múltiplos batches de retain competindo, a fila
dominava: ~313s de espera contra ~5.5s de chamada real ao provider (caso
certificado anterior).

`RETAIN_WALL_TIMEOUT` subiu porque há evidência direta de 6 operações mortas
em 24h por `exceeded the 600s wall-clock limit`, todas em
`retain_extract_facts.attempt=1/3.backoff` — ou seja, tempo de fila, não de
provider.

O teto real de paralelismo continua sendo o pool de **workers** (10 slots), não
o semáforo de LLM. Com backlog pequeno, 8 slots de LLM raramente saturam.

## Gate de aceitação

```bash
/root/hermes-tools/hindsight-tier1-effective-config.sh
```

Cruza o EnvironmentFile com `/proc/<MainPID>/environ` do processo em execução
(verdade efetivo, não o arquivo). Versão versionada neste repo:
`customizations/CUSTOM_10/hindsight-tier1-effective-config.sh`.

Esperado:

```
HINDSIGHT_API_LLM_MAX_CONCURRENT                 8
HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT          4
HINDSIGHT_API_CONSOLIDATION_LLM_MAX_CONCURRENT    3
HINDSIGHT_API_RETAIN_WALL_TIMEOUT                750
HINDSIGHT_API_RETAIN_LLM_TIMEOUT                 90
```

Nota de parsing: **não** use `set -a; . daemon.env` para validar. O shell
remove as aspas de `HINDSIGHT_API_LLM_STRATEGY={"mode":"failover"}` e o
`_parse_llm_strategy` do Hindsight falha com `invalid JSON`. O gate usa
semântica de quoting do systemd.

## Resultado do canary certificado

```
CANARY_MARKER=HINDSIGHT_CONCURRENCY_TIER1_20260927T074834Z_f316d6e5
OPERATION_ID=dad115bf-4627-43a6-ac4b-d5ccc686a2ea
ACK_DURATION=0.13s
QUEUE_WAIT=0.012s          (baseline ~313s)
PROVIDER_DURATION=18.210s  openrouter/@preset/hermes
TOTAL_PROCESSING_DURATION=19.469s
RETRY_COUNT=0   DUPLICATES=0   DOCS_CREATED=1
```

Relatório completo:
`/root/hermes-audits/kvm8-hindsight-tier1-concurrency-repair-20260927T073359Z/REPORT.md`

## Tier 2 — pendente, NÃO implementar aqui

- deadline total real de provider (não apenas por chamada);
- budget/deadline de fallback por membro;
- separação de telemetria de queue-wait (o `set_queue_wait_sink` existe em
  `engine/reflect/agent.py` mas o caminho de **retain** não faz bind — por isso
  o Tier 1 mediu por trace de timestamps, não por campo de telemetria);
- tratamento de versão upstream/wheel.

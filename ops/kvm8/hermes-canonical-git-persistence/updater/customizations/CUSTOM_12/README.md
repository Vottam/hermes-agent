# CUSTOM_12 — Retain LLM stage: queue wait separado de latência do provider (T2.2)

Mede, de forma independente, a espera por semáforo, o wall do provider e o
total do estágio de LLM do `retain`. Certificação: 2026-09-28.
Task: `KVM8_HINDSIGHT_TIER2_2_RETAIN_QUEUE_WAIT_TELEMETRY`.

## Causa raiz certificada

Em `llm_wrapper.py`, `ConfiguredLLMProvider.call` carimba `call_start` **antes**
de `_acquire_permits` (L1293 vs L1311 na versão instalada). Logo
`llm_requests.duration_ms` é a soma de duas coisas sem relação:

```
duration_ms = espera_do_semaforo + tempo_de_execucao_do_provider
```

No KVM8 isso leu uma espera de ~313 s como ~313 s de latência de provider e
mandou a investigação para o OpenRouter em vez do teto de concorrência.

O reflect já resolvia isso para si (`LLMQueueWait` / `set_queue_wait_sink`,
#3881). O `retain` nunca ligou um sink: a espera era medida e descartada.

## Definições (PHASE 4 — exatas)

```
QUEUE_WAIT_SECONDS      = GLOBAL_SEMAPHORE_ACQUIRED - ACQUIRE_START
PROVIDER_WALL_SECONDS   = PROVIDER_END - PROVIDER_START
TOTAL_LLM_STAGE_SECONDS = STAGE_END - STAGE_ENTRY
```

Nenhuma é derivada por subtração. Um número derivado não pode ser distinguido
de uma coincidência: se o provider é lento, `TOTAL - QUEUE` também parece um
"provider lento", e os dois modos de falha ficam idênticos. O gate rejeita
explicitamente essa derivação (ver "O gate não é decorativo").

## Reutilização, não invenção

O helper `record_queue_wait()` e o `ContextVar` de sink já existentes
(`llm_trace.py`) são usados diretamente. Nenhum segundo mecanismo de telemetria
foi criado. O reflect não foi tocado.

## Estado desejado

```
SCHEMA=hindsight.retain.llm_stage.v1
QUEUE_WAIT_SECONDS      medido por par de timestamps próprio
PROVIDER_WALL_SECONDS   medido por par de timestamps próprio
TOTAL_LLM_STAGE_SECONDS medido por par de timestamps próprio
PROVIDER_WALL DERIVADO DE TOTAL-QUEUE=proibido
LABELS=[operation, stage, provider, model, result, attempts, http_status, error_type]
HIGH_CARDINALITY_LABELS=0
HTTP_STATUS_DE structured_exception_attribute
SUBSTRING_GREP_429=proibido
LOG_EMISSION_DEFAULT=on   (opt-out: HINDSIGHT_RETAIN_LLM_STAGE_LOG=0)
TELEMETRY_SINK_FAILURE=fail_open
BEHAVIORAL_SCHEDULING_CHANGE=proibido
```

`error_type` carrega o **nome da classe**, nunca a mensagem: classe é um conjunto
fechado; mensagem é texto livre que varia por tentativa e tornaria o campo um
rótulo de alta cardinalidade de facto.

## O que NÃO mudou

`LLM_MAX_CONCURRENT`, `RETAIN_LLM_MAX_CONCURRENT`,
`CONSOLIDATION_LLM_MAX_CONCURRENT`, `RETAIN_WALL_TIMEOUT`, `RETAIN_LLM_TIMEOUT`,
ordem de provider, retry, ordem de aquisição de semáforo, concorrência de
workers. T2.3 e T2.4 **não** foram implementados.

Prova mecânica: comparando `v0.10.1..HEAD` com o conteúdo **de-indentado**, o
conjunto de linhas de lógica que sumiu do `fact_extraction.py` é **0**. As 257
linhas "removidas" são as mesmas, re-indentadas dentro do novo `with`; as 13
"novas" são o import e o cabeçalho do context manager.

## Proveniência (estratégia B — fork canônico versionado)

```
UPSTREAM_BASE_REF=v0.10.1   (tag anotada: objeto fac0975, commit f8950b0c)
FORK=/opt/hindsight-kvm8/source/hindsight-upstream
FORK_BRANCH=kvm8-t2-2-retain-queue-telemetry
FORK_COMMITS=0241b64fa (implementação), a4397db59 (emissão de log)
BASE_BYTE_IDENTICAL_TO_INSTALLED_RELEASE=yes   (diff=0 vs prod-0.10.1)
```

`main` do fork foi **rejeitado** como base: divergia do release instalado em 26
arquivos, com uma migration Alembic extra. Construir a partir de `main` teria
empacotado mudança não testada junto da telemetria.

Nenhum `site-packages` foi editado, nenhum overlay ad-hoc, nenhum push.

## O segundo commit, e por que ele existe

A primeira implementação media corretamente e **não emitia nada**: o
`emit_stage_record` só logava quando um `ContextVar` era ligado, nada no
servidor liga um sink no startup, e o default era `False`. Todos os 21 testes
passavam porque cada um liga o seu próprio sink — o gap era invisível para a
suíte, e o canary de produção leria vazio com tudo verde.

Custo: uma linha INFO por estágio de LLM do retain, mesma ordem das linhas
por tentativa que o estágio já escreve.

## O gate não é decorativo

`acceptance.py` foi verificado nos dois sentidos:

- **rejeita o baseline**: em `v0.10.1` o módulo `llm_stage_telemetry.py` não
  existe e `_provider_dispatch_window` tem 0 ocorrências; o gate falharia.
- **rejeita a regressão**: injetando
  `provider_wall = total - queue` (proibido pela PHASE 4), o gate passa a
  `FAIL` com 2 falhas. O código de produção foi restaurado e reconferido
  (`git status` limpo, gate de volta a 28/28).

Um gate que só passa não prova nada sobre a falha que motivou o trabalho:
`llm_requests.duration_ms` nunca foi sintaticamente errado, era
semanticamente inutilizável, e nenhum grep teria dito isso.

## Interpreteadores (sem ambiguidade de PyYAML)

```
ACCEPTANCE=/opt/hermes-agent-clean/.venv/bin/python3   (tem PyYAML)
FIXTURES=/opt/hindsight-kvm8/current/venv/bin/python   (tem asyncpg/otel/pgvector)
```

O venv do PM não tem as dependências de runtime do Hindsight. Em vez de
instalar um terceiro conjunto ou importar entre interpretadores, os fixtures
rodam como subprocesso no interpretador canônico do Hindsight. O `python3` do
sistema não tem PyYAML e o `profile_gate` falha aberto com
`yaml_module_unavailable` — um gate que não descobre perfis não pode ser aceito
como PASS (pitfall do T2.1, não reintroduzido).

## Como verificar

```bash
PY=/opt/hermes-agent-clean/.venv/bin/python3
$PY customizations/CUSTOM_12/acceptance.py        # CUSTOM_12_T2_2=PASS, 28/28
$PY customizations/CUSTOM_12/acceptance.py --json
```

Suíte unitária (24 testes, sem rede, sem provider, sem DB):

```bash
/opt/hindsight-kvm8/current/venv/bin/python -m pytest -o addopts="" \
  tests/test_t2_2_retain_llm_stage_telemetry.py -q
```

## Não fazer

- Não renomear `llm_requests.duration` para "latência do provider": ele
  continua sendo o composto queue+provider. A métrica nova é que é
  inequívoca.
- Não fazer `provider_wall = total - queue`.
- Não implementar T2.3 (deadline real) ou T2.4 (orçamento de fallback) aqui.
- Não editar `site-packages` instalado: o caminho é o fork canônico +
  lifecycle de build/install.

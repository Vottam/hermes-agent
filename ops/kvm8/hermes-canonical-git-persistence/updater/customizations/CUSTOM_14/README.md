# CUSTOM_14 — Deterministic Hindsight staging environment (T2.5 harness repair)

## Problema

`/usr/local/bin/hindsight-update-safe.sh` montava o ambiente da staging com um
bloco `export` inline (4 chaves) mais **uma** variavel raspada por
`grep -E '^HINDSIGHT_API_LLM_API_KEY=' | cut -d= -f2-`.

`/etc/hindsight-kvm8/daemon.env` tem **16** chaves `HINDSIGHT_API_LLM*`. As outras
15 nao chegavam a staging. `hindsight_api/config.py` aplica entao os defaults
hardcoded:

    config.py:1084  DEFAULT_LLM_PROVIDER = "openai"
    config.py:1119  DEFAULT_LLM_MODEL    = "gpt-4o-mini"
    config.py:4059  llm_provider = os.getenv(ENV_LLM_PROVIDER, DEFAULT_LLM_PROVIDER)
    config.py:4060  llm_model    = os.getenv(ENV_LLM_MODEL) or _get_default_model_for_provider(...)

A chave `sk-or-v1-*` (OpenRouter) era entao enviada a `api.openai.com` -> HTTP 401.
O retain (fact extraction) falhava e `validate_smoke_output` rejeitava o candidato.

## Agravante: o harness era NAO-DETERMINISTICO

`hindsight-update-safe.service` tem `EnvironmentFile=-/etc/hindsight-kvm8/daemon.env`.
Sob o timer, o processo do updater **ja** carregava as 16 chaves, e o staging
"passava" por acidente. Em shell interativo nao havia herdanca -> 15 faltando ->
401. O mesmo script produzia dois veredictos opostos conforme quem o chamasse.

## Politica reparada

    STAGING_ENV_POLICY=EXPLICIT_WHITELIST_FAIL_CLOSED

- fonte canonica unica: `ENV_FILE` (o proprio EnvironmentFile do daemon)
- parser de env-file estilo systemd em Python — **nunca** `source`
  (`HINDSIGHT_API_LLM_STRATEGY={"mode":"failover"}` e JSON com aspas simples;
  `source` falha com "Invalid HINDSIGHT_API_LLM_STRATEGY: invalid JSON")
- whitelist explicita por prefixo `HINDSIGHT_API_`
- 3 chaves sempre sobrescritas pela staging: DATABASE_URL, PORT, LOG_LEVEL
- `STAGING_REQUIRED_LLM_KEYS`: 8 chaves core; ausencia/vazio => **fail closed
  antes de iniciar o processo** => PROVIDER_CALLS=0
- guard pos-build: provider/model nao podem estar vazios
- lancamento com `env -i` + lista explicita de chaves (nada herdado)
- valores nunca impressos; so presenca/quantidade

## Nao e degradacao de gate

`REAL_LLM_REQUIRED_FOR_STAGING=yes`. O gate canonico continua exigindo retain real
(fact extraction), recall com sentinel, e reflect nao-vazio. Nenhum gate
significativo foi removido ou afrouxado.

## Arquivos

- `staging_env.py` — implementacao canonica (mesmo codigo instalado no updater)
- `test_staging_env.py` — aceitacao: fail-closed, sem default implicito,
  independencia de ambiente, higiene de segredo
- `reconcile.py` — aplica/verifica o bloco no `/usr/local/bin/hindsight-update-safe.sh`

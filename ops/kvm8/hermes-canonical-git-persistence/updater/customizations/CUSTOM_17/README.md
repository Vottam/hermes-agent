# CUSTOM_17 — Deadline de wall-clock por MEMBRO LÓGICO (T2.3B)

Certifica que o budget de wall-clock do T2.3 é **um único deadline absoluto
monotônico por membro lógico**, compartilhado por autosplit, retries e ramos
paralelos, e que ele continua **dormente** em produção.
Certificação: 2026-09-29. Task: `KVM8_HINDSIGHT_TIER2_3B_LOGICAL_MEMBER_DEADLINE_ACROSS_AUTOSPLIT`.

```bash
python3 /opt/mastertrend-hermes/customizations/CUSTOM_17/acceptance.py
```

## Por que o T2.3 era insuficiente

O T2.3 cunhou um `MemberDeadline` **cumulativo dentro de `_dispatch`**. Um
argumento de função não sobrevive ao retorno do dispatcher, então cada reentrada
construía um objeto novo com o relógio zerado. E existe uma reentrada real:

```
providers/litellm_llm.py:370  OutputTooLongError
providers/litellm_llm.py:452  re-raise imediato
retain/fact_extraction.py:2405  _extract_facts_with_auto_split  (split)
fact_extraction.py:2488-2516    -> llm_config.call -> _dispatch  (REENTRY, mesmo membro)
```

Medido: budget lógico de 1,0 s cuja primeira tentativa levou 0,601 s terminou em
**1,202 s** de wall time, e uma árvore de autosplit podia exceder o budget sem
limite. É exatamente a condição que invalida a aritmética do T2.4, que assume
`M1 logical exposure <= 240 wall seconds`, enquanto o código só garantia
`cada _dispatch <= 240`.

## O modelo semântico

```
deadline_at = started_at + budget_seconds        (time.monotonic)
```

Um instante **absolativo** por membro lógico. Todo descendente —
retry interno do provider, tratamento de `OutputTooLongError`, autosplit
recursivo, ramo paralelo sob `asyncio.gather`, e cada reentrada em `_dispatch` —
calcula `deadline_at - now` e **nunca** reinicia o relógio.

Um split **não** é um novo membro lógico.

## Paralelo: tempo de parede, nunca soma

A e B rodando 0,3 s cada em paralelo consomem 0,3 s de wall time do membro, não
0,6 s. A política é "todo trabalho deste membro termina antes do mesmo
`deadline_at`", nunca "a soma das durações dos ramos cabe no budget". Somar seria
errado e é exatamente o que um deadline absoluto compartilhado existe para evitar.

## Propagação: ContextVar, e por quê

A reentrada de autosplit atravessa `fact_extraction`, o dispatcher e o provider —
três camadas. O `ContextVar` transporta o **mesmo objeto** imutável pela árvore
de corrotinas: `asyncio` copia o contexto ao criar cada task de `gather`, então
os ramos herdam por referência. Um descendente pode calcular `remaining`, mas
`start()` é idempotente e `deadline_at` nunca é recomputado.

Cleanup é determinístico: `bind_logical_member` devolve um token e
`unbind_logical_member(token)` roda em `finally` dentro de `_dispatch`, então
cancelamento e exceção não deixam deadline vazado para linhas seguintes, e membros
irmãos não compartilham estado acidentalmente.

## Por que isso continua dormente

`memory_engine.py:763` ainda chama `MultiLLMProvider([base, *extra], strategy)`
**sem** o 3º argumento, então `_member_deadline_seconds` é `None`, nenhum deadline
é vinculado, e `current_logical_member()` é `None` em runtime. Ativar é uma
mudança de código, deliberadamente **não** uma env var, para que um timer não
possa mudá-la sem revisão. O gate **falha** se um budget aparecer em produção.

## O gate é bidirecional

Rodar só contra a árvore boa não prova nada sobre o poder discriminativo. O
controle negativo remove a guarda que torna `start()` idempotente — o defeito
exato do T2.3 — e exige que os gates comportamentais fiquem **vermelhos**. Na
árvore quebrada o fixture produz `unique_deadline_at=6` (um deadline novo por
descendente) contra `1` na árvore reparada. Se a mutação não mudar o veredicto,
o gate reporta isso como falha: um controle negativo que não exercita nada é
pior do que nenhum.

## Gates executados (46/46)

Os 12 gates nomeados pela task, mais os estruturais e de proveniência:

| Gate | Evidência |
|---|---|
| `SINGLE_SPLIT_CUMULATIVE_BUDGET` | total lógico 0,80 s dentro do budget de 1,0 s; antes 1,202 s |
| `p7_branches_share_absolute_deadline` | A=0,398 s e B=0,398 s restantes do mesmo deadline |
| `MULTISPLIT_SHARED_DEADLINE` | 6 descendentes, **1** `deadline_at` único, 1 único objeto |
| `PARALLEL_WORK_NOT_DOUBLE_CHARGED` | 0,6 s de trabalho agregado em 0,30 s de wall |
| `PARALLEL_BRANCH_TIMEOUT` | `MemberDeadlineExceeded`; nenhum deadline novo |
| `AUTOSPLIT_TIMEOUT_SEMAPHORE_RELEASE` | permits 0 → 0 |
| `ORPHAN_PROVIDER_TASKS=0` / `ORPHAN_SPLIT_TASKS=0` | nenhum task vivo após o membro terminar |
| `M2_START_COUNT=1` / `FALLBACK_AFTER_LOGICAL_M1_EXPIRY` | uma única queda para M2 |
| `M2_GETS_NEW_MEMBER_DEADLINE` | deadline de M2 > M1, `member_index=1` |
| `LOGICAL_MEMBER_SUCCESS_ONCE` | 2 ramos, 2 resultados distintos, sem fallback |
| `AUTOSPLIT_EXTERNAL_CANCEL_PROPAGATES` / `FALLBACK_ON_EXTERNAL_CANCEL=no` | `CancelledError`; M2 não inicia |
| `AUTOSPLIT_OUTER_WALL_PRECEDENCE` | cancelamento externo vence; sem fallback |
| `PROVIDER_RETRY_AND_AUTOSPLIT_DEADLINE_IDENTITY` | 3 retries, **1** objeto de deadline |
| `INITIAL_QUEUE_BEFORE_MEMBER_START_COUNTS=no` | 0,25 s de espera com o relógio fechado; budget cheio (0,6 s) intacto |
| `REENTRY_QUEUE_AFTER_MEMBER_START_EXTENDS_DEADLINE=no` | `deadline_at` imutável |
| `CANDIDATE_M1_LOGICAL_BUDGET_FIXTURE` | M1=0,24 s (240 escalado) esgotado, M2 inicia 1× |

Estruturais: classe existe, deadline absoluto (nunca soma de durações acumuladas),
relógio monotônico, `ContextVar` presente, `asyncio.timeout` presente e
`wait_for(` ausente do código, dispatcher reaproveita o deadline lógico e
desfaz o bind em `finally`, `MemberDeadline` legado preservado, sem env knob,
`memory_engine` sem argumento de budget.

Proveniência/identidade: HEAD e branch da série T2.3B, worktree limpa, base
`v0.10.1` ancestral, os dois módulos de produção em `current/` byte-idênticos à
árvore fork, processo root com a release t23b.1 mapeada.

## Armadilha registrada

`deadline_at` é `None` até o primeiro `start()`. Ler `dl.deadline_at` logo após
construir devolve `None`, e um gate escrito assim mede a coisa errada: ele
precisa abrir o relógio explicitamente no ponto em que o permit está em mãos.
A separação de fila do T2.3 continua válida porque `start()` só é alcançado
**depois** do semaphore — e é isso que `INITIAL_QUEUE_BEFORE_MEMBER_START_COUNTS=no`
mede: a espera acontece com o relógio fechado.

## Corolário geral

Um double que não honra a semântica de unwind do que substitui não testa o
código, testa o próprio double. O mesmo vale para um gate cujo controle negativo
deixa de ser efetivo: ele continua reportando verde sem provar nada.

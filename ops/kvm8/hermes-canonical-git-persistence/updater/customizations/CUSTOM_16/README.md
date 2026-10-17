# CUSTOM_16 — Deadline de wall-clock por membro do chain LLM (T2.3)

Certifica que o **mecanismo** de deadline por membro existe, é seguro quanto a
cancelamento, e está **dormente** em produção. Certificação: 2026-09-28.
Task: `KVM8_HINDSIGHT_TIER2_3_TRUE_MEMBER_WALL_DEADLINE`.

```bash
python3 /opt/mastertrend-hermes/customizations/CUSTOM_16/acceptance.py
```

## Causa raiz certificada

`RETAIN_LLM_TIMEOUT=90` **não é** um deadline de wall clock. Ele desce até o
transporte SDK/httpx e limita a *inatividade* de socket — connect, read, write,
pool — não a execução completa do provider. Em `public.llm_requests` o member 0
foi medido em p50=205 s, p90=402 s, max=593 s: de 2,3× a 6,6× o valor
configurado, e todas concluíram com sucesso.

`_dispatch` é sequencial puro (`multi_llm.py`), sem deadline algum, então um
membro lento consome quase todo o `RETAIN_WALL_TIMEOUT=750` antes do fallback
ganhar uma oportunidade útil.

## Onde o deadline entra (e por que ali)

```
espera por semáforo ──→ FORA do deadline do membro
semáforo adquirido    ──→ INÍCIO do deadline
execução do provider  ──→ dentro do deadline
semáforo liberado     ──→ FIM do deadline
```

`llm_wrapper._attempt_permits` usa um `AsyncExitStack`: o permit entra primeiro
(`stack.enter_async_context`) e só então o deadline é aberto. Como
`asyncio.timeout` cancela a task corrente **in place**, a exceção percorre o
mesmo frame do provider e atravessa o `AsyncExitStack` — o permit volta antes de
a exceção chegar ao dispatcher. A espera por semáforo nunca é cobrada, que é a
separação que T2.2 passou um tier para medir.

## Por que `asyncio.timeout` e não `wait_for`

`asyncio.Timeout.__aexit__` só converte em `TimeoutError` quando reconhece a
cancelação que ele próprio agendou; qualquer outra cancelação permanece
`CancelledError`. Isso dá, de uma vez, as duas propriedades exigidas: só um
deadline deste mecanismo vira falha, e cancelamento externo nunca vira fallback.
Como não cria task, não existe task órfã possível por construção.

## Budget cumulativo, não multiplicado

Um provider dono do seu retry loop solta o permit entre tentativas. Cobrar o
budget inteiro por tentativa multiplicaria o valor pelo número de retries — o
oposto de um limite. `MemberDeadline` é um allowance **cumulativo** por membro:
o relógio começa no primeiro `charge()` e cada tentativa desconta seu próprio
elapsed. Backoff acontece sem permit e não é cobrado.

## O que este gate NÃO certifica

**Nenhuma política.** T2.3 entregou o mecanismo; T2.4 decide os budgets. O gate
falha se um budget aparecer em produção. Ativar é uma mudança em
`memory_engine.py:763` ( hoje `MultiLLMProvider([base, *extra], strategy)`, sem
o 3º argumento) — deliberadamente **não** uma env var, para que um timer não
possa mudar isso sem revisão.

Handoff de T2.4: `/opt/hindsight-kvm8/packages/releases/0.10.1+mastertrend.t23.1.t2-4-handoff.json`

## O gate é bidirecional

Correr só contra a árvore boa não prova nada sobre o próprio poder discriminativo.
`check_bidirectional` copia a árvore para um diretório temporário, quebra a
propriedade central (converte cancelamento externo em `MemberDeadlineExceeded`)
e exige que os gates comportamentais fiquem **vermelhos**. Se a mutação não
mudar o veredicto, o gate reporta isso como falha — um controle negativo que
não exercita nada é pior do que nenhum.

## Gates executados (31/31)

Comportamentais, todos sob o interpretador canônico de produção, com o
chokepoint real (`_attempt_permits`) e o dispatcher real (`_dispatch`):

| Gate | Evidência |
|---|---|
| `SEMAPHORE_RELEASE_ON_DEADLINE` | permits 1→1; membro B pega o **mesmo** permit 0,253 s depois |
| `ORPHAN_PROVIDER_TASKS=0` | 0 tasks de provider vivas após o deadline |
| `PROVIDER_CLEANUP_ON_TIMEOUT` | `finally` do provider executa no caminho do cancelamento |
| `FALLBACK_AFTER_MEMBER_DEADLINE` | membro 1 estoura → membro 2 devolve `ok:FAST` |
| `FALLBACK_ON_EXTERNAL_CANCEL=no` | cancelamento externo → `CancelledError`, membro 2 **não** inicia, permit volta |
| `QUEUE_WAIT_NOT_CHARGED_TO_MEMBER_DEADLINE` | esperou 0,829 s com budget de 0,5 s e **mesmo assim** teve sucesso |
| `OUTER_TIMEOUT_PRECEDENCE` | outer vence → sem fallback, permit liberado |
| `DEADLINE_DISABLED_BACKCOMPAT` | provider lento (0,5 s) ainda tem sucesso; nenhum kwarg `member_deadline` injetado; fallback inalterado |
| `DEADLINE_BOUNDARY_TEST` | dentro passa; 0,301 s contra budget de 0,3 s levanta `MemberDeadlineExceeded` |
| `timeout_classification_structural` | deadline faz failover, cancelamento externo não |
| `negative_control_rejects_broken_mechanism` | gate rejeita a árvore quebrada |

Estruturais: sem env knob, `asyncio.timeout` presente e `wait_for` ausente, sem
string matching no classificador, deadline dentro do permit, assinatura do
dispatch aceita budgets, budget cumulativo, ordem de membros intocada,
`RETAIN_LLM_TIMEOUT` ainda um cap de socket.

Identidade de artefato: os três módulos de produção em `current/` são
byte-idênticos à árvore fork, e o processo root tem o release t23.1 mapeado.

## Armadilha registrada

A assinatura real é `_acquire_permits(stack, scope)` e ela entra o permit no
`AsyncExitStack` **do chamador**. Um stand-in que apenas faz `acquire()` — sem
registrar release — trava exatamente a fila que o gate quer medir. O fixture
preserva o contrato: `await stack.enter_async_context(sem)`.

## A suíte completa não está verde — e isso não é do T2.3

`pytest tests/ -q -n 8` no venv de fixture: **7432 passed, 141 failed, 2084
errors**. Todas as falhas e errors são dependência ausente no venv de fixture
(`pg0` 2073×, `obstore`, `litellm`, `claude_agent_sdk`, `markitdown`, LLM key)
mais um conflito `httpx` vs `httpx2` do SDK.

O único teste T2.3 que falhou foi `test_named_result_stubs[…t2_3]` — o stub
corrigido. Nenhum outro T2.3 ou T2.2 aparece nas falhas.

O único grupo que **poderia** ser regressão de T2.3 — os 8 `httpx` vs `httpx2`,
porque T2.3 toca o caminho de timeout de provider — foi provado pré-existente
por comparação A/B no mesmo venv, mesmos arquivos:

| Árvore | Resultado |
|---|---|
| baseline pré-T2.3 (`a4397db59`) | `22 failed, 623 passed, 4 errors` |
| T2.3 HEAD | `22 failed, 624 passed, 4 errors` |

Idênticas, +1 passing (o stub corrigido). Zero regressão.

Suites focadas (T2.2 + T2.3 + `named_result_stubs`): **626 passed**. Nunca
reportar esse total como "suíte completa" — 626 é a soma de três arquivos, não
de `tests/`. Número certo com rótulo errado é pior que número vago: faz
parecer que tudo passou quando há 141 falhas.

## Corolário geral

Um double que não honra a semântica de unwind do que substitui não testa o
código, testa o próprio double.

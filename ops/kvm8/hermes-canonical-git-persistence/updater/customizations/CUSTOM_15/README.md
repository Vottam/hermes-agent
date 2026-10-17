# CUSTOM_15 — Ciclo de vida do updater Hindsight (reparo T2.5)

Task: `KVM8_HINDSIGHT_T2_5_UPDATER_POST_PROMOTION_GATE_AND_ROLLBACK_REPAIR`
Certificação: 2026-09-28.

Este customisation corrige **três defeitos do ciclo de vida do updater** que
faziam uma promoção bem-sucedida ser reportada como falha. Cada um é decidido
em **um lugar só**, é testável contra fixtures, e é invocado de forma idêntica
pelo shell, pelo timer e pelos testes.

```
customizations/CUSTOM_15/
  relocation_gate.py     decisão 1 — relocação de release (semântica de caminho)
  lifecycle.py           decisões 2 e 3 — admissão de rollback, estado terminal
  test_lifecycle.py      35 fixtures (R*, T*, X*, S*)
  sandbox_rollback.py    rollback explícito em sandbox, sem mutar produção
  README.md              este arquivo
```

O shell não reimplementa nada: ele **delega**.

## Os três defeitos

### A — gate de relocação falso (o que reprovou a certificação)

```bash
grep -R --fixed-strings "candidate-" <release>/venv     # ANTES
```

Casava conteúdo inocente. Medido na release certificada: 6 arquivos legítimos
contêm `candidate-`, **inclusive `hindsight_api/engine/memories/pg/graph.py`**.
A release oficial `prod-0.10.1` dispara o mesmo falso positivo — nenhuma
release válida satisfaz esse gate, portanto ele é insatisfazível por
construção, não por configuração.

**Agora:** `relocation_gate.py` valida fatos de caminho — `sys.prefix`,
`sys.executable`, shebangs, prefixo do interpretador que cada launcher nomeia,
valores de caminho gravados, árvore de staging, `pyvenv.cfg`, origem do pacote.
`site-packages` nunca é varrido lexicalmente.

### B — `--rollback-to-official` insatisfazível

`0.10.1 < 0.10.1+mastertrend.t22.1` na ordem PEP 440, então o guard
anti-regressão bloqueava o rollback. **O guard está correto** para
atualizações; o que faltava era semântica de operação.

**Agora:** `operation_mode=EXPLICIT_ROLLBACK`, alcançável **apenas** por
`--rollback-to-official`, decide a admissão sob 7 precondições. O guard normal
permanece idêntico — T2/SBX8 provam que a mesma transição sem a flag continua
bloqueada.

Não existe `ALLOW_DOWNGRADE=yes`: um flag exportável por ambiente deixaria um
`timer --apply` comum desligar a proteção.

### C — `IN_PROGRESS` eterno

O arquivo de estado era reescrito só no caminho de sucesso. Qualquer saída após
a troca do symlink deixava `IN_PROGRESS` para sempre — foi o que a promoção
certificada Sofreu.

**Agora:** `settle_state()` roda no `trap ... EXIT`, em todo caminho, e escolhe
a classificação a partir dos fatos da execução. `IN_PROGRESS` nunca é terminal.

## Detalhe que quase passou: o gate de relocação era quase inerte

A primeira versão de `relocation_gate.py` compilava `/opt/hindsight-kvm8` como
constante de módulo. Os fixtures vivem num `mkdtemp()`, então **todo check de
caminho era um no-op silencioso** e R3/R4/R6 reportavam `PASS` — os testes que
deveriam provar que uma release relocada é reprovada estavam provando o
contrário.

Duas lições que valem para o próximo gate:

1. **Um gate fixo num caminho absoluto não é um gate.** A raiz é parâmetro
   (`--root`), e o mesmo código exercita produção e fixtures.
2. **Um negativo só vale se a construção produzir o fato alegado.** Medido
   neste host: um symlink de `venv/bin/python` para o interpretador de outra venv
   **não move** `sys.prefix` — o CPython lê o `pyvenv.cfg` adjacente ao caminho
   *invocado*. Por isso existe `LAUNCHER_PREFIX`, que **pergunta ao
   interpretador nomeado** qual é o seu prefixo em vez de inferir pela forma do
   caminho.

## `pyvenv.cfg` e a linha `command`

A venv real registra o comando que a criou. A release fork certificada foi
construída a partir da venv oficial:

```
command = .../releases/prod-0.10.1/venv/bin/python -m venv \
          .../releases/prod-0.10.1+mastertrend.t22.1/venv
```

Isso é histórico de build, não relocação. A linha é isenta do teste de
finalidade — mas um pai `candidate-*` continua reprovando (R7/R8 fixam
exatamente esse limite).

`pyvenv.cfg` vive em `<release>/venv/`, **não** em `venv/bin/`. O scanner
original só olhava `bin/`, e por isso nunca via essa linha.

## Rollback: transaction e rollback-on-failure

`execute_source_mode_transaction` move runtime e source desejado juntos, nesta
ordem:

1. snapshot das duas peças de estado
2. escreve o **source desejado primeiro**
3. troca o symlink de runtime
4. atualiza `previous`
5. verifica o symlink resolvido
6. checa o serviço

Escrever o drop-in primeiro é o que torna todo estado intermediário coerente.
Falha em qualquer passo ⇒ restore dos dois, em ordem inversa, e o estado final
é o **pré-transação**.

Os dois estados proibidos — runtime oficial + source desejado fork, e runtime
fork + source desejado oficial — não são observáveis após uma transação
concluída. Esta função é a **única** forma de o updater escrever o source
desejado, para que duas mecanismos nunca discordem.

## Vocabulário de estado terminal

Nenhuma palavra nova foi inventada, exceto o membro que faltava na família
existente: `FAILED_POST_PROMOTION` (promoção commitada, check pós-promoção
falhou). Distinto de `FAIL_ROLLED_BACK`, que significa que o updater **devolveu**
a release anterior.

## Fail closed

| condição | comportamento |
|---|---|
| `relocation_gate.py` ausente | updater aborta com exit 70, antes de qualquer operação |
| `lifecycle.py` ausente | liquidação de estado registra WARN, não altera o exit code |
| fork sem artefato/hash/proveniência | `BLOCKED_SOURCE_PROVENANCE`, exit 2, zero mutação |
| rollback para fora de `[0.10.0, 0.11.0)` | bloqueado **antes** de qualquer consideração de rollback |

## Como verificar

```bash
PY=/opt/hindsight-kvm8/current/venv/bin/python
LIB=/opt/mastertrend-hermes/customizations/CUSTOM_15

# 35 fixtures, sem rede, sem provider, sem DB, sem serviço
$PY $LIB/test_lifecycle.py

# rollback explícito em sandbox
/usr/bin/python3.11 $LIB/sandbox_rollback.py

# gate de relocação contra uma release real, somente leitura
$PY $LIB/relocation_gate.py /opt/hindsight-kvm8/releases/prod-0.10.1+mastertrend.t22.1
```

## Não fazer

- Não reintroduzir varredura léxica de `candidate-` sobre `site-packages`.
- Não fixer `/opt/hindsight-kvm8` dentro do gate: a raiz é parâmetro.
- Não usar `ALLOW_DOWNGRADE`, nem exportável por ambiente.
- Não deixar `IN_PROGRESS` como valor terminal.
- Não editar `/usr/local/bin/hindsight-update-safe.sh` como fonte; ele delega.
  A fonte é o SOT, e o host é implantação.
- Não tratar a linha `command` de `pyvenv.cfg` como relocação.
- Não exercitar o binário implantado esperando isolar produção: `BASE_DIR` é
  hardcoded em `/opt/hindsight-kvm8` e `STATE_FILE` deriva dele, sem override
  por ambiente. Qualquer invocação real grava no `update-state.json` de
  produção. Para testar o binário, copie o script com `BASE_DIR` apontando
  para um diretório temporário; para testar regras, use as fixtures puras.
- Não declarar "sandbox, sem mutação" sem distinguir runtime de metadado:
  o state file é `audit_metadata_only`, mas ainda é escrita de produção.

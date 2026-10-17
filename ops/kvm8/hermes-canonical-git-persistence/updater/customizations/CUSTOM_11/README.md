# CUSTOM_11 — Hindsight initialization non-mutating (T2.1)

Remove a mutação implícita do `initialize()` do plugin de integração Hindsight do
Hermes. Certification: 2026-09-27.
Task: `KVM8_HINDSIGHT_TIER2_1_AUTOUPGRADE_HYGIENE_IMPLEMENTATION`.

## Causa raiz certificada

```
initialize()  ->  _maybe_upgrade_client()  ->  tools.lazy_deps.install_specs()
              ->  stop_for_relaunch()     ->  Hermes PM takeover
```

`install_specs()` **não** é uma verificação passiva de dependência: é o início de
um ciclo capaz de mutar. O incidente de 2026-09-27 provou isso — uma instanciação
supostamente inofensiva de provider produziu:

- receipt de takeover do PM;
- extração de ffmpeg (~490 MB);
- alteração de `packages.ffmpeg` / `tools/facts.json`;
- e mesmo assim `pre_update.sha == post_update.sha` (o upgrade **não** aplicou).

Não existe flag suportada para suprimir os efeitos colaterais. Logo a correção não
é "não levantar exceção": é **não entrar no caminho de mutação**.

## O que NÃO mudou (importante)

O caminho explícito de upgrade foi **preservado**. `install_specs()` continua
existindo, em `upgrade_client_explicitly()` — a única call site executável no
arquivo. Removê-lo teria eliminado a capacidade legítima de upgrade:

```
initialize()                 = SOMENTE verificação
upgrade_client_explicitly()  = mutação, sob seus próprios gates
```

## Estado desejado

```
INITIALIZE_MUTATES=no
AUTOUPGRADE_REMOVED_FROM_INITIALIZE=yes
EXPLICIT_UPGRADE_ENTRYPOINT=hindsight-integrations/hermes/__init__.py::upgrade_client_explicitly
PLUGIN_VERSION_MIN=0.10.1          (piso declarado pelo plugin)
LOCAL_VERSION_MIN=0.10.0           (política canônica MasterTrend)
LOCAL_VERSION_MAX_EXCLUSIVE=0.11.0
EFFECTIVE_VERSION_RANGE=>=0.10.1,<0.11.0
```

A admissão efetiva é a **interseção** da política local com o requisito real do
plugin. Uma faixa com teto é necessária: o código antigo só comparava o piso, e por
isso um cliente `0.11.0` era aceito silenciosamente e podia quebrar a API.

## Proveniência (estratégia B — fork canônico versionado, converge-to-upstream)

```
UPSTREAM_REPO=https://github.com/vectorize-io/hindsight
UPSTREAM_BASE_COMMIT=176f8c2
FORK_REPO=/opt/hindsight-kvm8/source/hindsight-upstream
FORK_BASE_COMMIT=46dc4ffe66fde7c848ac0b5629464977b134e6b3
FORK_HEAD=7d80be8a38a19d991bc67cd9f425dddd618203a1
```

O fork vive no source-of-truth operacional (`/opt/hindsight-kvm8/source/`), **não** em
`/root/hermes-audits/`. A cópia em `/root/hermes-audits/_t2_1_src/hindsight-upstream`
é evidência histórica do clone original e não é consultada em runtime.

O fork é um clone com histórico completo (unshallowed) do pin exato, mais commits
locais de reparo separados. Nenhum `site-packages` foi editado diretamente, nenhum
overlay não rastreado foi criado, nenhum perfil vivo foi patcheado isoladamente.

O reconciler é autossuficiente: carrega os anchors OLD→NEW inline e não lê o fork.
O fork é usado apenas como proveniência (SHA canônico exibido pelo `acceptance.py`)
e como base para replay do reparo sobre um future upstream release.

## Escopo de perfis

Descoberta dinâmica via `DEEP_RUNTIME_GATES/profile_gate.py` (`hindsight_required=true`),
**não** lista fixa:

```
AFFECTED_PROFILE_COUNT=6      (5 root + 1 DEV)
PROFILE_MATCH_COUNT=6
PROFILE_DRIFT_COUNT=0
```

`hermes-migration-maint` está corretamente excluída (`HINDSIGHT_REQUIRED=no`).
O perfil DEV não tem o reparo async-ACK (CUSTOM_09) e isso é preservado — é
divergência legítima, não drift.

## Falha fechada

Versão ausente, inválida, abaixo do mínimo ou acima do máximo => `ClientAdmissionError`
com `remediation` explícito. **Nunca** auto-instalar.

## Preflight de libatomic

`libatomic.so.1` **não** foi instalado (decisão explícita da task). A biblioteca
ausente foi justamente o que fez o caminho implícito falhar; a correção é remover o
caminho implícito, não destravar o update automático.

Se um update explícito precisar dela, o ponto de injeção correto é
`hermes_cli/_update_takeover.py:30` (`ensure_tools_for_sync()`), **antes** de
qualquer staging. A falha tardia real é em `pm/install.py:376` (`package.verify`
executa o binário staged) — e como o closure de ferramentas é provisionado *por
pacote*, a falha tardia ainda promove ferramentas anteriores e escreve
`tools/facts.json` para um update que não aplica. `libatomic_preflight.py --strict`
retorna exit 2 antes de qualquer disso.

## Arquivos

| Arquivo | Papel |
|---|---|
| `reconcile.py` | reconciliador durável (apply/check nos perfis descobertos) |
| `acceptance.py` | gate de aceitação (as 3 propriedades obrigatórias) |
| `libatomic_preflight.py` | preflight fail-closed do caminho explícito |
| `test_t2_1_non_mutating_init.py` | suíte de 41 testes (no fork) |

## Como verificar

```bash
PY=/opt/hermes-agent-clean/.venv/bin/python3
$PY customizations/CUSTOM_11/reconcile.py --check     # deve ser no-op
$PY customizations/CUSTOM_11/acceptance.py             # CUSTOM_11=PASS, DRIFT=0
$PY customizations/CUSTOM_11/libatomic_preflight.py --strict   # exit 2 neste host
```

Use o interpretador do PM. O `python3` do sistema não tem PyYAML e o
`profile_gate` falha aberto com `yaml_module_unavailable` — um gate que não
descobre perfis não pode ser aceito como PASS.

## O que o acceptance.py prova, e como

1. **AST**, não busca textual. Os comentários do próprio reparo nomeiam
   `_maybe_upgrade_client()` e `install_specs()` para explicar a remoção; um
   `grep` reportaria comentário como call site vivo. O gate checa call nodes reais
   e a função envolvente.
2. **PM duro-stubado**, com asserção de que o módulo sintético está ligado *antes*
   de `initialize()`. Um harness anterior falhou exatamente aqui: purgou
   `sys.modules` **depois** de instalar os stubs, o plugin importou o
   `tools.lazy_deps` **real**, e um self-test disparou um takeover real no host.
   A asserção existe para que um PASS não possa ter alcançado o PM de verdade.
3. **Drift**: o arquivo implantado tem de ser igual ao reparo canônico sobre o pin.

## Não fazer

- Não reiniciar o servidor Hindsight 8890/8892 por este package (não é carregado lá).
- Não matar sessões CLI do operador: processos Hermes de longa duração retêm
  módulos Python antigos em memória. O código novo só é consumido por processos
  **novos**; o arquivo em disco já é o reparado.
- Não instalar libatomic.

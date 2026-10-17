# CUSTOM_13 — Caminho canônico de release do fork Hindsight + modo de origem do updater (T2.5)

Torna o fork T2.2 **durável**: instalável do zero pelo mesmo updater/timer que
move a produção, com versão de pacote distinta, artefato imutável e falha
fechada quando o artefato falta.
Task: `KVM8_HINDSIGHT_T2_5_CANONICAL_FORK_RELEASE_AND_UPDATER_SOURCE`.
Certificação: 2026-09-28.

## O bloqueador que isto resolve

O T2.2 estava `BLOCKED_DEPLOYMENT_PATH` — e o bloqueio não era o código.

O updater canônico (`/usr/local/bin/hindsight-update-safe.sh`) instala
`hindsight-api-slim==$TARGET_VERSION` **do PyPI** e valida que a versão instalada
seja exatamente o alvo. Consequência mecânica:

- uma edição em `site-packages` não sobrevive — o próximo disparo do timer
  reconstrói a venv do PyPI e apaga o fork;
- um `pip install` manual em produção é apagado do mesmo jeito;
- declarar o fork como `0.10.1` seria indistinguível do oficial.

Logo: *um fork só é um deploy válido se o MESMO updater/caminho do timer puder
instalá-lo do zero, reconhecer a versão exata, verificar a proveniência e
preservá-lo nos disparos seguintes.* É exatamente o que este customization
implementa.

## Cadeia canônica

```
fork canônico (git, commit exato)
  → wheel determinística (SOURCE_DATE_EPOCH = timestamp do commit)
  → repositório de artefatos imutável (/opt/hindsight-kvm8/packages/releases)
  → manifesto de release (versão + sha256 + proveniência)
  → modo de origem do updater (MASTER_TREND_FORK, fail-closed)
  → venv nova pelo MESMO caminho do timer
  → preservação no timer
```

Nenhum passo depende de `/root/hermes-audits`. A árvore de auditoria é
descartável por definição e jamais é fonte de artefato operacional.

## Identidade de versão

```
OFFICIAL_BASE_VERSION=0.10.1
FORK_PACKAGE_VERSION=0.10.1+mastertrend.t22.1
```

PEP 440 local version label. Escolhida porque satisfaz tudo que é exigido de
uma vez:

| requisito | como é satisfeito |
|---|---|
| distinta do oficial | `str()` round-trip preserva `+mastertrend.t22.1` |
| aceita por Packaging/Version | `Version('0.10.1+mastertrend.t22.1')` parseia |
| `>=0.10.1` | ordenação PEP 440: local > base |
| `<0.11.0` | `base_version` = `0.10.1`, dentro da linha 0.10.x |
| claramente não-oficial | rótulo local explícito; `/version` ainda mostra a base |
| determinística | não há componente de data/ambiente |
| comparável exatamente | string idêntica, sem normalização |

### Três superfícies de versão (registradas separadamente)

```
importlib.metadata.version('hindsight-api-slim')  0.10.1+mastertrend.t22.1  <-- CANÔNICA
hindsight_api.__version__                          0.10.1   (constante de origem, não alterada)
GET /version  {"api_version": ...}                0.10.1   (derivada de __version__)
```

`CANONICAL_VERSION_AUTHORITY` = **metadados do pacote instalado**, gerados pela
wheel construída. É a única superfície que o instalador controla e a que o
updater lê (`get_current_version`).

A segunda e a terceira superfícies reportam a base porque `__version__` é uma
constante no fonte e `/version` deriva dela. **Isso é deliberado e não
enfraquece a validação**: mexer em `__version__` faria o endpoint afirmar uma
identidade que a distribution não tem. A validação de identidade fork é feita
por comparação de **string exata** contra a versão do pacote instalado, e o
endpoint continua sendo útil como referência da linhagem upstream. O relatório
declara as duas, para ninguém ler `api_version: 0.10.1` como downgrade.

## Proveniência do artefato

```
CANONICAL_WHEEL_REPOSITORY=/opt/hindsight-kvm8/packages
  └── releases/hindsight_api_slim-0.10.1+mastertrend.t22.1-py3-none-any.whl
  └── releases/0.10.1+mastertrend.t22.1.manifest.json
```

Convenção existente do host (`/opt/hindsight-kvm8` já é a raiz de releases,
source, current/previous). Proibido: `/tmp`, `site-packages`, diretório de
perfil, árvore de auditoria. `root:root`, `0755` nos diretórios, `0644` no
wheel — sem escrita para o usuário de runtime.

### Determinismo

```
SOURCE_DATE_EPOCH=1790553297   (= timestamp do commit a4397db5)
build #1 sha256 = bfb9b92d…f330bb
build #2 sha256 = bfb9b92d…f330bb   -> BYTE_IDENTICAL=yes
build #3 (sem semente) = b4eec4db…fd2bd  -> diferente
```

O build #3 é o que prova que a semente é load-bearing, e não decoração: sem
ela o hash muda a cada build e nenhum `EXPECTED_SHA256` seria verificável.
Identidade de payload é ainda mais forte que isso — os três arquivos T2.2
dentro da wheel têm sha256 byte-idêntico ao commit de origem.

## Modos de origem do updater

```
UPDATER_SOURCE_MODE=MASTER_TREND_FORK     (fork canônico, hash-verificado)
UPDATER_SOURCE_MODE=OFFICIAL_UPSTREAM     (PyPI; default, comportamento anterior)
```

Modo é **constante explícita**, nunca inferido da presença de arquivos. Os
quatro campos exigidos em modo fork:

```
HINDSIGHT_FORK_PACKAGE_VERSION=0.10.1+mastertrend.t22.1
HINDSIGHT_FORK_LOCAL_SOURCE=/opt/hindsight-kvm8/packages/releases
HINDSIGHT_FORK_EXPECTED_SHA256=bfb9b92d…f330bb
HINDSIGHT_FORK_SOURCE_COMMIT=a4397db59d2a7eb5a859644a20eb6bb8816ffcca
```

Override por variável de ambiente para que o caminho do timer e o caminho do
operador sejam **o mesmo código**, não dois fluxos.

## O gate de proveniência (fail-closed)

`hindsight_source_provenance_gate` roda ANTES de discovery, download, install,
migration, troca de symlink, restart e promoção. Quatro fatos, cada um de uma
fonte de evidência diferente, para que nenhum arquivo corrompido isolado
satisfaça o gate:

1. manifesto existe e faz parse como JSON
2. versão/sha/commit declarados no manifesto batem com os pins do operador
3. a wheel existe no repositório canônico
4. sha256 **real** da wheel == esperado **e** a versão embutida no METADATA da
   wheel == versão alvo

Resultado: `BLOCK_SOURCE_PROVENANCE`, exit 2, zero mutação.

### O que o gate NÃO faz

Não busca no PyPI. Não degrada para `OFFICIAL_UPSTREAM`. Um fork que não pode
ser provado é uma execução **bloqueada**, não um downgrade silencioso. Se um
problema no fork fizesse o updater cair para o oficial, ele apagaria o trabalho
T2 reportando sucesso — exatamente a classe de bug que este gate existe para
eliminar.

### Testes executados (6/6 BLOCK como exigido)

| teste | condição | veredito |
|---|---|---|
| T0 | artefato real presente | `PASS_MASTER_TREND_FORK` |
| T1 | wheel removida | `BLOCK_WHEEL_MISSING` |
| T2 | sha esperado errado | `BLOCK_MANIFEST_MISMATCH` |
| T2b | bytes da wheel corrompidos de verdade | `BLOCK_WHEEL_HASH_MISMATCH` |
| T3 | manifesto truncado | `BLOCK_MANIFEST_CORRUPT` |
| T4 | versão errada no manifesto | `BLOCK_MANIFEST_MISMATCH` |
| T5 | versão fork ≠ versão embutida na wheel | `BLOCK_WHEEL_MISSING` |
| T6 | modo desconhecido | `BLOCK_UNKNOWN_SOURCE_MODE` |

T2b importa mais que T2: T2 muda uma variável, T2b **adultera o artefato real**
e prova que o hash é conferido contra bytes, não contra uma declaração.

## Resolução de dependências

```
MAIN_PACKAGE_SOURCE=LOCAL_CANONICAL
DEPENDENCY_SOURCE=PyPI (índice confiável existente, inalterado)
```

O pacote principal é endereçado por **caminho absoluto da wheel**, o que é mais
forte que `--find-links`: um nome solto deixaria o resolver escolher entre
candidatos; um caminho explícito só pode ser o arquivo que foi verificado por
hash.

Provas:

- `pip install --no-index --find-links <repo local>` → **RC=0** (PyPI totalmente
  desabilitado e ainda resolve ⇒ é o artefato local)
- mesma instalação com diretório vazio → **RC=1** `No matching distribution found`
- `https://pypi.org/pypi/hindsight-api-slim/0.10.1%2Bmastertrend.t22.1/json` → **404**

O 404 é estrutural, não circunstancial: a versão fork **não existe no PyPI**,
logo nem uma regressão do resolver poderia substituir conteúdo oficial por
nesse requisito exato.

## Lógica de versão (não normalizar)

O bug que isso evita: `Version('0.10.1+mastertrend.t22.1')` e `0.10.1` são
*diferentes*, mas uma versão normalizada/stripada passaria a comparar igual à
base e esconderia a diferença. Em modo fork a condição desejada é a identidade
exata do pacote instalado:

```
INSTALLED_VERSION == FORK_PACKAGE_VERSION     (string exata, sem normalizar)
```

Se o runtime reportar só a base upstream, isso é **incompatibilidade real**, não
detalhe de formatação: `BLOCKED_FORK_VERSION_IDENTITY`.

Comparações de faixa usam PEP 440 via `packaging.version.Version` e
`base_version` (linha de release), que é o que faz `0.11.0rc1` ser corretamente
rejeitado pelo teto.

## Guard de política — a versão local não contorna o teto

Testado contra a função `hindsight_target_in_policy` **extraída do updater
vivo**:

```
0.10.1+mastertrend.t22.1  -> yes   (permitida)
0.10.1                    -> yes
0.10.0                    -> yes
0.10.2                    -> yes
0.9.9                     -> no    BELOW_MIN
0.9.0                     -> no    BELOW_MIN
0.11.0                    -> no    AT_OR_ABOVE_MAX_EXCLUSIVE
0.11.0rc1                 -> no    AT_OR_ABOVE_MAX_EXCLUSIVE
1.0.0                     -> no    AT_OR_ABOVE_MAX_EXCLUSIVE
```

O rótulo local vive **dentro** do intervalo porque a política compara a linha
de release. Um fork não é um buraco na política.

## Upstream mais novo: observado, nunca instalado

```
AUTO_CONVERGE_TO_UPSTREAM=no
```

Em modo fork, discovery continua rodando (o timer ainda "vê" o upstream), mas o
resultado é **admissão/informação**:

```
UPSTREAM_LATEST_OBSERVED=0.10.2     (fixture)
FORK_BASE_VERSION=0.10.1
UPSTREAM_NEWER_THAN_FORK_BASE=yes
FORK_REBASE_REQUIRED=yes
UPSTREAM_UPDATE_AVAILABLE_BUT_NOT_INSTALLED=yes
TARGET_VERSION=0.10.1+mastertrend.t22.1   <-- permanece o fork
=> PASS_NO_UPDATE
```

O ramo `DISCOVERY_SUCCESS_UPDATE_AVAILABLE` foi deliberadamente interceptado:
sem a interceptação ele adotaria `DISCOVERY_LATEST` como alvo e instalaria
conteúdo oficial do PyPI por cima do fork.

### Bug encontrado e corrigido durante a certificação

A primeira versão da observação comparava `up.base_version` (que é `str`) com
`base` (que é `Version`), levantando `TypeError` e degradando **toda**
observação para `UNPARSEABLE` — o que faria um upstream mais novo parecer
"sempre igual" e o rebase exigido nunca aparecer. Corrigido com
`up_line = Version(up.base_version)`. A matriz de fixtures prova o comportamento
correto em SAME/NEWER/OLDER/UNPARSEABLE.

## Convergência futura para o upstream (PHASE 12)

```
CONVERGE_TO_UPSTREAM_PLAN=
  1. release oficial nova aparece        -> FORT: o upstream já contém o T2.2?
  2. inspecionar se as correções T2 subiram upstream
  3. rebase do fork, ou descartar commits locais que ficaram obsoletos
  4. rodar os gates (unit + proveniência + hash + fresh install + timer)
  5. certificar
  6. trocar o source mode EXPLICITAMENTE para OFFICIAL_UPSTREAM
     e reconstruir a partir do release oficial
```

O fork **não** é uma suposição permanente. Mas a convergência é uma decisão
humana e explícita — um timer nãoCT deve rebasear e recertificar sozinho.

Rollback também é explícito, nunca um fallback implícito:

```
ROLLBACK_TARGET=0.10.1
ROLLBACK_SOURCE_MODE=OFFICIAL_UPSTREAM
ROLLBACK_COMMAND=hindsight-update-safe.sh --rollback-to-official --apply
```

## Durabilidade do timer

O caminho do timer (`hindsight-update-safe.service` →
`hindsight-update-safe.sh --apply`, 03:17 UTC) não foi special-cased: ele usa o
**mesmo** código de instalação que foi certificado em sandbox. Foi provado que
esse caminho instala a versão fork a partir da origem local canônica, e que
com a versão fork já instalada ele resulta em `PASS_NO_UPDATE` sem reinstalar
nem rebaixar.

## O que NÃO mudou

Tier 1 (`LLM_MAX_CONCURRENT`, `RETAIN_LLM_MAX_CONCURRENT`,
`CONSOLIDATION_LLM_MAX_CONCURRENT`, `RETAIN_WALL_TIMEOUT`, `RETAIN_LLM_TIMEOUT`),
T2.1, async ACK, provider/model/auth/fallback, T2.3, T2.4, DEFAULT do modo
upstream, geometria de release, ciclo de promotion/rollback existente.

## Como verificar

```bash
# proveniência (read-only, sem tocar produção)
/usr/local/bin/hindsight-update-safe.sh --check --source-mode MASTER_TREND_FORK

# fail-closed: esconder a wheel tem que bloquear
mv /opt/hindsight-kvm8/packages/releases/hindsight_api_slim-*.whl /tmp/
/usr/local/bin/hindsight-update-safe.sh --check --source-mode MASTER_TREND_FORK
mv /tmp/hindsight_api_slim-*.whl /opt/hindsight-kvm8/packages/releases/

# suite T2.2 (24 testes, sem rede, sem provider, sem DB)
/opt/hindsight-kvm8/current/venv/bin/python -m pytest -o addopts="" \
  tests/test_t2_2_retain_llm_stage_telemetry.py -q
```

## Não fazer

- Não instalar o fork à mão em `site-packages`: o timer apaga.
- Não renomear `HINDSIGHT_FORK_EXPECTED_SHA256` para o hash de um artefato
  adulterado: o gate existe para tornar isso impossível.
- Não apontar o repositório de artefatos para `/root/hermes-audits` ou `/tmp`.
- Não normalizar a versão fork para `0.10.1` em nenhum log de validação.
- Não deixar `AUTO_CONVERGE_TO_UPSTREAM` em `yes` em modo fork.
- Não editar `hindsight_api.__version__` para "combinar" com o `/version`.

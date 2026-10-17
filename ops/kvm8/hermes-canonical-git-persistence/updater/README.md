Control-plane Hermes Update Safe

Esta cópia versionada é a fonte pública do mecanismo de atualização segura do KVM8. O arquivo host `/usr/local/sbin/hermes-update-git.sh` continua sendo apenas um wrapper; a configuração privada do host não é publicada.

Contrato

- `--check` é a validação padrão e nunca promove.
- `--apply` exige autorização explícita fora deste commit.
- O alvo deve ser SHA completo, congelado uma única vez após o fetch.
- O candidato é uma worktree isolada e o upstream bruto não é executado antes da reconstrução das customizações.
- Os gates são fail-closed e verificam statusbar, PM, Python, SQLite, perfis, Hindsight, gateway, DEV e artefatos TUI.
- O root do checkout e o PM launcher são derivados de `HERMES_UPDATE_GIT_REPO`; não há default para `/opt/hermes-agent-clean`.
- O preflight usa Python 3.14.7/SQLite 3.53.1. O PyYAML referenciado por `HERMES_GATE_PYTHONPATH` é somente dependência de leitura dos gates; não é runtime Hermes e não instala nada.
- O timer systemd permanece disabled até autorização do proprietário.
- Não há reset destrutivo, force-push, cherry-pick indiscriminado, restart implícito ou fallback operacional.

Arquivos

- `hermes-update-git.sh`: updater fail-closed.
- `customizations.yaml`: manifesto de estado desejado.
- `customizations/`: reconciliadores, acceptance gates e deep runtime gates.
- `tests/`: testes semânticos do alvo e da idempotência.

Referências a paths antigos presentes em fixtures negativas ou documentação histórica não são entrypoints operacionais. O gate deve tratá-las como evidência de rejeição, nunca como substituto canônico.

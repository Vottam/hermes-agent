KVM8 — Runbook de próxima atualização segura

1. Identificar a última versão estável

- Ler o manifesto Git desta árvore.
- No KVM8, verificar o release canônico por `hermes --version`, `git rev-parse HEAD`, worktree limpa, Python 3.14.7 e SQLite 3.53.1.
- Confirmar os nove launchers, gateway e Hindsight sem restart.
- Não inferir a estabilidade por branch ou por alias; exigir SHA completo e evidência.

2. Construir o candidato

- Usar exclusivamente o release canônico e o remote upstream oficial.
- Fazer um fetch somente no checkout candidato ou em worktree isolada.
- Fixar um SHA completo; não aceitar branch, tag ambígua, HEAD ou revisão abreviada como alvo de promoção.
- Criar worktree temporária fora do caminho operacional.
- Não executar código upstream bruto como se fosse candidato operacional.

3. Reconstruir customizações

- Aplicar o commit do statusbar somente quando a arquitetura do alvo passar no reconciliador semântico.
- Reconciliar Package Manager, Golden Box, PT-BR, seleção/fallback de modelos, PM dos nove perfis, Hindsight e gates de runtime.
- Não reaplicar patch histórico cegamente: se o upstream já contém a semântica, classificar como ALREADY_SATISFIED; se o anchor mudou, bloquear.
- Não copiar configurações privadas, bancos, sessões ou segredos para o candidato.

4. Validar PM, Python, SQLite e perfis

- `bash -n` no updater e nos wrappers.
- Verificar o interpretador oficial: Python 3.14.7 e SQLite 3.53.1.
- Executar os gates com o interpretador explicitamente selecionado; PyYAML usado pelos gates é somente dependência de leitura e não substitui o runtime Hermes.
- Verificar os nove launchers, HERMES_HOME por perfil, ausência de venv legado e ausência de referências operacionais ao checkout aposentado.
- Validar statusbar por reconciliação/acceptance real no repositório candidato; CLI smoke sozinho não é prova.

5. Promover com handoff independente

- Exigir autorização explícita para `--apply`; esta tarefa somente valida.
- Produzir bundle, hashes, manifesto pre/post e rollback antes de qualquer promoção.
- Usar o handoff independente documentado pelo updater para o gateway; não reiniciar o gateway de dentro do processo principal de promoção.
- Confirmar o PID e a saúde do gateway/Hindsight após o handoff.

6. Rollback

- Se qualquer gate falhar, não promover.
- Se a promoção já tiver iniciado e um gate pós-promo falhar, usar somente o rollback privado e verificado do candidato anterior.
- Não reativar stages, venvs, aliases ou paths antigos como fallback.
- Se o backup privado necessário não existir, classificar BLOCKED e parar.

7. Aposentar a versão anterior

- Primeiro comprovar que o novo release é operacional e que os launchers convergiram.
- Registrar caminhos, proprietários, tamanhos, referências e substituto canônico.
- Remover somente artefatos dispensáveis por allowlist exata.
- Manter backups como arquivos de recuperação, nunca como instalações ou PATH concorrentes.

8. Registrar evidências

- Commit de código, documentação e manifesto em branch exclusiva do KVM8.
- Hash SHA-256 de scripts e templates externos no audit privado.
- Relatório final com HEAD, base upstream, worktree, branch remota, gates, serviços e bloqueios.
- Nunca publicar valores privados.

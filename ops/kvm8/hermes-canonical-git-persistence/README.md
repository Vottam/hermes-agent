KVM8 — Persistência Git canônica do Hermes

Escopo

Esta árvore registra a customização do Hermes 0.22.2 usada no KVM8 e o contrato para futuras atualizações. O código do produto fica no repositório Hermes; configurações externas, credenciais, bancos, sessões, SOUL e backups privados ficam fora do Git público.

Baseline certificado

- Release: 0.22.2+1.g167ec34
- Base upstream: v0.22.2 / d923a5eb487a17c0a68801bba9ef0f56c81040a8
- Customização local principal: 167ec345ffd5a5bd154eaf215a313f13b42c66b3
- Python operacional: 3.14.7+20260901
- SQLite operacional: 3.53.1
- Gateway e Hindsight: preservados; nenhuma reinicialização faz parte desta árvore

Regra de ouro

Há uma única instalação Hermes operacional aprovada. Um candidato é construído em worktree isolada, recebe as customizações semânticas, passa pelos gates e só então pode ser promovido mediante autorização explícita. Backups e artefatos de rollback não são instalações concorrentes e nunca entram no PATH operacional.

Conteúdo

- manifest.yaml: manifesto sanitizado de versão, runtime e customizações.
- RUNBOOK.md: procedimento auditável para próxima atualização.
- ROLLBACK.md: recuperação sem fallback silencioso.
- UPDATER_NOOP.md: contrato e resultado da validação não destrutiva.
- updater/: fonte versionada do control-plane externo e gates, sem config.env privado.
- templates/: modelos sanitizados de configuração externa e systemd.

Segurança pública

Não adicionar a este diretório API keys, tokens, senhas, connection strings, bancos SQLite, sessões, dados pessoais, dumps, arquivos SOUL ou conteúdo privado de perfis. Valores privados necessários à recuperação permanecem no mecanismo de backup restrito do KVM8 e são referenciados somente por hash e caminho privado.

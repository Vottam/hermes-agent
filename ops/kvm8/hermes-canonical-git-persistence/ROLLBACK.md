KVM8 — Procedimento de rollback

Pré-condições

- O rollback é uma ação autorizada, não um fallback automático.
- O release anterior deve estar identificado por SHA completo e constar no audit privado.
- O bundle ou backup restrito deve passar por SHA-256 e verificação Git antes do uso.

Procedimento

1. Parar a promoção se ela ainda estiver no estágio de candidato.
2. Preservar o relatório, o stdout e o estado do candidato; não apagar evidência.
3. Restaurar o release aprovado anterior a partir do bundle/backup privado verificado.
4. Reexecutar runtime-check, repo-guard, nove launchers, statusbar, gateway e Hindsight.
5. Confirmar que nenhum stage aposentado, venv legado ou alias antigo voltou ao PATH.
6. Registrar o resultado e a causa em `/root/hermes-audits/` e no Hindsight privado.

Proibições

- Não usar `git reset --hard` no checkout operacional como mecanismo informal.
- Não fazer force-push.
- Não reativar uma instalação histórica somente porque ela ainda existe em backup.
- Não declarar rollback byte-a-byte de configuração AUDIT quando a cópia pré-alteração não existir.
- Não reiniciar gateway, Hindsight ou bancos sem autorização específica.

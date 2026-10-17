KVM8 — Contrato de validação NOOP do updater

O modo `--check` deve:

- aceitar somente SHA completo quando `--target` for informado;
- fazer no máximo o fetch previsto no clone/caminho controlado;
- congelar o alvo uma única vez;
- construir candidato em worktree isolada;
- executar reconciliadores e gates sem promover;
- não fazer reset, cherry-pick indiscriminado, force-push, restart, chamada LLM ou alteração de banco;
- emitir registro final honesto com `PROMOTION_EXECUTED=no`;
- deixar a worktree e o checkout candidato limpos.

Resultado da auditoria KVM8 desta árvore

- O commit statusbar foi aplicado limpo sobre o `v0.22.2` oficial.
- O updater original foi bloqueado por defaults para `/opt/hermes-agent-clean`, remote aposentado e preflight Python legado.
- A correção candidata tornou o root, o PM launcher e o preflight configuráveis e selecionou Python 3.14.7/SQLite 3.53.1.
- A validação completa do candidato deve permanecer anexada ao relatório privado antes de qualquer promoção.
- Este documento não autoriza `--apply` nem habilita timer.

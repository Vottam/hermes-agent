# Rollback — hermes-statusbar-runtime-context

## Rollback imediato (se patch foi aplicado)

```bash
cd /opt/hermes-agent-clean

# Restaurar arquivos ao estado original
git checkout HEAD -- \
    agent/agent_init.py \
    agent/agent_runtime_helpers.py \
    agent/chat_completion_helpers.py \
    agent/conversation_loop.py \
    cli.py \
    run_agent.py

# Se havia stash de pré-patch:
git stash pop --index
```

## Rollback se patch foi commitado

```bash
cd /opt/hermes-agent-clean

# Reverter o commit (mantém histórico)
git revert HEAD --no-edit

# OU resetar para o commit anterior (descarta o commit)
git reset --hard HEAD~1
```

## Rollback se Hermes update sobrescreveu o patch

```bash
cd /opt/hermes-agent-clean

# Verificar se o patch ainda existe como stash
git stash list

# Se sim:
git stash pop --index

# Se não, reaplicar:
git apply --3way patches/hermes-statusbar-runtime-context/hermes-statusbar-runtime-context.patch
```

## Validação pós-rollback

```bash
# Sintaxe
python -m py_compile agent/agent_init.py agent/agent_runtime_helpers.py \
    agent/chat_completion_helpers.py agent/conversation_loop.py cli.py run_agent.py

# Hermes funcional
hermes --version
hermes doctor
```

## Nota

O patch não altera providers, gateways, Hindsight, secrets ou configs.
O rollback é seguro e não afeta nenhum serviço.

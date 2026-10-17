Se Doctor detectar statusbar ausente/quebrado:
1. Executar: reconcile.py --check
2. Se DEGRADED: reconcile.py --apply (não cherry-pick / stash / patch direto)
3. Verificar com: hermes-update-git.sh --apply (updater canônico)
4. NUNCA usar: hermes-update-safe.sh, git cherry-pick, git stash pop --index, cli.py como fonte

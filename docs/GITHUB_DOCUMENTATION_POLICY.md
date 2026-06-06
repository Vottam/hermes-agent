# GitHub Documentation Policy

Standards for documenting patches and features in this repository.

## Scope

Applies to all reusable patches, operational scripts, and public-facing documentation in this repository.

## Rules

### Every reusable patch MUST have its own README.md

Must include:
- **What it does** — clear, one-line summary
- **Why it matters** — problem it solves, who it's for
- **Before / After** — concrete example of the change
- **Requirements** — version, dependencies, environment
- **Installation** — step-by-step commands
- **Safety** — what it does NOT modify
- **Rollback** — how to revert
- **Files** — table of files in the patch
- **Keywords** — searchable terms

### Every patch MUST be listed in `patches/README.md`

Central index with patch name, description, and relative link.

### Every public feature MUST have a document in `docs/`

For non-trivial features, create a dedicated document explaining:
- Problem and motivation
- Solution architecture
- Installation and usage
- Safety guarantees
- Rollback procedures
- Compatibility
- Links to related issues/discussions

### All scripts MUST have commented headers

```bash
#!/usr/bin/env bash
# <Patch Name> — <One-line purpose>
# Safety: what this script does NOT modify
# Usage: how to run it
```

### NEVER publish secrets

- No API keys, tokens, passwords, or credentials in any file
- No `.env` files with real values
- No hardcoded secrets in scripts or documentation

### Destructive commands require backup/rollback

Every script that modifies state must:
1. Create a backup before modifying
2. Document the exact rollback command
3. Validate the result after applying

### Use clear, searchable paths

- Patches go in `patches/<patch-name>/`
- Documentation goes in `docs/<topic>.md`
- Scripts are named descriptively: `preflight.sh`, `apply.sh`, `test.sh`
- Use lowercase with hyphens

### Language

- Objective and human-readable
- No "forensic documentation" that forces the reader to guess
- Include concrete examples (before/after, command output)
- Write for someone who finds this via search 6 months from now

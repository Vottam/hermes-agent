# Patches

Reusable operational patches for Hermes Agent.

## Available patches

| Patch | Description | Status |
|-------|-------------|--------|
| [Hermes Status Bar Runtime Context](hermes-statusbar-runtime-context/README.md) | Shows runtime-resolved model, provider, and real context usage in the status bar | ✅ tested |

## Quick apply

Each patch directory contains its own README with full instructions. In general:

```bash
cd patches/<patch-name>
./preflight.sh   # validate environment
./apply.sh       # apply the patch
./test.sh        # verify
```

Rollback instructions are in each patch's `rollback.md`.

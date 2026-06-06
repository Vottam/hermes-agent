# Hermes Status Bar Runtime Context

The Hermes status bar displays runtime-resolved model, runtime-resolved provider, and real context usage.

## What it does

Instead of showing the configured alias (`@preset/hermes`) and a static context window, the status bar now shows what the API actually returned:

```
<resolved_model> │ <resolved_provider> │ <used_context>/<max_context>
```

Example:

```
gpt-5.4-mini-2026-03-17 │ openrouter │ 33.2K/272K
```

## Why it matters

- **OpenRouter users**: see which specific model was actually served
- **Fallback chains**: detect when fallback is active (model/provider change)
- **Aliases/presets**: see the concrete model, not the alias
- **Context window**: monitor the real context budget, not the configured value
- **Debugging**: verify runtime behavior without inspecting logs

## Before / After

```
Before: ⚕ hermes │ 19K/256K │ 45%
After:  ⚕ gpt-4o-mini │ openrouter │ 19K/128K │ 15%
```

## Display format

```
<resolved_model> │ <resolved_provider> │ <used_context>/<max_context>
```

With fallback: if resolved values are unavailable, falls back to configured → safe default.

## Quick install

```bash
# Clone the fork
git clone https://github.com/Vottam/hermes-agent.git /root/hermes-patches/hermes-agent

# Navigate to the patch
cd /root/hermes-patches/hermes-agent/patches/hermes-statusbar-runtime-context

# Validate, apply, test
./preflight.sh
./apply.sh
./test.sh
```

Or apply the patch directly in an existing Hermes installation:

```bash
cd /opt/hermes-agent-clean  # or your Hermes project directory
git apply patches/hermes-statusbar-runtime-context/hermes-statusbar-runtime-context.patch
```

## Requirements

- Hermes Agent v0.16.0+
- Python 3.11+

## Files

| File | Description |
|------|-------------|
| `README.md` | This file — overview and quick start |
| `hermes-statusbar-runtime-context.patch` | Clean patch file (253 lines, +107/-9) |
| `preflight.sh` | Validates environment before applying |
| `apply.sh` | Applies the patch |
| `test.sh` | Validates after applying |
| `rollback.md` | Rollback instructions |
| `upstream-proposal.md` | Proposal for upstream merge |
| `comment-on-existing-issue.md` | Comment published on #38006 |
| `new-issue-proposal.md` | Alternative new issue template |

## Safety

- **Display-only**: does not modify provider routing, fallback logic, or auth
- **No secrets**: never displays API keys, tokens, endpoints, or account IDs
- **No Hindsight changes**: memory system is untouched
- **No gateway changes**: messaging platforms are untouched
- **Graceful fallback**: if runtime data is unavailable, falls back to previous behavior
- **Sanitized output**: provider values show only the last segment after `/`

## Rollback

```bash
cd /opt/hermes-agent-clean  # or your Hermes project directory
git checkout HEAD -- \
    agent/agent_init.py \
    agent/agent_runtime_helpers.py \
    agent/chat_completion_helpers.py \
    agent/conversation_loop.py \
    cli.py \
    run_agent.py
```

See `rollback.md` for full instructions.

## Upstream discussion

- **Issue**: [NousResearch/hermes-agent#38006](https://github.com/NousResearch/hermes-agent/issues/38006) — status bar context meter uses model_aliases value
- **Comment published**: [#38006 (comment)](https://github.com/NousResearch/hermes-agent/issues/38006#issuecomment-4639978995)
- **Related issues**: #35427, #33433, #30251, #26877, #6232

Keywords: Hermes Agent, status bar, runtime context, resolved model, resolved provider, OpenRouter, context window, token usage, TUI, CLI, runtime footer, model alias, fallback chain

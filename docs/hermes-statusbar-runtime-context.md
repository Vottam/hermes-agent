# Hermes Status Bar Runtime Context

Technical documentation for the runtime-resolved status bar patch.

## Problem

The Hermes status bar shows **configured** (declared) values from `config.yaml`, not **runtime-resolved** values from the actual API response.

Example of what users see today:

```
⚕ hermes │ 19K/256K │ 45%
```

Where `hermes` is the configured alias, `256K` is the configured context length — neither reflects what is actually happening at runtime.

This creates confusion with:
- **OpenRouter** or other model routers (user can't see which model was actually served)
- **Fallback chains** (status bar still shows the primary model after fallback activates)
- **Model aliases/presets** (shows the alias, not the resolved model)
- **Context window** (shows configured value, not the actual model's context window)

## Solution

The status bar now displays runtime-resolved metadata:

```
<resolved_model> │ <resolved_provider> │ <used_context>/<max_context>
```

Example:

```
gpt-5.4-mini-2026-03-17 │ openrouter │ 33.2K/272K
```

## Three distinct concepts

| Concept | Example | What the user sees now |
|---------|---------|----------------------|
| **Configured** | `@preset/hermes` in config.yaml | `hermes` |
| **Alias resolution** | `@preset/hermes` → `openai/gpt-4o-mini` | (not shown) |
| **Runtime-resolved** | `response.model` = `gpt-4o-mini` | (not shown) |

The status bar should show **#3** — what the API actually returned — falling back to #2 or #1 only when runtime data is unavailable.

## Before / After

```
Before: ⚕ hermes │ 19K/256K │ 45%
After:  ⚕ gpt-4o-mini │ openrouter │ 19K/128K │ 15%
```

## Architecture

```
API response
    │
    ▼
┌─────────────────────────────┐
│  Runtime Metadata Capture   │
│  (agent attributes)         │
│                             │
│  _resolved_model            │
│  _resolved_provider         │
│  _resolved_context_length   │
│  _resolved_context_model    │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  Display Helpers            │
│  (run_agent.py)             │
│                             │
│  get_display_model_name()   │
│  get_display_provider_name()│
│  get_display_context_length()│
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  Status Bar / TUI           │
│  (cli.py)                   │
│                             │
│  _get_status_bar_snapshot() │
│  _build_status_bar_text()   │
└─────────────────────────────┘
```

### Capture points

| File | Function | What is captured |
|------|----------|-----------------|
| `agent/agent_init.py` | `init_agent()` | `_resolved_provider = agent.provider` |
| `agent/agent_runtime_helpers.py` | `try_recover_primary_transport()` | `_resolved_provider = agent.provider` |
| `agent/agent_runtime_helpers.py` | `restore_primary_runtime()` | `_resolved_provider = agent.provider` |
| `agent/agent_runtime_helpers.py` | `switch_model()` | `_resolved_provider = agent.provider` |
| `agent/chat_completion_helpers.py` | `try_activate_fallback()` | `_resolved_provider = agent.provider` |
| `agent/conversation_loop.py` | `run_conversation()` (post-response) | `_resolved_model` from `response.model`, `_resolved_context_length` from `get_model_context_length()` |

### Display helpers (new methods on `AIAgent`)

- `get_display_model_name()` — returns resolved → configured → "unknown"
- `get_display_provider_name()` — returns resolved → configured → ""
- `get_display_context_length()` — returns resolved → compressor → 0

## Fallback chain

| Field | 1st (runtime) | 2nd (mid-session) | 3rd (configured) | Last resort |
|-------|--------------|-------------------|-----------------|-------------|
| Model | `response.model` from API | `agent.model` | `self.model` from config | `"unknown"` |
| Provider | `_resolved_provider` (runtime) | `agent.provider` | `self.provider` | `""` (segment omitted) |
| Context | `get_model_context_length(resolved_model)` | `compressor.context_length` | configured value | `0` (shows `ctx --`) |

## Installation

### From the fork

```bash
git clone https://github.com/Vottam/hermes-agent.git /root/hermes-patches/hermes-agent
cd /root/hermes-patches/hermes-agent/patches/hermes-statusbar-runtime-context
./preflight.sh
./apply.sh
./test.sh
```

### Direct patch application

```bash
cd /opt/hermes-agent-clean  # or your Hermes project directory
git apply patches/hermes-statusbar-runtime-context/hermes-statusbar-runtime-context.patch
```

## Requirements

- Hermes Agent v0.16.0+
- Python 3.11+
- `gh` CLI authenticated (for GitHub operations)

## Files

| File | Description |
|------|-------------|
| `README.md` | Overview and quick start |
| `hermes-statusbar-runtime-context.patch` | Clean patch file (253 lines) |
| `preflight.sh` | Pre-flight validation |
| `apply.sh` | Patch application script |
| `test.sh` | Post-application validation |
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

## Compatibility

- **OpenRouter**: Yes — reads `response.model` which contains the actual served model
- **OpenAI, Anthropic, Google, Ollama, others**: Yes — any provider returning `model` in the response
- **Fallback chains**: Yes — `_resolved_provider` updated on every switch
- **Aliases/presets**: Yes — shows the real model after resolution
- **Backward compatible**: Yes — falls back to current behavior when runtime data is unavailable

## Upstream discussion

- **Issue**: [NousResearch/hermes-agent#38006](https://github.com/NousResearch/hermes-agent/issues/38006) — status bar context meter uses model_aliases value instead of real model context
- **Comment published**: [#38006 (comment)](https://github.com/NousResearch/hermes-agent/issues/38006#issuecomment-4639978995)
- **Related issues**: #35427, #33433, #30251, #26877, #6232

## Testing

The following should be validated:

1. Status bar shows resolved model (not alias) after first API call
2. Status bar shows resolved provider after fallback activation
3. Status bar shows real context usage and window from API response
4. No crash when `response.model` is empty or missing
5. No crash when provider/context metadata is unavailable
6. No secrets, tokens, or sensitive data in status bar output
7. Graceful fallback to configured values when runtime data is unavailable
8. Correct display across all terminal widths (< 52, < 76, ≥ 76, TUI)

## Keywords

Hermes Agent, status bar, runtime context, resolved model, resolved provider, OpenRouter, context window, token usage, TUI, CLI, runtime footer, model alias, fallback chain

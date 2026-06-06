# Upstream Proposal: Runtime-Resolved Model, Provider, and Context in Status Bar

**Target:** NousResearch/hermes-agent (official repository)
**Type:** Feature proposal — consolidation of runtime metadata in status bar
**Component:** Status bar / TUI / CLI / runtime_footer

---

## Summary

This is **not** a request for a brand-new feature. It is a proposal to **consolidate and generalize** existing partial fixes into a coherent, unified approach for displaying runtime-resolved metadata in the Hermes status bar.

Multiple open issues (#38006, #35427, #33433, #30251, #26877, #6232) address individual symptoms of the same root cause: the status bar shows **configured/declared** values instead of **runtime-resolved** ones. This proposal suggests a single architectural pattern that solves all of them at once.

---

## Related Issues

| # | Title | Relation to this proposal |
|---|-------|--------------------------|
| [#38006](https://github.com/NousResearch/hermes-agent/issues/38006) | status bar context meter uses model_aliases/context_length instead of real model context | **Direct** — context meter shows alias value, not runtime model context |
| [#35427](https://github.com/NousResearch/hermes-agent/issues/35427) | runtime_footer with provider/tokens/cost/tools | **Direct** — runtime_footer needs resolved provider, not configured |
| [#33433](https://github.com/NousResearch/hermes-agent/issues/33433) | context bar stuck at 0% when usage=None | **Direct** — context bar lacks real usage data from API response |
| [#30251](https://github.com/NousResearch/hermes-agent/issues/30251) | reasoning level in status bar | **Adjacent** — another field that should reflect runtime state |
| [#26877](https://github.com/NousResearch/hermes-agent/issues/26877) | tps in runtime_footer | **Adjacent** — runtime performance metadata belongs in the same layer |
| [#6232](https://github.com/NousResearch/hermes-agent/issues/6232) | header showing model/thinking in gateway replies | **Adjacent** — display layer should use resolved model name |

---

## Problem Statement

The Hermes status bar currently displays **configured** (declared) values:

```
⚕ hermes │ 19K/256K │ 45%
```

Where `hermes` is the configured alias, `256K` is the configured context length, and neither reflects what is actually happening at runtime.

This creates confusion in real-world setups:

| Scenario | Configured value shown | Actual runtime value |
|----------|----------------------|---------------------|
| `@preset/hermes` alias | `hermes` | `gpt-4o-mini` or whatever the preset resolves to |
| OpenRouter routing | `openrouter` | `openrouter/auto` → `anthropic/claude-sonnet-4` |
| Fallback activation | Primary model name | Fallback model name |
| Model switch mid-session | Original model | New model |
| Context window | Configured `context_length` | Actual model context (may differ from alias config) |

### Three distinct concepts

Users (and the code) conflate three different things:

1. **Configured model/provider** — what the user wrote in `config.yaml` (e.g., `@preset/hermes`, `openrouter`)
2. **Alias/preset** — an intermediate resolution step (e.g., `@preset/hermes` → `openai/gpt-4o-mini`)
3. **Runtime-resolved model/provider** — what the API actually returned (e.g., `gpt-4o-mini` from `response.model`, the actual provider endpoint that served the request)

The status bar should show **#3** (runtime-resolved), falling back to **#2** then **#1** only when runtime data is unavailable.

---

## Proposed Behavior

### Core principle

> The status bar should answer: "What is the agent **actually using right now**?"
> Not: "What did the user configure?"

### Display format

```
<resolved_model> · <resolved_provider> · <used_context>/<max_context>
```

| Terminal width | Example |
|---------------|---------|
| Narrow (< 52) | `⚕ gpt-4o · openrouter · 1m30s` |
| Medium (< 76) | `⚕ gpt-4o openrouter 45%` |
| Wide (≥ 76) | `⚕ gpt-4o openrouter 90K/200K 45%` |
| Rich TUI | `[⚕] gpt-4o [· openrouter] [│ 90K/200K │] 45%` |

### Fallback chain

If runtime-resolved values are not yet available (e.g., before first API call), fall back gracefully:

| Field | 1st (runtime) | 2nd (mid-session) | 3rd (configured) | Last resort |
|-------|--------------|-------------------|-----------------|-------------|
| Model | `response.model` from API | `agent.model` | `self.model` from config | `"unknown"` |
| Provider | `_resolved_provider` (runtime) | `agent.provider` | `self.provider` | `""` (segment omitted) |
| Context window | `get_model_context_length(resolved_model)` | `compressor.context_length` | configured value | `0` (shows `ctx --`) |

### What this consolidates

| Issue | How this proposal addresses it |
|-------|-------------------------------|
| #38006 (context meter wrong) | Context comes from `get_model_context_length(resolved_model)`, not from `model_aliases` config |
| #35427 (runtime_footer provider) | Provider comes from `_resolved_provider`, updated at every provider switch |
| #33433 (context bar 0%) | Context usage comes from API response tokens, not from `usage=None` path |
| #30251 (reasoning level) | Same pattern: capture from API response, display with fallback |
| #26877 (tps) | Same layer: compute from API response timing, display with fallback |
| #6232 (header model) | Display layer uses `get_display_model_name()` which returns resolved name |

---

## Anti-Secret Policy

The status bar **MUST NEVER** display:

- API keys, tokens, or credentials
- Private endpoint URLs (e.g., `http://192.168.x.x:11434`)
- Account IDs, org IDs, or user IDs
- Raw routing metadata (e.g., internal provider selection logic)
- Full provider paths (only the last segment after `/` is shown)

If a provider value contains sensitive information, the display layer should sanitize it to show only the recognizable name (e.g., `openrouter` instead of `https://openrouter.ai/api/v1`).

---

## Implementation Approach

### Architecture

Introduce a **runtime metadata layer** on the agent that captures resolved values at key transition points, and a **display helper layer** that the status bar reads from.

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

```python
def get_display_model_name(self) -> str:
    return (
        getattr(self, "_resolved_model", None)
        or getattr(self, "_resolved_context_model", None)
        or getattr(self, "model", None)
        or "unknown"
    )

def get_display_provider_name(self) -> str:
    return (
        getattr(self, "_resolved_provider", None)
        or getattr(self, "provider", None)
        or ""
    )

def get_display_context_length(self) -> int:
    resolved = getattr(self, "_resolved_context_length", None)
    if resolved is not None:
        return resolved or 0
    compressor = getattr(self, "context_compressor", None)
    return getattr(compressor, "context_length", 0) or 0
```

### Status bar integration

The status bar reads from display helpers instead of directly accessing `agent.model` / `agent.provider`. This decouples display from configuration and ensures the fallback chain is centralized.

---

## Compatibility

- **OpenRouter:** Yes — `response.model` contains the actual served model
- **OpenAI, Anthropic, Google, Ollama, others:** Yes — any provider returning `model` in the response
- **Fallback chains:** Yes — `_resolved_provider` updated on every switch
- **Aliases/presets:** Yes — shows the real model after resolution
- **Backward compatible:** Yes — falls back to current behavior when runtime data is unavailable
- **No breaking changes:** Existing status bar format preserved

## Scope

This change affects only the **status bar display layer**. It does **not** modify:
- Provider routing or fallback logic
- Gateway behavior
- Authentication or secrets handling
- Configuration file format
- Any runtime agent behavior

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `response.model` is empty or missing | Low | Fallback to `agent.model` |
| `get_model_context_length()` fails | Low | Exception caught, fallback to `compressor.context_length` |
| Provider value contains full URL path | Medium | Display only last segment after `/` |
| Terminal too narrow for new segments | Medium | Segments omitted progressively (< 52, < 76 cols) |
| Hermes update overwrites local patch | Medium | Upstream merge eliminates this risk |

## Testing Checklist

1. Status bar shows resolved model (not alias) after first API call
2. Status bar shows resolved provider after fallback activation
3. Status bar shows real context usage and window from API response
4. No crash when `response.model` is empty or missing
5. No crash when provider/context metadata is unavailable
6. No secrets, tokens, URLs, or sensitive data in status bar output
7. Graceful fallback to configured values when runtime data is unavailable
8. Correct display across all terminal widths (< 52, < 76, ≥ 76, TUI)
9. Provider segment omitted (not empty string) when provider is unknown
10. Context shows `ctx --` (not `0/0`) when context length is unavailable

---

**Note:** This proposal consolidates multiple individual issues into a single architectural pattern. The status bar should reflect what the agent is **actually using**, not what was **declared in config**. This is especially valuable for users with OpenRouter, fallback chains, provider routing, model aliases, or multiple models.

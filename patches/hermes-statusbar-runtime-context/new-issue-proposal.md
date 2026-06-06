# New Issue Proposal — Consolidate Runtime-Resolved Metadata in Status Bar

**Title:** Show runtime-resolved model, provider, and context in status bar

---

## Problem

The Hermes status bar shows **configured** values from `config.yaml`, not **runtime-resolved** values from the actual API response. This affects multiple fields and is tracked across several individual issues:

| Field | Currently shows | Should show |
|-------|----------------|-------------|
| Model | Configured alias (`@preset/hermes`) | Resolved model from `response.model` (`gpt-4o-mini`) |
| Provider | Configured provider (`openrouter`) | Actual provider that served the request |
| Context window | `model_aliases.<persona>.config` value | Real context length from `get_model_context_length(resolved_model)` |
| Context usage | Often stuck at 0% (#33433) | Real token usage from API response |

This creates confusion for users with:
- OpenRouter or other model routers
- Fallback chains
- Model aliases/presets
- Multiple providers

## Related Issues

- [#38006](https://github.com/NousResearch/hermes-agent/issues/38006) — context meter uses model_aliases instead of real model context
- [#35427](https://github.com/NousResearch/hermes-agent/issues/35427) — runtime_footer provider/tokens/cost/tools
- [#33433](https://github.com/NousResearch/hermes-agent/issues/33433) — context bar stuck at 0% when usage=None
- [#30251](https://github.com/NousResearch/hermes-agent/issues/30251) — reasoning level in status bar
- [#26877](https://github.com/NousResearch/hermes-agent/issues/26877) — tps in runtime_footer
- [#6232](https://github.com/NousResearch/hermes-agent/issues/6232) — header showing model/thinking in gateway replies

## Proposal

Introduce a **runtime metadata layer** that captures resolved values at key points:

### Capture points

| When | What | Attribute |
|------|------|-----------|
| Agent init | Current provider | `_resolved_provider` |
| Fallback activation | New provider | `_resolved_provider` |
| Transport recovery | Restored provider | `_resolved_provider` |
| Runtime restore | Restored provider | `_resolved_provider` |
| Manual model switch | New provider | `_resolved_provider` |
| API response received | Model name from API | `_resolved_model` |
| API response received | Context length from model metadata | `_resolved_context_length` |

### Display helpers

Add to `AIAgent` in `run_agent.py`:
- `get_display_model_name()` — returns resolved → configured → "unknown"
- `get_display_provider_name()` — returns resolved → configured → ""
- `get_display_context_length()` — returns resolved → compressor → 0

### Status bar integration

The status bar reads from display helpers instead of directly accessing config values. Fallback chain ensures graceful degradation.

### Display format

```
⚕ <resolved_model> · <resolved_provider> · <used>/<max>
```

Example: `⚕ gpt-4o · openrouter · 90K/200K 45%`

## Anti-Secret Policy

The status bar must **never** display:
- API keys, tokens, credentials
- Private endpoint URLs
- Account IDs, org IDs
- Raw routing metadata

Provider values are sanitized to show only the recognizable name (e.g., `openrouter` not the full URL).

## Scope

Display layer only. Does not modify:
- Provider routing or fallback logic
- Gateway behavior
- Authentication
- Configuration format

## Backward Compatibility

Fully backward compatible. When runtime data is unavailable (e.g., before first API call), falls back to current behavior.

## Testing

- [ ] Resolved model shown after first API call
- [ ] Resolved provider shown after fallback
- [ ] Real context usage and window shown
- [ ] No crash when `response.model` is empty
- [ ] No crash when provider/context unavailable
- [ ] No secrets in status bar output
- [ ] Graceful fallback when runtime data unavailable
- [ ] Correct display at all terminal widths

---

**TL;DR:** Multiple open issues share the same root cause — the status bar shows configured values instead of runtime-resolved ones. A single runtime metadata layer fixes them all.

# Comment on Existing Issue — #38006

**Issue:** [TUI] status bar context meter shows model_aliases value instead of real model context
**URL:** https://github.com/NousResearch/hermes-agent/issues/38006

---

The context meter problem in #38006 is one symptom of a broader pattern: **the status bar shows configured values instead of runtime-resolved ones**.

There are three distinct concepts the status bar conflates:

| Concept | Example | What the user sees now |
|---------|---------|----------------------|
| **Configured** | `@preset/hermes` in config.yaml | `hermes` |
| **Alias resolution** | `@preset/hermes` → `openai/gpt-4o-mini` | (not shown) |
| **Runtime-resolved** | `response.model` = `gpt-4o-mini` | (not shown) |

The status bar should show **#3** — what the API actually returned — falling back to #2 or #1 only when runtime data is unavailable.

### Proposed fix

Introduce a runtime metadata layer that captures resolved values at key points:

- `response.model` → `_resolved_model` (after each API response)
- Current provider → `_resolved_provider` (at init, fallback, recovery, switch)
- `get_model_context_length(resolved_model)` → `_resolved_context_length`

The status bar reads from these with a fallback chain: `resolved` → `mid-session` → `configured` → `safe default`.

### What this consolidates

- **#38006:** context meter uses `_resolved_context_length`, not `model_aliases` config
- **#35427:** runtime_footer provider uses `_resolved_provider`
- **#33433:** context bar uses real token usage from API response
- **#30251, #26877:** reasoning level and tps follow the same pattern

### Example

```
Before: ⚕ hermes │ 19K/256K │ 45%
After:  ⚕ gpt-4o-mini │ 19K/128K │ 15%
```

### Anti-secret policy

The display layer sanitizes provider values (last segment only) and never exposes API keys, endpoints, account IDs, or raw routing metadata.

### Working implementation

I've implemented and tested this on Hermes v0.16.0: 6 files, +107/-9 lines, 3 display helpers, capture at 6 points, graceful fallback, 20/20 tests passing. Happy to share the patch or open a PR.

---

**TL;DR:** #38006, #35427, #33433 share a root cause. A single runtime metadata layer fixes all three. Working implementation available.

# Changelog

### 2026-09-19 - Trusted Console Ingress and Structured Trace (Azeroth Control fork)

Fork of `Hokken/mod-llm-guide` at `4b89056`, kept minimal and upstreamable.

* **One submission path**: `.ag`, the `AzerothGuide` whisper and the new
  console ingress all call `SubmitGuideRequest()`, which owns validation
  (500 bytes, 8 lines, no control characters, valid UTF-8), cooldown,
  pending limits, the live character context/snapshot and the queue row.
* **Console-only control**: `agctl submit` / `agctl clear` (`SEC_CONSOLE`)
  with strict token, GUID and base64url parsing and one machine-readable
  `AZC_GUIDE_CONTROL` line per command, never containing question text.
* **Origin-aware queue**: rows record `origin` (`ingame` or `web`) and a
  unique `external_request_id`; the world script delivers only in-game rows.
* **Structured trace**: `tools/guide_trace.py` records routing and answer
  rounds, tool status/timing/marker counts, scalar schema arguments,
  provider timing and the final grounding state, persisted by
  `save_response()` / `save_error()`. No results, SQL, prompts, payloads or
  reasoning are stored; failures keep a fixed error code only.
* **Safe migration**: an idempotent update file plus the matching bridge
  migration. The base SQL file is unchanged so AzerothCore never re-applies
  it and wipes `llm_guide_memory`.

### 2026-09-14 - Model Compatibility and Provider Switching

* **Five provider backends**: The guide can use Anthropic, OpenAI, Google
  Gemini, OpenRouter, or a local Ollama server through documented, independent
  provider settings.
* **Model-aware requests**: OpenAI-compatible calls now select safe token,
  temperature, and reasoning parameters from the provider and model ID.
  Explicit unsupported-parameter responses receive bounded retries and
  process-local compatibility caching.
* **Reasoning-safe budgets**: `LLMGuide.OpenAI.MaxTokensMultiplier` protects
  visible answers from hidden reasoning-token use while preserving the normal
  budget for models running with supported `reasoning_effort = none`.
* **Ollama tool reliability**: Thinking can be disabled with
  `LLMGuide.Ollama.DisableThinking`. Because Ollama ignores `tool_choice`, an
  empty routing result falls through to deterministic and normal tool routing,
  and the final response round omits tool definitions entirely.
* **Server-owned Ollama context**: Removed the ineffective per-request context
  option. The README and configuration template now explain
  `OLLAMA_CONTEXT_LENGTH`, Modelfile `num_ctx`, and verification with
  `ollama ps`.
* **Configuration and documentation**: Provider examples, model-ID formats,
  configuration defaults, and runtime fallbacks are synchronized. Fine-tuned
  OpenAI IDs inherit their base-model profile.

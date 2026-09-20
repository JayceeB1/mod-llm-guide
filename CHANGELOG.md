# Changelog

### 2026-09-20 - Standalone Questions Ignore the Session (Azeroth Control fork)

* **Conversation resolver only when needed**: `route_question()` used to call
  the conversation resolver (an LLM call) on every request of a character that
  had any Session History. A `clarify` or `unsupported` answer ended the
  request before a tool ran, even for a question that named everything it
  needed, and the stored refusal then fed the next request. In a live
  qualification only 5 of 14 independent questions reached the tools.
  `guide_conversation.needs_conversation_context()` now decides, and
  `process_request()` applies it: a question that is worded as a question,
  names its target (proper noun, quoted name, numeric ID or a named service)
  and carries no reference to an earlier turn is handled exactly as in a new
  session, with no resolver call, no replayed turns and no "previously
  discussed topics" in the prompt. Anything else keeps the resolver as before.
* **Memory is unchanged**: it stays enabled and every request is still stored.
  The trace reports the turns actually replayed (0 for a standalone question).
* **Tests**: first tests of the fork, `python -m unittest discover -s tests`.
  They cover a standalone question after five memories going straight to the
  tools, a real follow-up using the conversation context, and a topic change
  ignoring the old context, both at the routing level and through
  `process_request()`.

### 2026-09-19 - Lease Recovery Without Deadlocks (Azeroth Control fork)

* **Bridge poll loop**: `fetch_pending_requests()` now finds expired leases
  with a plain read and requeues them by primary key. The previous locking
  `UPDATE ... WHERE status = 'processing'` also locked the row a worker was
  completing, in the opposite index order, and deadlocked with
  `save_response()` (MySQL 1213), losing that answer. A disposable-schema
  stress run (2 workers + poll loop, 20 s) went from 281 save deadlocks to 0.

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

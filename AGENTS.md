# ha-llm-context

Collects Home Assistant traces, history, logbook entries, and the error log;
condenses them into short timelines; and sends them with a question to a local LLM
through an OpenAI-compatible API. It's a troubleshooting aid, not an HA
integration: it runs outside HA and only reads from it.

Status: design only. No code yet. Update this file as the layout takes shape.

## Design

- **Three stages, kept separate:** collect (HA APIs → raw JSON), condense (raw →
  timeline text), and ask (timeline + context → model). Each stage should run on its
  own, so a saved raw pull can be condensed again without hitting HA, and a timeline
  can be read without a model.
- **Condense in code, reason in the model.** Local models have small context
  windows and lose track in noise. Filter to the requested entities and window, keep
  only the attributes that matter, collapse repeated lines, and say what was dropped.
  Don't hand the model raw history.
- **Data sources:** WebSocket `trace/list` and `trace/get` for automation traces;
  REST `/api/history/period/<start>` (without `minimal_response`, so attributes come
  back), `/api/logbook/<start>`, and `/api/error_log`. Prefer the APIs to reading
  the recorder database.
- **Model endpoint:** any OpenAI-compatible `/v1/chat/completions` (Ollama,
  llama.cpp, LM Studio), set by URL and model name. Don't tie it to one runtime.
- **Context files:** the user can attach files, such as an automation's YAML or a
  project's notes, so the model knows what the automation is meant to do.
- **Read-only against HA.** Never call services or write state.

## Secrets and privacy

- The HA URL, token, and model endpoint come from environment variables or `.env`,
  which is git-ignored. Never put them in code, tests, or examples.
- Pulled data (`out/`) is git-ignored. It holds entity names, home layout, and
  presence history, so don't commit it or use real pulls as test fixtures.
  Make fixtures from invented entities.
- The point is that data stays local. Don't add a hosted-model default.

## Conventions

- Python 3.12+. Keep dependencies few: an HTTP client and a WebSocket client.
- Tests must not need a live HA instance or a model. Use recorded or invented
  fixtures.
- MIT licensed (`LICENSE`).

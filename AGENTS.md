# ha-llm-context

Collects Home Assistant traces, history, logbook entries, and the error log;
condenses them into short timelines; and sends them with a question to a local LLM
through an OpenAI-compatible API. It's a troubleshooting aid, not an HA
integration: it runs outside HA and only reads from it.

Status: all three stages work against a live HA instance, and the Open WebUI
tool works inside Open WebUI. The MCP server is new: tested with the SDK's own
client over stdio and HTTP, not yet with Open WebUI or LM Studio. Update this file as the layout changes.

## Layout

- `src/ha_llm_context/config.py`: `.env` loading, settings, `--since`/`--until` parsing
- `src/ha_llm_context/collect.py`: REST + WebSocket pulls into a run directory
  (`meta.json`, `history.json`, `logbook.json`, `traces.json`, and `error_log.txt`
  or, when HA writes no log file and `/api/error_log` returns 404, `system_log.json`)
- `src/ha_llm_context/condense.py`: run directory → `timeline.txt`; pure, no network
- `src/ha_llm_context/ask.py`: prompt building and the `/chat/completions` call
- `src/ha_llm_context/pipeline.py`: entity search and collect-then-condense in one
  call; shared by the CLI and the Open WebUI tool
- `src/ha_llm_context/tools.py`: the two model-facing tools (names, descriptions,
  parameter text, defaults, error handling) shared by both front ends below
- `src/ha_llm_context/mcp_server.py`: MCP server (stdio, or streamable HTTP with an
  optional bearer token from `MCP_TOKEN`); needs the `mcp` extra
- `src/ha_llm_context/cli.py`: `find`, `collect`, `condense`, `ask`, `run`, `mcp` subcommands
- `openwebui/ha_timeline_tool.py`: Open WebUI tool (pasted into Open WebUI, which
  installs this package from GitHub). It must stay a single file that imports
  `ha_llm_context`; keep logic in the package, not here. Settings are Valves.
- `mcp/`: how to run the MCP server, and a compose file; `Dockerfile` at the root
- **Parity:** the MCP server and the Open WebUI tool offer the same tools. Change
  the text in `tools.py`, then copy it into the Open WebUI tool's docstrings
  (Open WebUI reads them from the pasted file). `tests/test_tool_parity.py`
  fails until they match.
- `tests/test_traces.py`: automation variables, run timing, related entities
- `tests/test_mcp_server.py`, `tests/test_tool_parity.py`: MCP server and parity
- `tests/fixtures/run1/`: an invented run (hallway motion light blocked by a
  lux condition). Extend it, or add new invented runs, instead of using real pulls.

Dev setup: `python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest` (the test extra includes the MCP SDK)

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

- Python 3.11+. Keep dependencies few: an HTTP client and a WebSocket client; the MCP SDK
  only as the optional `mcp` extra.
- Tests must not need a live HA instance or a model. Use recorded or invented
  fixtures.
- MIT licensed (`LICENSE`).

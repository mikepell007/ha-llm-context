# ha-llm-context

Pull Home Assistant traces, history, logbook entries, and errors; condense them into
a short per-entity timeline; and ask a local LLM what went wrong.

> **Status: early.** All three stages work against invented test data but
> haven't been tested much against a live instance yet.

## Why

Troubleshooting an automation usually means lining up four sources by hand: the
automation trace, state history, the logbook, and the error log. Raw, they're far
too large for a local model's context window. This tool does the collecting and
trimming in code, so the model gets a short timeline and a specific question.

Everything stays local: your HA instance, your machine, your model.

## How it works

1. **Collect** from your HA instance using a long-lived access token:
   - automation traces over the WebSocket API (`trace/list`, `trace/get`)
   - state and attribute history: `GET /api/history/period/<start>`
   - logbook: `GET /api/logbook/<start>`
   - error log: `GET /api/error_log`
2. **Condense** into one timeline per entity group: one line per change, with only
   the attributes that matter, and repeated lines collapsed.
3. **Ask** any OpenAI-compatible endpoint (Ollama, llama.cpp server, LM Studio).
   The prompt holds the timeline, optional context files (an automation's YAML,
   project notes), and your question.

## Requirements

- Home Assistant with a long-lived access token (Profile → Security)
- Python 3.12+
- A local model served behind an OpenAI-compatible API

## Usage

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # then fill in HA_URL, HA_TOKEN, LLM_BASE_URL, LLM_MODEL

# Everything at once: collect the last 2 hours, condense, and ask
ha-llm-context run --since 2h \
  -e automation.hallway_motion_light -e binary_sensor.hallway_motion -e light.hallway \
  -c hallway.yaml -q "Why didn't the hallway light turn on at 19:02?"

# Or one stage at a time
ha-llm-context collect -e light.hallway --since 1d      # prints out/<timestamp>
ha-llm-context condense out/<timestamp> --print          # writes timeline.txt
ha-llm-context ask out/<timestamp> -q "..."              # uses timeline.txt
```

Any `automation.*` entity you pass also pulls that automation's traces from
the window. Use `--attr NAME` to keep extra attributes beyond the per-domain
defaults, and `--tz` to choose the display time zone. The timeline ends with a
"Dropped" section that counts everything left out.

## Tips

- By default HA keeps only the last 5 traces per automation. To keep more, add
  this to the automation's YAML:

  ```yaml
  trace:
    stored_traces: 50
  ```

- History requests that leave out `minimal_response` include attributes, such as
  a climate entity's `hvac_action` and `current_temperature`.

## License

MIT. See [LICENSE](LICENSE).

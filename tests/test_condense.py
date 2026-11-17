from datetime import UTC, datetime
from pathlib import Path

from ha_llm_context.condense import Dropped, Options, condense, history_events, parse_error_log

FIXTURE = Path(__file__).parent / "fixtures" / "run1"
OPTS = Options(tz=UTC)


def lines_for(timeline: str, entity: str) -> list[str]:
    return [ln for ln in timeline.splitlines() if f"  {entity} " in ln]


def test_state_changes_and_start_values():
    tl = condense(FIXTURE, OPTS)
    motion = lines_for(tl, "binary_sensor.hallway_motion")
    assert [ln.split(None, 2)[2] for ln in motion] == ["at start: off", "off → on", "on → off"]
    assert "41 → 38 lx" in tl
    # None-valued attributes aren't shown on the starting line.
    assert "brightness=None" not in tl


def test_unchanged_rows_are_dropped_and_counted():
    tl = condense(FIXTURE, OPTS)
    assert "- 2 history rows with no change to state or kept attributes" in tl


def test_trace_run_shows_outcome_and_why_condition_failed():
    tl = condense(FIXTURE, OPTS)
    assert "RUN 4f2a9c1e trigger: state of binary_sensor.hallway_motion (off → on) → stopped: a condition was false" in tl
    assert "condition/0/entity_id/0: FAIL (state=38, wanted_state_below=30)" in tl
    assert '"below":30' in tl  # automation config included


def test_logbook_duplicates_are_folded():
    tl = condense(FIXTURE, OPTS)
    assert "logbook:" not in tl
    assert "light.kitchen" not in tl
    assert "- 2 logbook entries merged into history lines" in tl
    assert "- 1 logbook automation entries covered by a trace" in tl


def test_error_log_filtered_deduped_and_traceback_summarised():
    tl = condense(FIXTURE, OPTS)
    assert "Before the window" not in tl
    assert "Info line" not in tl
    assert "Failed to send command to light.hallway | TimeoutError: device did not respond" in tl
    assert "Update failed (×3, last at 19:15:00)" in tl


def test_events_are_chronological():
    tl = condense(FIXTURE, OPTS)
    events = tl.split("## Events\n")[1].split("\n\n")[0].splitlines()
    times = [ln[:8] for ln in events if ln[:2].isdigit()]
    assert times == sorted(times)


def test_no_config_option():
    assert '"below":30' not in condense(FIXTURE, Options(tz=UTC, include_config=False))


def test_busy_entity_is_capped():
    rows = [{"entity_id": "sensor.fake_power", "state": str(i), "attributes": {},
             "last_changed": f"2026-03-07T18:{i:02d}:00+00:00",
             "last_updated": f"2026-03-07T18:{i:02d}:00+00:00"} for i in range(50)]
    dropped = Dropped()
    start = datetime(2026, 3, 7, 18, tzinfo=UTC)
    events = history_events([rows], start, Options(tz=UTC, max_changes_per_entity=10), dropped)
    assert len(events) == 11  # 5 head + marker + 5 tail
    assert "40 more changes omitted" in events[5].text
    assert sum(dropped.counts.values()) == 40


def test_parse_error_log_keeps_continuation_lines_with_record():
    text = (
        "2026-03-07 19:00:00.000 ERROR (MainThread) [x.y] Boom\n"
        "Traceback (most recent call last):\n"
        "ValueError: bad\n"
        "2026-03-07 19:00:01.000 WARNING (MainThread) [x.z] Hmm\n"
    )
    recs = parse_error_log(text, UTC)
    assert [(r.level, r.message) for r in recs] == [("ERROR", "Boom | ValueError: bad"), ("WARNING", "Hmm")]


SYSTEM_LOG_RUN = Path(__file__).parent / "fixtures" / "run_system_log"


def line_ends(tl: str, marker: str, suffix: str) -> bool:
    return any(ln.endswith(suffix) for ln in tl.splitlines() if marker in ln)


def test_system_log_fallback_is_condensed():
    tl = condense(SYSTEM_LOG_RUN, OPTS)
    assert "Note: /api/error_log returned 404; used system_log/list" in tl
    # Grouped entry that began before the window: shown at window start, with full-date first time.
    [line] = [ln for ln in tl.splitlines() if "fake_thermostat" in ln]
    assert line.startswith("18:00:00")
    assert line.endswith("Timeout talking to climate.fake_office | TimeoutError: no reply "
                         "(×14 total, first at 2026-03-07 12:00:00, last at 19:40:00)")
    assert line_ends(tl, "fake_weather", "Update failed")
    assert "Long ago" not in tl
    assert "- 1 error-log records outside the window" in tl


def test_steps_are_labelled_from_config_and_if_branches_folded():
    from ha_llm_context.condense import step_lines

    config = {"actions": [
        {"if": "{{ fake_ready }}", "then": [{"action": "light.turn_on"}]},
        {"repeat": {"for_each": [], "sequence": []}},
        {"alias": "Tell someone", "action": "notify.fake"},
    ]}
    steps = {
        "trigger/0": [{}],
        "action/0": [{}],
        "action/0/if": [{"result": {"result": False}}],
        "action/0/if/condition/0": [{"result": {"result": False, "entities": []}}],
        "action/1": [{}],
        "action/2": [{"result": {"params": {"domain": "notify", "service": "fake", "service_data": {}}}}],
    }
    assert step_lines(steps, config) == [
        "action/0: if {{ fake_ready }} → false",
        "action/1: repeat for_each: ran",
        "action/2: Tell someone: called notify.fake",
    ]


def test_runs_with_identical_steps_are_shown_once():
    from ha_llm_context.condense import trace_events

    def run(rid, minute, result):
        return {"run_id": rid, "script_execution": "finished",
                "timestamp": {"start": f"2026-03-07T19:{minute:02d}:00+00:00",
                              "finish": f"2026-03-07T19:{minute:02d}:00.1+00:00"},
                "trace": {"action/0": [{"result": {"result": result}}]}}

    # A, B, A: not back to back, so the third run points at the first.
    runs = [run("aaaaaaaa1", 1, True), run("bbbbbbbb2", 2, False), run("cccccccc3", 3, True)]
    start = datetime(2026, 3, 7, 18, tzinfo=UTC)
    events, _ = trace_events({"automation.fake": {"runs": runs}}, start,
                             start.replace(hour=20), OPTS, Dropped())
    assert events[0].detail == ["action/0: pass"]
    assert events[1].detail == ["action/0: FAIL"]
    assert events[2].detail == ["(same steps as RUN aaaaaaaa)"]


def test_large_config_is_left_out_unless_asked():
    import json, shutil, tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        shutil.copytree(FIXTURE, run_dir)
        traces = json.loads((run_dir / "traces.json").read_text())
        traces["automation.hallway_motion_light"]["runs"][0]["config"]["description"] = "x" * 3000
        (run_dir / "traces.json").write_text(json.dumps(traces))
        assert "left out (" in condense(run_dir, OPTS)
        assert "x" * 3000 in condense(run_dir, Options(tz=UTC, config_limit=None))


def _run(rid, second, code, steps=None):
    return {"run_id": rid, "state": "stopped", "script_execution": code,
            "timestamp": {"start": f"2026-03-07T19:00:{second:02d}+00:00",
                          "finish": f"2026-03-07T19:00:{second:02d}.5+00:00"},
            "trace": steps or {"trigger/0": [{}]}}


def test_rejected_triggers_fold_and_trace_limit_is_flagged():
    from ha_llm_context.condense import trace_events

    runs = [_run(f"r{i}xxxxxxx", i, "failed_single") for i in range(4)]
    traces = {"automation.fake": {"stored": 4, "runs": runs}}
    start = datetime(2026, 3, 7, 18, tzinfo=UTC)
    events, _ = trace_events(traces, start, start.replace(hour=20), OPTS, Dropped())
    texts = [e.text for e in events]
    assert any(t.startswith("4 triggers NOT RUN: an earlier run was still in progress") for t in texts)
    assert any("HA keeps only 4 traces" in t and "stored_traces" in t for t in texts)


def test_no_trace_limit_warning_when_older_traces_exist():
    from ha_llm_context.condense import trace_events

    traces = {"automation.fake": {"stored": 5, "runs": [_run("aaaaaaaa", 1, "finished")]}}
    start = datetime(2026, 3, 7, 18, tzinfo=UTC)
    events, _ = trace_events(traces, start, start.replace(hour=20), OPTS, Dropped())
    assert [e.text for e in events] == ["RUN aaaaaaaa trigger: ? → finished"]


def test_entity_without_history_is_called_out():
    from ha_llm_context.condense import missing_history

    start = datetime(2026, 3, 7, 18, tzinfo=UTC)
    rows = [[{"entity_id": "light.fake"}]]
    [ev] = missing_history(rows, {"light.fake", "automation.fake"}, start)
    assert ev.entity == "automation.fake" and "excluded from the recorder" in ev.text

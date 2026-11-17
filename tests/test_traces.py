"""Automation variables, run timing, and related entities (invented data)."""

from datetime import UTC, datetime

from ha_llm_context.collect import find_entity_refs, related_entities
from ha_llm_context.condense import (Dropped, Options, flatten, missing_history, run_timing,
                                     run_variables, trace_events, used_variables,
                                     variable_values, var_lines)

START = datetime(2026, 3, 7, 18, tzinfo=UTC)
END = datetime(2026, 3, 7, 20, tzinfo=UTC)
OPTS = Options(tz=UTC)

CONFIG = {
    "variables": {"limit": 5, "plan": "{{ ... }}", "helper": "input_select.fake_mode"},
    "actions": [
        {"if": "{{ plan.mode != states(helper) }}", "then": [{"action": "input_select.select_option"}]},
        {"repeat": {"for_each": "{{ plan.rooms }}", "sequence": [{"delay": 30}]}},
    ],
}


def _vars(mode, demand):
    return {"this": {"entity_id": "automation.fake"}, "trigger": {"platform": "time"},
            "limit": 5, "helper": "input_select.fake_mode",
            "plan": {"mode": mode, "rooms": [
                {"head": "climate.fake_a", "sensor": "sensor.fake_a", "temp": 20.5, "demand": demand, "note": ""},
                {"head": "climate.fake_b", "sensor": "sensor.fake_b", "temp": 22, "demand": "neutral", "note": None},
            ]}}


def _run(rid, start, finish, variables, steps_extra=None):
    steps = {"trigger/0": [{"timestamp": start, "changed_variables": variables}],
             "action/0": [{"timestamp": start, "result": {}}],
             "action/0/if": [{"timestamp": start, "result": {"result": False}}]}
    steps.update(steps_extra or {})
    return {"run_id": rid, "state": "stopped", "script_execution": "finished",
            "timestamp": {"start": start, "finish": finish}, "trace": steps, "config": CONFIG}


def test_run_variables_skip_internal_names():
    assert set(run_variables({"t": [{"changed_variables": _vars("heat", "x")}]})) == {"limit", "helper", "plan"}


def test_used_variables_are_those_the_actions_reference():
    used, paths = used_variables(CONFIG, {"limit", "helper", "plan"})
    assert used == {"plan", "helper"}  # limit is only used to build other variables
    assert paths == ["plan.mode", "plan.rooms"]


def test_list_items_are_flattened_slimmed_and_skip_empties():
    flat = {}
    flatten(_vars("heat", "high")["plan"], "plan", flat)
    assert flat["plan.rooms[0].head"] == "climate.fake_a"
    assert "plan.rooms[0].sensor" not in flat  # second entity ID in an item is wiring
    assert "plan.rooms[0].note" not in flat and "plan.rooms[1].note" not in flat
    assert flat["plan.rooms[0].demand"] == "high"


def test_var_lines_first_run_then_changes_only():
    v1 = variable_values(_vars("heat", "neutral"), *used_variables(CONFIG, {"plan", "helper", "limit"}))
    first = var_lines(v1, None, 2000)
    assert first[0].startswith("vars: plan.mode=heat")
    assert any(ln.strip().startswith("plan.rooms[0]: head=climate.fake_a, temp=20.5, demand=neutral")
               for ln in first)
    v2 = variable_values(_vars("cool", "high"), *used_variables(CONFIG, {"plan", "helper", "limit"}))
    changed = var_lines(v2, v1, 2000)
    assert changed == ["vars changed: plan.mode: heat → cool",
                       "  plan.rooms[0]: demand: neutral → high"]
    assert var_lines(v1, v1, 2000) == []


def test_var_budget_reports_what_was_left_out():
    v = variable_values(_vars("heat", "neutral"), {"plan"}, ["plan.mode"])
    lines = var_lines(v, None, 20)
    assert lines[-1].strip().startswith("… ") and "over the size limit" in lines[-1]


def test_slow_run_names_its_slowest_step():
    run = _run("r1", "2026-03-07T19:00:00+00:00", "2026-03-07T19:02:05+00:00", {}, {
        "action/1": [{"timestamp": "2026-03-07T19:00:01+00:00", "result": {}}],
        "action/1/repeat/sequence/0": [{"timestamp": "2026-03-07T19:00:02+00:00", "result": {}}],
    })
    took, slowest = run_timing(run, CONFIG, END, OPTS)
    assert took == " (took 2m05s)"
    assert slowest == "slowest step: action/1/repeat/sequence/0 (repeat for_each) took 2m03s"


def test_fast_run_shows_no_timing_and_running_run_says_so():
    fast = _run("r1", "2026-03-07T19:00:00+00:00", "2026-03-07T19:00:00.02+00:00", {})
    assert run_timing(fast, CONFIG, END, OPTS) == ("", None)
    running = _run("r2", "2026-03-07T19:59:00+00:00", None, {})
    took, _ = run_timing(running, CONFIG, END, OPTS)
    assert took == " (running for at least 1m00s)"


def _at(minute, mode, demand, temp=20.5):
    v = _vars(mode, demand)
    v["plan"]["rooms"][0]["temp"] = temp
    return _run(f"run{minute:05d}", f"2026-03-07T19:{minute:02d}:00+00:00",
                f"2026-03-07T19:{minute:02d}:00.1+00:00", v)


def test_back_to_back_same_step_runs_fold_with_net_change():
    runs = [_at(0, "heat", "neutral", 20.5), _at(1, "heat", "neutral", 20.6),
            _at(2, "heat", "neutral", 20.5), _at(3, "heat", "high", 20.5)]
    dropped = Dropped()
    events, _ = trace_events({"automation.fake": {"stored": 50, "runs": runs}}, START, END, OPTS, dropped)
    assert len(events) == 2
    assert any(d.startswith("vars: plan.mode=heat") for d in events[0].detail)
    assert events[1].text.startswith("3 more runs with the same steps → finished, until {t:")
    # The temperature wobbled and came back; only the demand change is listed.
    assert events[1].detail == [
        "vars, net change over these runs: plan.rooms[0]: demand: neutral → high",
        "(1 other values changed and changed back)"]
    assert dropped.counts["automation runs folded into a 'more runs with the same steps' line"] == 3


def test_slow_run_is_never_folded():
    slow = _at(1, "heat", "neutral")
    slow["timestamp"]["finish"] = "2026-03-07T19:01:30+00:00"
    runs = [_at(0, "heat", "neutral"), slow, _at(2, "heat", "neutral")]
    events, _ = trace_events({"automation.fake": {"stored": 50, "runs": runs}}, START, END, OPTS, Dropped())
    assert len(events) == 3
    assert "(took 30s)" in events[1].text


def test_find_entity_refs_only_keeps_real_entities_in_order():
    known = {"climate.fake_a", "input_select.fake_mode", "sensor.fake_a"}
    assert find_entity_refs(CONFIG, known) == ["input_select.fake_mode"]
    cfg = {"x": "{{ states('sensor.fake_a') }} and climate.fake_a and plan.mode", "climate.fake_a": 1}
    assert find_entity_refs(cfg, known) == ["sensor.fake_a", "climate.fake_a"]


def test_related_entities_respect_limit_and_exclusions():
    traces = {"automation.fake": {"config": {"a": "sensor.one sensor.two light.three automation.fake"}, "runs": []}}
    known = {"sensor.one", "sensor.two", "light.three", "automation.fake"}
    related, dropped = related_entities(traces, known, {"automation.fake", "sensor.one"}, limit=1)
    assert related == {"automation.fake": ["sensor.two"]} and dropped == 1


def test_missing_related_history_is_one_line():
    events = missing_history([], {"automation.fake", "sensor.one", "sensor.two"}, START,
                             related={"sensor.one", "sensor.two"})
    assert [e.text for e in events] == [
        "no history recorded (excluded from the recorder, or the entity ID is wrong); "
        "its state and attributes are unknown",
        "no history recorded for: sensor.one, sensor.two"]

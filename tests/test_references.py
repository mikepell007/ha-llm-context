from ha_llm_context import references, tools
from ha_llm_context.config import HASettings
from ha_llm_context.references import Lookup, render, scan_config

TARGET = "sensor.fake_office_temp"

STATES = [
    {"entity_id": TARGET, "state": "21.5", "attributes": {"friendly_name": "Office temp"}},
    {"entity_id": "sensor.fake_office_temp_2", "state": "20", "attributes": {}},
    {"entity_id": "automation.fake_heat", "state": "on", "attributes": {"friendly_name": "Office heat"}},
    {"entity_id": "automation.fake_coordinator", "state": "on", "attributes": {"friendly_name": "Coordinator"}},
    {"entity_id": "automation.fake_unrelated", "state": "on", "attributes": {}},
    {"entity_id": "automation.fake_by_device", "state": "on", "attributes": {}},
    {"entity_id": "automation.fake_broken", "state": "off", "attributes": {}},
    {"entity_id": "script.fake_report", "state": "off", "attributes": {"friendly_name": "Report"}},
]

CONFIGS = {
    "automation.fake_heat": {
        "triggers": [{"trigger": "state", "entity_id": TARGET}],
        "conditions": [{"condition": "numeric_state", "entity_id": [TARGET], "below": 19}],
        "actions": [{"action": "climate.set_hvac_mode", "target": {"entity_id": "climate.fake_office"}}],
    },
    "automation.fake_coordinator": {   # old singular keys; only templates mention it
        "trigger": [{"platform": "time_pattern", "minutes": "/5"}],
        "variables": {"rooms": "{{ [states('sensor.fake_office_temp') | float] }}"},
        "action": [{"service": "notify.x", "data": {"message": "{{ states.sensor.fake_office_temp.state }}"}}],
    },
    "automation.fake_unrelated": {      # only the _2 sensor: must not match
        "triggers": [{"trigger": "state", "entity_id": "sensor.fake_office_temp_2"}],
    },
    "automation.fake_by_device": {"triggers": [{"trigger": "device", "device_id": "dev1"}]},
    "script.fake_report": {"sequence": [{"action": "notify.x", "data": {"message": "{{ states('sensor.fake_office_temp') }}"}}]},
}


def test_scan_config_places_and_whole_ids_only():
    assert scan_config(CONFIGS["automation.fake_heat"], TARGET) == [
        "trigger (triggers/0)", "condition (conditions/0)"]
    assert scan_config(CONFIGS["automation.fake_coordinator"], TARGET) == [
        "template in variables (variables/rooms)", "template in action (action/0)"]
    assert scan_config(CONFIGS["automation.fake_unrelated"], TARGET) == []
    assert scan_config({"use_blueprint": {"input": {"sensor": TARGET}}}, TARGET) == [
        "blueprint input (use_blueprint/input)"]


def _lookup(**kw):
    look = Lookup(TARGET, STATES, configs=dict(CONFIGS), **kw)
    return look


def test_render_merges_scan_and_ha_search():
    look = _lookup(
        related={"automation": ["automation.fake_heat", "automation.fake_by_device"],
                 "scene": ["scene.fake_evening"], "device": ["dev1"], "area": ["office"],
                 "integration": ["fakeweather"]},
        unreadable=["automation.fake_broken"],
        devices=[{"id": "dev1", "name": "Office Sensor", "name_by_user": None, "area_id": "office"}],
        areas=[{"area_id": "office", "name": "Office"}],
    )
    text = render(look)
    assert text.startswith(f"# References to {TARGET}\nOffice temp, now 21.5\n")
    assert "## Automations (3)" in text
    assert "- automation.fake_coordinator (Coordinator): template in variables (variables/rooms)" in text
    assert "- automation.fake_heat (Office heat): trigger (triggers/0), condition (conditions/0)" in text
    assert "- automation.fake_by_device: listed by HA's related search" in text
    assert "fake_unrelated" not in text
    assert "## Scripts (1)\n- script.fake_report (Report): template in action (sequence/0)" in text
    assert "- Device: Office Sensor (area Office)" in text
    assert "- Scenes: scene.fake_evening" in text
    assert "could not read 1 configs: automation.fake_broken" in text
    assert "## Not searched" in text


def test_render_flags_missing_entity():
    look = Lookup("sensor.fake_gone", STATES,
                  configs={"automation.fake_heat": {"triggers": [{"entity_id": "sensor.fake_gone"}]}})
    text = render(look)
    assert "Not a current entity" in text
    assert "- automation.fake_heat (Office heat): trigger (triggers/0)" in text
    assert "## Scripts (0)\n- none" in text


class FakeClient:
    def __init__(self, settings):
        pass

    def states(self):
        return STATES

    def close(self):
        pass


class FakeWS:
    calls = []

    def __init__(self, settings):
        pass

    def call(self, msg_type, **fields):
        FakeWS.calls.append(msg_type)
        if msg_type == "search/related":
            return {"automation": ["automation.fake_heat"]}
        if msg_type in ("automation/config", "script/config"):
            if fields["entity_id"] == "automation.fake_broken":
                raise RuntimeError("automation/config failed: not_found")
            return {"config": CONFIGS.get(fields["entity_id"], {})}
        raise AssertionError(f"unexpected call {msg_type}")

    def close(self):
        pass


def test_fetch_reads_every_config_and_skips_failures(monkeypatch):
    monkeypatch.setattr(references, "HAClient", FakeClient)
    monkeypatch.setattr(references, "HAWebSocket", FakeWS)
    FakeWS.calls = []
    look = references.fetch(HASettings("http://ha.invalid", "t"), TARGET)
    assert look.unreadable == ["automation.fake_broken"]
    assert set(look.configs) == set(CONFIGS)
    assert "config/device_registry/list" not in FakeWS.calls  # no device or area in the result


def test_tool_validates_input():
    cfg = tools.ToolConfig(HASettings("http://ha.invalid", "t"))
    assert tools.find_references(cfg, "office temp").text.startswith("Error: Not entity IDs")
    assert tools.find_references(cfg, "sensor.a sensor.b").text == "Error: give one entity ID at a time."

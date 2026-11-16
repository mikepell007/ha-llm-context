import asyncio
import importlib.util
import shutil
from pathlib import Path

import pytest

from ha_llm_context import pipeline
from ha_llm_context.config import HASettings
from ha_llm_context.pipeline import format_matches, match_entities, parse_entity_ids

FIXTURE = Path(__file__).parent / "fixtures" / "run1"
TOOL = Path(__file__).parent.parent / "openwebui" / "ha_timeline_tool.py"

STATES = [
    {"entity_id": "climate.fake_office", "state": "heat", "attributes": {"friendly_name": "Office Thermostat"}},
    {"entity_id": "sensor.fake_office_temperature", "state": "21", "attributes": {"friendly_name": "Office temp"}},
    {"entity_id": "automation.fake_office_heat", "state": "on", "attributes": {"friendly_name": "Office heating"}},
    {"entity_id": "light.fake_porch", "state": "off", "attributes": {"friendly_name": "Porch"}},
]


def test_parse_entity_ids():
    assert parse_entity_ids("Light.Porch, automation.porch_light  light.porch") == [
        "light.porch", "automation.porch_light"]
    with pytest.raises(ValueError, match="Not entity IDs: porch, light"):
        parse_entity_ids("porch light")
    with pytest.raises(ValueError):
        parse_entity_ids(" , ")


def test_match_entities_by_name_or_id_and_domain():
    hits, total = match_entities(STATES, "office")
    assert total == 3
    hits, _ = match_entities(STATES, "office thermostat")
    assert [h["entity_id"] for h in hits] == ["climate.fake_office"]
    hits, _ = match_entities(STATES, "office", domain="automation")
    assert [h["entity_id"] for h in hits] == ["automation.fake_office_heat"]
    hits, total = match_entities(STATES, "office", limit=1)
    assert "and 2 more" in format_matches(hits, total)


def _fake_collect(settings, entities, start, end, out_dir):
    shutil.copytree(FIXTURE, out_dir)
    return out_dir


def test_pull_timeline_keeps_raw_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "collect", _fake_collect)
    settings = HASettings("http://ha.invalid", "t")
    result = pipeline.pull_timeline(settings, ["light.hallway"], "2h", out_root=tmp_path)
    assert result.run_dir.parent == tmp_path
    assert (result.run_dir / "timeline.txt").read_text() == result.timeline
    assert "# Home Assistant timeline" in result.timeline


def test_pull_timeline_without_out_root_leaves_nothing(monkeypatch):
    seen = {}

    def collect(settings, entities, start, end, out_dir):
        seen["dir"] = out_dir
        return _fake_collect(settings, entities, start, end, out_dir)

    monkeypatch.setattr(pipeline, "collect", collect)
    result = pipeline.pull_timeline(HASettings("http://ha.invalid", "t"), ["light.hallway"])
    assert result.run_dir is None
    assert not seen["dir"].exists()


def test_pull_timeline_rejects_backwards_window():
    with pytest.raises(ValueError, match="not before"):
        pipeline.pull_timeline(HASettings("http://ha.invalid", "t"), ["light.x"],
                               since="2026-03-07T20:00:00+00:00", until="2026-03-07T18:00:00+00:00")


# ---- Open WebUI tool (loaded the way Open WebUI does: as a standalone file)

@pytest.fixture
def tool():
    pytest.importorskip("pydantic")
    spec = importlib.util.spec_from_file_location("ha_timeline_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Tools()


def test_tool_without_valves_explains_setup(tool):
    out = asyncio.run(tool.get_home_assistant_timeline("light.fake"))
    assert out.startswith("Error:") and "ha_url and ha_token" in out


def test_tool_rejects_names_instead_of_ids(tool):
    tool.valves.ha_url, tool.valves.ha_token = "http://ha.invalid", "t"
    out = asyncio.run(tool.get_home_assistant_timeline("the porch light"))
    assert "Not entity IDs" in out


def test_tool_returns_timeline_and_reports_status(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "collect", _fake_collect)
    tool.valves.ha_url, tool.valves.ha_token = "http://ha.invalid", "t"
    tool.valves.out_dir, tool.valves.timezone = str(tmp_path), "UTC"
    events = []

    async def emit(event):
        events.append(event)

    out = asyncio.run(tool.get_home_assistant_timeline("light.hallway", "2h", "", emit))
    assert out.startswith("# Home Assistant timeline")
    assert events[-1]["data"]["done"] is True
    assert list(tmp_path.iterdir())  # raw pull kept


def test_tool_find_entities(tool, monkeypatch):
    monkeypatch.setattr(pipeline, "find_entities",
                        lambda settings, q, d: format_matches(*match_entities(STATES, q, d)))
    tool.valves.ha_url, tool.valves.ha_token = "http://ha.invalid", "t"
    out = asyncio.run(tool.find_home_assistant_entities("porch"))
    assert out == "light.fake_porch: Porch (now off)"


def test_tool_reports_ha_errors_instead_of_raising(tool, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("WebSocket authentication failed; check HA_TOKEN")

    monkeypatch.setattr(pipeline, "collect", boom)
    tool.valves.ha_url, tool.valves.ha_token = "http://ha.invalid", "t"
    out = asyncio.run(tool.get_home_assistant_timeline("light.x"))
    assert out == "Error pulling from Home Assistant: WebSocket authentication failed; check HA_TOKEN"

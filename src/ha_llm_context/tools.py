"""The two model-facing tools, shared by every front end (MCP server, Open WebUI).

Names, descriptions, parameter text and defaults live here so the front ends
can't drift apart. The Open WebUI tool has to repeat the text in its own
docstrings (Open WebUI reads them from the pasted file);
tests/test_tool_parity.py fails if the copies differ.

Both functions return text for the model and never raise: a model can act on
"Error: ..." (fix an entity ID, ask the user to set a token), but an exception
just ends the chat turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import pipeline, references
from .ask import estimate_tokens
from .condense import Options
from .config import HASettings

READ_ONLY = (
    "Read-only: it cannot create, edit, enable, or trigger Home Assistant "
    "automations, and it cannot control devices."
)

FIND_NAME = "find_home_assistant_entities"
FIND_DESCRIPTION = (
    "Search Home Assistant entities by words in their entity ID or friendly name. "
    "Use this first when the user describes a device or automation in words "
    "and you need its exact entity ID. Read-only: it cannot create or change "
    "anything in Home Assistant."
)
FIND_PARAMS = {
    "query": 'Words that must all appear, e.g. "office climate" or "porch motion".',
    "domain": 'Optional domain to limit the search, e.g. "automation", "climate", "light".',
}
FIND_RETURNS = "One line per match: entity ID, friendly name, and current state."

REFS_NAME = "find_home_assistant_references"
REFS_DESCRIPTION = (
    "Find everything in Home Assistant that refers to one entity: automations "
    "and scripts (including references inside templates and variables), "
    "scenes, groups, and its device, area and integration. Use it when the user "
    "asks where an entity is used, what would be affected by changing or "
    "removing it, or which automations might have changed it. Read-only: it "
    "cannot create or change anything in Home Assistant."
)
REFS_PARAMS = {
    "entity_id": 'One exact entity ID, e.g. "sensor.office_temperature". Use '
                 "find_home_assistant_entities first if you only have a name.",
}
REFS_RETURNS = ("Each automation and script that refers to the entity and where "
                "(trigger, condition, action, template), then other related items, "
                "then what was and wasn't searched.")

TIMELINE_NAME = "get_home_assistant_timeline"
TIMELINE_DESCRIPTION = (
    "Get a condensed, time-ordered timeline for Home Assistant entities: "
    "state and attribute changes, automation runs with each step's result, "
    "logbook entries, and warnings and errors from the log. Use it to explain "
    "why an automation or device did or didn't do something. "
    + READ_ONLY
    + " If the user asks for a new or changed Home Assistant automation, write "
    "the YAML in your reply for them to paste into Home Assistant. Do not use "
    'other tools (such as scheduled tasks or "automations" in this chat app) '
    "for that: they are not Home Assistant."
)
TIMELINE_PARAMS = {
    "entity_ids": (
        'Comma-separated entity IDs, e.g. "automation.porch_light". Giving just the '
        "automation is usually enough: its runs, variables, and the entities its "
        "config refers to are included."
    ),
    "since": 'Start of the window: "30m", "2h", "1d", or an ISO time. Defaults to "2h".',
    "until": "End of the window in the same formats. Empty means now.",
}
TIMELINE_RETURNS = "The timeline text, ending with a list of what was left out."

DEFAULT_SINCE = "2h"

# Shared defaults for the front ends' settings.
DEFAULT_MAX_RELATED = 30
DEFAULT_WARN_TOKENS = 8000


@dataclass
class ToolConfig:
    """Everything the tools need besides the model's arguments."""

    settings: HASettings
    options: Options = field(default_factory=Options)
    out_root: Path | None = None  # keep raw pulls here; None keeps nothing
    related: bool = True
    max_related: int = DEFAULT_MAX_RELATED
    warn_tokens: int = DEFAULT_WARN_TOKENS


@dataclass
class ToolOutput:
    text: str
    ok: bool
    tokens: int = 0


def find(cfg: ToolConfig, query: str, domain: str = "") -> ToolOutput:
    try:
        text = pipeline.find_entities(cfg.settings, query, domain)
    except Exception as err:  # reported to the model, see module docstring
        return ToolOutput(f"Error searching entities: {err}", False)
    return ToolOutput(text, True)


def find_references(cfg: ToolConfig, entity_id: str) -> ToolOutput:
    try:
        ids = pipeline.parse_entity_ids(entity_id)
    except ValueError as err:
        return ToolOutput(f"Error: {err}", False)
    if len(ids) != 1:
        return ToolOutput("Error: give one entity ID at a time.", False)
    try:
        text = references.find_references(cfg.settings, ids[0])
    except Exception as err:
        return ToolOutput(f"Error searching references: {err}", False)
    return ToolOutput(text, True)


def timeline(cfg: ToolConfig, entity_ids: str, since: str = DEFAULT_SINCE,
             until: str = "") -> ToolOutput:
    try:
        entities = pipeline.parse_entity_ids(entity_ids)
    except ValueError as err:
        return ToolOutput(f"Error: {err}", False)
    try:
        result = pipeline.pull_timeline(cfg.settings, entities, since or DEFAULT_SINCE,
                                        until, cfg.out_root, cfg.options,
                                        cfg.related, cfg.max_related)
    except Exception as err:
        return ToolOutput(f"Error pulling from Home Assistant: {err}", False)

    tokens = estimate_tokens(result.timeline)
    text = result.timeline
    if tokens > cfg.warn_tokens:
        text = (f"Note: this timeline is about {tokens} tokens, which may exceed your "
                "context window. A shorter window or fewer entities would help.\n\n" + text)
    return ToolOutput(text, True, tokens)


def count_entities(entity_ids: str) -> int:
    """For progress messages; 0 when the text doesn't parse."""
    try:
        return len(pipeline.parse_entity_ids(entity_ids))
    except ValueError:
        return 0

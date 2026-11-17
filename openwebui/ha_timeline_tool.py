"""
title: Home Assistant Timeline
description: Read-only troubleshooting for Home Assistant. Finds entities and returns a condensed timeline of state changes, automation runs, logbook entries, and errors for the model to reason over.
version: 0.2.0
license: MIT
requirements: ha-llm-context @ https://github.com/mikepell007/ha-llm-context/archive/refs/heads/main.zip
"""

# Paste this file into Open WebUI under Workspace > Tools, then set the
# Valves (gear icon): at least ha_url and ha_token. The token is stored in
# Open WebUI's database and is never part of this file.
#
# This tool only reads from Home Assistant: it never calls services or
# changes state.

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

Emitter = Callable[[dict[str, Any]], Awaitable[None]] | None


async def _status(emit: Emitter, description: str, done: bool = False) -> None:
    if emit:
        await emit({"type": "status", "data": {"description": description, "done": done}})


class Tools:
    class Valves(BaseModel):
        ha_url: str = Field("", description="Home Assistant URL, e.g. http://homeassistant.local:8123")
        ha_token: str = Field("", description="Long-lived access token (an admin user's, so the error log is readable)")
        timezone: str = Field("", description="Display time zone, e.g. America/New_York. Empty uses the server's, which in Docker is usually UTC.")
        keep_raw: bool = Field(True, description="Keep each raw pull and its timeline on disk")
        out_dir: str = Field("/app/backend/data/ha-llm-context", description="Where raw pulls are kept when keep_raw is on")
        include_related: bool = Field(True, description="Also pull entities that a requested automation's config refers to")
        max_related: int = Field(30, description="Most related entities to add per pull")
        max_changes_per_entity: int = Field(40, description="Busy entities beyond this keep only their first and last changes")
        max_errors: int = Field(30, description="Unique error-log messages to include")
        include_config: bool = Field(True, description="Include automation configs under 2000 characters")
        extra_attributes: str = Field("", description="Comma-separated attributes to keep beyond the per-domain defaults")
        warn_tokens: int = Field(8000, description="Warn the model when a timeline is estimated above this many tokens")

    def __init__(self):
        self.valves = self.Valves()

    def _settings(self):
        from ha_llm_context.config import HASettings

        if not self.valves.ha_url or not self.valves.ha_token:
            raise ValueError("The tool's ha_url and ha_token valves aren't set. "
                             "An admin sets them under Workspace > Tools > Home Assistant Timeline.")
        return HASettings(self.valves.ha_url.rstrip("/"), self.valves.ha_token)

    def _options(self):
        from ha_llm_context.condense import Options

        v = self.valves
        return Options(
            tz=ZoneInfo(v.timezone) if v.timezone else None,
            extra_attrs=tuple(a.strip() for a in v.extra_attributes.split(",") if a.strip()),
            max_changes_per_entity=v.max_changes_per_entity,
            max_errors=v.max_errors,
            include_config=v.include_config,
        )

    async def find_home_assistant_entities(
        self, query: str, domain: str = "", __event_emitter__: Emitter = None
    ) -> str:
        """
        Search Home Assistant entities by words in their entity ID or friendly name.
        Use this first when the user describes a device or automation in words
        and you need its exact entity ID.
        :param query: Words that must all appear, e.g. "office climate" or "porch motion".
        :param domain: Optional domain to limit the search, e.g. "automation", "climate", "light".
        :return: One line per match: entity ID, friendly name, and current state.
        """
        from ha_llm_context.pipeline import find_entities

        await _status(__event_emitter__, f"Searching Home Assistant for '{query}'")
        try:
            result = await asyncio.to_thread(find_entities, self._settings(), query, domain)
        except Exception as err:  # report to the model instead of failing the chat
            await _status(__event_emitter__, "Entity search failed", done=True)
            return f"Error searching entities: {err}"
        await _status(__event_emitter__, "Entity search done", done=True)
        return result

    async def get_home_assistant_timeline(
        self, entity_ids: str, since: str = "2h", until: str = "",
        __event_emitter__: Emitter = None,
    ) -> str:
        """
        Get a condensed, time-ordered timeline for Home Assistant entities:
        state and attribute changes, automation runs with each step's result,
        logbook entries, and warnings and errors from the log. Use it to explain
        why an automation or device did or didn't do something.
        :param entity_ids: Comma-separated entity IDs, e.g. "automation.porch_light". Giving just the automation is usually enough: its runs, variables, and the entities its config refers to are included.
        :param since: Start of the window: "30m", "2h", "1d", or an ISO time. Defaults to "2h".
        :param until: End of the window in the same formats. Empty means now.
        :return: The timeline text, ending with a list of what was left out.
        """
        from ha_llm_context.ask import estimate_tokens
        from ha_llm_context.pipeline import parse_entity_ids, pull_timeline

        try:
            entities = parse_entity_ids(entity_ids)
            settings, options = self._settings(), self._options()
        except Exception as err:
            return f"Error: {err}"

        await _status(__event_emitter__, f"Pulling {len(entities)} entities from Home Assistant ({since})")
        out_root = Path(self.valves.out_dir) if self.valves.keep_raw else None
        try:
            result = await asyncio.to_thread(
                pull_timeline, settings, entities, since, until, out_root, options,
                self.valves.include_related, self.valves.max_related)
        except Exception as err:
            await _status(__event_emitter__, "Home Assistant pull failed", done=True)
            return f"Error pulling from Home Assistant: {err}"

        tokens = estimate_tokens(result.timeline)
        await _status(__event_emitter__, f"Timeline ready (~{tokens} tokens)", done=True)
        if tokens > self.valves.warn_tokens:
            return (f"Note: this timeline is about {tokens} tokens, which may exceed your "
                    "context window. A shorter window or fewer entities would help.\n\n"
                    + result.timeline)
        return result.timeline

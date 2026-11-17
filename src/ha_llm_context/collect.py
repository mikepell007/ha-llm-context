"""Stage 1: pull raw data from Home Assistant into a run directory.

Read-only: this module only issues GET requests and WebSocket queries
(auth, trace/list, trace/get, automation/config, system_log/list). It never calls services or
writes state.

Run directory layout:
    meta.json       window, entities, collection time, notes on anything missing
    history.json    /api/history/period response (with attributes)
    logbook.json    /api/logbook response for the window
    error_log.txt   /api/error_log text, when HA writes a log file; otherwise
    system_log.json system_log/list result (grouped warnings and errors)
    traces.json     {automation entity_id: {"item_id": ..., "runs": [trace/get result, ...]}}
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import connect

from .config import HASettings


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class HAClient:
    def __init__(self, settings: HASettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.url,
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=60,
            transport=transport,
        )

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r

    def history(self, start: datetime, end: datetime, entities: list[str]) -> Any:
        # No minimal_response, so attributes come back on every row.
        # significant_changes_only=0 keeps attribute-only updates.
        return self._get(
            f"/api/history/period/{_iso(start)}",
            {
                "end_time": _iso(end),
                "filter_entity_id": ",".join(entities),
                "significant_changes_only": "0",
            },
        ).json()

    def logbook(self, start: datetime, end: datetime) -> Any:
        # The logbook API filters on one entity at most, so pull the window
        # and let the condense stage filter.
        return self._get(f"/api/logbook/{_iso(start)}", {"end_time": _iso(end)}).json()

    def error_log(self) -> str:
        return self._get("/api/error_log").text

    def state(self, entity_id: str) -> dict[str, Any]:
        return self._get(f"/api/states/{entity_id}").json()

    def states(self) -> list[dict[str, Any]]:
        return self._get("/api/states").json()

    def close(self) -> None:
        self.http.close()


class HAWebSocket:
    """Minimal read-only WebSocket client for trace queries."""

    def __init__(self, settings: HASettings):
        url = settings.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        self.ws = connect(f"{url}/api/websocket", max_size=64 * 1024 * 1024)
        self._id = 0
        hello = json.loads(self.ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected WebSocket greeting: {hello.get('type')}")
        self.ws.send(json.dumps({"type": "auth", "access_token": settings.token}))
        reply = json.loads(self.ws.recv())
        if reply.get("type") != "auth_ok":
            raise RuntimeError("WebSocket authentication failed; check HA_TOKEN")

    def call(self, msg_type: str, **fields: Any) -> Any:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "type": msg_type, **fields}))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get("id") == self._id:
                break
        if not reply.get("success"):
            raise RuntimeError(f"{msg_type} failed: {reply.get('error')}")
        return reply["result"]

    def close(self) -> None:
        self.ws.close()


def runs_in_window(summaries: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Trace summaries whose run started inside [start, end], oldest first."""
    kept = [s for s in summaries if start <= _parse_ts(s["timestamp"]["start"]) <= end]
    return sorted(kept, key=lambda s: s["timestamp"]["start"])


def collect_traces(
    client: HAClient, ws: HAWebSocket, automations: list[str], start: datetime, end: datetime
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entity_id in automations:
        # Traces are keyed by the automation's config id, not its entity_id.
        item_id = client.state(entity_id).get("attributes", {}).get("id")
        if not item_id:
            out[entity_id] = {"item_id": None, "runs": [], "note": "no config id; traces unavailable"}
            continue
        summaries = ws.call("trace/list", domain="automation", item_id=item_id)
        runs = [
            ws.call("trace/get", domain="automation", item_id=item_id, run_id=s["run_id"])
            for s in runs_in_window(summaries, start, end)
        ]
        out[entity_id] = {"item_id": item_id, "runs": runs, "stored": len(summaries)}
        try:
            # The automation as written (for blueprints: the blueprint inputs).
            # Used to find related entities even when nothing ran in the window.
            out[entity_id]["config"] = ws.call("automation/config", entity_id=entity_id).get("config")
        except RuntimeError:
            pass
    return out


_ENTITY_REF = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")


def find_entity_refs(obj: Any, known: set[str]) -> list[str]:
    """Entity IDs mentioned anywhere in a config (keys, values, templates),
    in order of first appearance, keeping only ones that exist in HA."""
    found: dict[str, None] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(k)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            for m in _ENTITY_REF.findall(o):
                if m in known:
                    found.setdefault(m)

    walk(obj)
    return list(found)


def related_entities(traces: dict[str, Any], known: set[str], exclude: set[str],
                     limit: int) -> tuple[dict[str, list[str]], int]:
    """Entities each automation's config refers to. Returns ({automation: [ids]}, dropped)."""
    out: dict[str, list[str]] = {}
    taken = set(exclude)
    dropped = 0
    for automation, info in traces.items():
        sources = [info.get("config")]
        for run in info.get("runs", []):
            sources += [run.get("config"), run.get("blueprint_inputs")]
        mine = []
        for ref in find_entity_refs([s for s in sources if s], known):
            if ref in taken or ref == automation:
                continue
            if len(taken) - len(exclude) >= limit:
                dropped += 1
                continue
            taken.add(ref)
            mine.append(ref)
        out[automation] = mine
    return out, dropped


def collect_errors(client: HAClient, ws: HAWebSocket, out_dir: Path) -> list[str]:
    """Save the error log. Returns notes about what couldn't be fetched.

    /api/error_log only works when HA writes home-assistant.log; HA OS and
    Supervised installs log to the journal instead and return 404. Fall back
    to system_log/list, the WebSocket source behind Settings > System > Logs.
    It only holds warnings and errors, already grouped with counts.
    """
    notes = []
    try:
        (out_dir / "error_log.txt").write_text(client.error_log())
        return notes
    except httpx.HTTPStatusError as err:
        notes.append(f"/api/error_log returned {err.response.status_code}; used system_log/list")
    try:
        (out_dir / "system_log.json").write_text(json.dumps(ws.call("system_log/list")))
    except RuntimeError as err:
        notes.append(f"system_log/list failed ({err}); no error log in this run")
    return notes


def collect(
    settings: HASettings,
    entities: list[str],
    start: datetime,
    end: datetime,
    out_dir: Path,
    related: bool = True,
    max_related: int = 30,
) -> Path:
    """Pull everything for the window into out_dir and return it.

    With related=True, entities that the requested automations' configs refer
    to are added (up to max_related), so the user needn't list them.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    automations = [e for e in entities if e.startswith("automation.")]
    notes: list[str] = []
    related_map: dict[str, list[str]] = {}
    all_entities = list(entities)

    client = HAClient(settings)
    ws = HAWebSocket(settings)
    try:
        traces = collect_traces(client, ws, automations, start, end) if automations else {}
        (out_dir / "traces.json").write_text(json.dumps(traces))
        if related and traces:
            known = {s["entity_id"] for s in client.states()}
            related_map, dropped = related_entities(traces, known, set(entities), max_related)
            for ids in related_map.values():
                all_entities += ids
            if dropped:
                notes.append(f"{dropped} more related entities left out (limit {max_related})")
        (out_dir / "history.json").write_text(json.dumps(client.history(start, end, all_entities)))
        (out_dir / "logbook.json").write_text(json.dumps(client.logbook(start, end)))
        notes += collect_errors(client, ws, out_dir)
    finally:
        ws.close()
        client.close()

    meta = {
        "start": _iso(start),
        "end": _iso(end),
        "entities": all_entities,
        "requested": entities,
        "related": related_map,
        "collected_at": _iso(datetime.now().astimezone()),
        "notes": notes,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return out_dir

"""Entry points shared by the CLI and the Open WebUI tool.

These wrap the stages so a caller doesn't have to know the run-directory
layout: find entities by name, and pull a window straight to a timeline.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .collect import HAClient, collect
from .condense import Options, condense
from .config import HASettings, parse_when

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def parse_entity_ids(text: str) -> list[str]:
    """Split a comma- or space-separated list and check each looks like an entity ID."""
    ids = [e.strip().lower() for e in re.split(r"[,\s]+", text) if e.strip()]
    bad = [e for e in ids if not ENTITY_ID.match(e)]
    if bad:
        raise ValueError(f"Not entity IDs: {', '.join(bad)}. Expected e.g. light.kitchen")
    if not ids:
        raise ValueError("No entity IDs given")
    return list(dict.fromkeys(ids))


def match_entities(states: list[dict[str, Any]], query: str, domain: str = "",
                   limit: int = 25) -> tuple[list[dict[str, Any]], int]:
    """States whose entity_id or friendly name contains every word of the query.

    Returns (up to `limit` matches, total match count).
    """
    words = [w for w in re.split(r"[\s._]+", query.lower()) if w]
    domain = domain.strip().lower().rstrip(".")
    hits = []
    for s in states:
        eid = s.get("entity_id", "")
        if domain and not eid.startswith(f"{domain}."):
            continue
        name = str((s.get("attributes") or {}).get("friendly_name", ""))
        hay = f"{eid} {name}".lower().replace("_", " ")
        if all(w in hay for w in words):
            hits.append(s)
    hits.sort(key=lambda s: s["entity_id"])
    return hits[:limit], len(hits)


def format_matches(hits: list[dict[str, Any]], total: int) -> str:
    if not hits:
        return "No matching entities."
    lines = [f"{s['entity_id']}: {(s.get('attributes') or {}).get('friendly_name', '')} "
             f"(now {s.get('state')})" for s in hits]
    if total > len(hits):
        lines.append(f"... and {total - len(hits)} more; narrow the search.")
    return "\n".join(lines)


def find_entities(settings: HASettings, query: str, domain: str = "", limit: int = 25) -> str:
    client = HAClient(settings)
    try:
        return format_matches(*match_entities(client.states(), query, domain, limit))
    finally:
        client.close()


@dataclass
class TimelineResult:
    timeline: str
    run_dir: Path | None  # None when raw data wasn't kept


def pull_timeline(settings: HASettings, entities: list[str], since: str = "2h",
                  until: str = "", out_root: Path | None = None,
                  options: Options | None = None) -> TimelineResult:
    """Collect a window from HA and condense it.

    With out_root, the raw pull and timeline.txt are kept in a new run
    directory under it (so it can be condensed again later); otherwise they
    go to a temporary directory that is removed afterwards.
    """
    end = parse_when(until) if until else datetime.now().astimezone()
    start = parse_when(since, now=end)
    if start >= end:
        raise ValueError(f"Window start {start} is not before its end {end}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if out_root is None:
        with tempfile.TemporaryDirectory(prefix="ha-llm-context-") as tmp:
            run_dir = collect(settings, entities, start, end, Path(tmp) / stamp)
            return TimelineResult(condense(run_dir, options), None)

    run_dir = collect(settings, entities, start, end, out_root / stamp)
    timeline = condense(run_dir, options)
    (run_dir / "timeline.txt").write_text(timeline)
    return TimelineResult(timeline, run_dir)

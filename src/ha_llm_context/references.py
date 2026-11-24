"""Find everything in Home Assistant that refers to one entity.

Two sources, because neither is complete alone:

- HA's own reverse lookup, the WebSocket `search/related` command behind the
  "Related" tab in an entity's settings. It knows scenes, groups, devices,
  areas and blueprints, but it doesn't look inside templates.
- A text scan of every automation and script config, which also finds the
  entity in templates such as {{ states('sensor.x') }} and in variables.

Read-only: `search/related`, `automation/config`, `script/config`, and the
device and area registry lists, plus GET /api/states.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .collect import HAClient, HAWebSocket
from .config import HASettings

# Config top-level keys -> what the model should call that part.
_SECTION = {
    "trigger": "trigger", "triggers": "trigger",
    "condition": "condition", "conditions": "condition",
    "action": "action", "actions": "action", "sequence": "action",
    "variables": "variables", "trigger_variables": "variables",
    "use_blueprint": "blueprint input",
    "fields": "script fields",
}
_TEMPLATE = re.compile(r"\{\{|\{%")
MAX_PLACES = 4      # locations listed per automation or script
MAX_RELATED = 10    # names listed per kind from HA's search


def _pattern(entity_id: str) -> re.Pattern[str]:
    # Whole IDs only: sensor.temp must not match sensor.temp_2. A dot before
    # is allowed, for templates such as states.sensor.temp.state.
    return re.compile(rf"(?<![\w]){re.escape(entity_id)}(?![\w])")


def scan_config(config: Any, entity_id: str) -> list[str]:
    """Where in a config the entity appears, e.g. "trigger (triggers/0)" or
    "template in variables (variables/rooms)". In order of first appearance."""
    pat = _pattern(entity_id)
    places: dict[str, None] = {}

    def walk(obj: Any, path: list[str]) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and pat.search(k):
                    places.setdefault(_place(path + [k], template=False))
                walk(v, path + [str(k)])
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, path + [str(i)])
        elif isinstance(obj, str) and pat.search(obj):
            places.setdefault(_place(path, template=bool(_TEMPLATE.search(obj))))

    walk(config, [])
    return list(places)


def _place(path: list[str], template: bool) -> str:
    top = path[0] if path else ""
    section = _SECTION.get(top, top or "config")
    where = "/".join(path[:2]) if len(path) > 1 else top
    label = f"template in {section}" if template else section
    return f"{label} ({where})" if where else label


@dataclass
class Lookup:
    """Everything fetched from HA for one search; render() needs nothing else."""

    entity_id: str
    states: list[dict[str, Any]]
    related: dict[str, list[str]] = field(default_factory=dict)  # search/related result
    configs: dict[str, Any] = field(default_factory=dict)        # automation/script entity_id -> config
    unreadable: list[str] = field(default_factory=list)          # configs that couldn't be fetched
    devices: list[dict[str, Any]] = field(default_factory=list)
    areas: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def fetch(settings: HASettings, entity_id: str) -> Lookup:
    client = HAClient(settings)
    try:
        states = client.states()
    finally:
        client.close()
    look = Lookup(entity_id, states)
    ws = HAWebSocket(settings)
    try:
        look.related = _try(look, "HA's related search", ws.call, {},
                            "search/related", item_type="entity", item_id=entity_id)
        for s in states:
            eid = s["entity_id"]
            domain = eid.split(".", 1)[0]
            if domain not in ("automation", "script") or eid == entity_id:
                continue
            try:
                look.configs[eid] = ws.call(f"{domain}/config", entity_id=eid).get("config")
            except RuntimeError:
                look.unreadable.append(eid)
        if any(look.related.get(k) for k in ("device", "area")):
            look.devices = _try(look, "device names", ws.call, [], "config/device_registry/list")
            look.areas = _try(look, "area names", ws.call, [], "config/area_registry/list")
    finally:
        ws.close()
    return look


def _try(look: Lookup, what: str, fn: Callable[..., Any], default: Any, *args: Any,
         **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except RuntimeError as err:
        look.notes.append(f"{what} unavailable: {err}")
        return default


def render(look: Lookup) -> str:
    target = look.entity_id
    by_id = {s["entity_id"]: s for s in look.states}

    def name(eid: str) -> str:
        n = (by_id.get(eid, {}).get("attributes") or {}).get("friendly_name")
        return f"{eid} ({n})" if n else eid

    out = [f"# References to {target}"]
    me = by_id.get(target)
    if me:
        fn = (me.get("attributes") or {}).get("friendly_name")
        out.append(f"{fn + ', ' if fn else ''}now {me.get('state')}")
    else:
        out.append("Not a current entity. Anything below refers to an ID that no longer "
                   "exists (or never did), which is often the bug.")

    related = look.related
    for domain, title in (("automation", "Automations"), ("script", "Scripts")):
        scanned = {eid: places for eid, cfg in look.configs.items()
                   if eid.startswith(f"{domain}.") and (places := scan_config(cfg, target))}
        from_search = set(related.get(domain, [])) - {target}
        found = sorted(set(scanned) | from_search)
        out += ["", f"## {title} ({len(found)})"]
        if not found:
            out.append("- none")
        for eid in found:
            places = scanned.get(eid, [])
            if places:
                shown = ", ".join(places[:MAX_PLACES])
                if len(places) > MAX_PLACES:
                    shown += f", +{len(places) - MAX_PLACES} more"
            else:
                shown = ("listed by HA's related search, but the ID isn't written in its "
                         "config (it refers through a device, area or blueprint)")
            out.append(f"- {name(eid)}: {shown}")

    devices = {d["id"]: d for d in look.devices}
    areas = {a["area_id"]: a.get("name") or a["area_id"] for a in look.areas}

    def device_name(did: str) -> str:
        d = devices.get(did)
        if not d:
            return did
        label = d.get("name_by_user") or d.get("name") or did
        return f"{label} (area {areas[d['area_id']]})" if d.get("area_id") in areas else label

    other = [
        ("Scenes", related.get("scene", []), name),
        ("Groups", related.get("group", []), name),
        ("Device", related.get("device", []), device_name),
        ("Area", related.get("area", []), lambda a: areas.get(a, a)),
        ("Integration", related.get("integration", []), str),
        ("Blueprints", related.get("automation_blueprint", []) + related.get("script_blueprint", []), str),
        ("Related entities (same group or device)",
         [e for e in related.get("entity", []) if e != target], name),
    ]
    lines = []
    for title, items, fmt in other:
        if items:
            shown = [fmt(i) for i in sorted(items)[:MAX_RELATED]]
            more = f", +{len(items) - MAX_RELATED} more" if len(items) > MAX_RELATED else ""
            lines.append(f"- {title}: {', '.join(shown)}{more}")
    if lines:
        out += ["", "## Also related in Home Assistant", *lines]

    n_auto = sum(1 for e in look.configs if e.startswith("automation."))
    n_script = len(look.configs) - n_auto
    out += ["", "## Searched",
            f"- {n_auto} automation and {n_script} script configs, including templates",
            "- HA's related search (scenes, groups, devices, areas, blueprints)"]
    if look.unreadable:
        out.append(f"- could not read {len(look.unreadable)} configs: {', '.join(sorted(look.unreadable))}")
    out += [f"- {n}" for n in look.notes]
    out += ["", "## Not searched",
            "- dashboards, and YAML-only definitions such as template sensors in configuration.yaml",
            "- entity IDs assembled inside templates, e.g. 'sensor.' ~ room ~ '_temp'"]
    return "\n".join(out) + "\n"


def find_references(settings: HASettings, entity_id: str) -> str:
    return render(fetch(settings, entity_id))

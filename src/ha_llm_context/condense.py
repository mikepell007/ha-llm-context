"""Stage 2: turn a raw run directory into a short timeline for a model.

Pure functions over the files written by collect.py; no network access.
Everything dropped is counted and reported at the end of the timeline, so
the model (and you) know what it isn't seeing.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

# Attributes worth showing, by domain. Anything else is dropped unless the
# caller asks for it with extra_attrs.
DEFAULT_ATTRS: dict[str, tuple[str, ...]] = {
    "climate": ("hvac_action", "current_temperature", "temperature",
                "target_temp_low", "target_temp_high", "preset_mode", "fan_mode"),
    "light": ("brightness", "color_temp_kelvin", "color_mode"),
    "cover": ("current_position",),
    "fan": ("percentage", "preset_mode"),
    "media_player": ("source", "volume_level"),
    "automation": ("current",),
    "script": ("current",),
    "water_heater": ("operation_mode", "temperature", "current_temperature"),
    "humidifier": ("humidity", "action"),
    "vacuum": ("status",),
    "lock": (),
}

LEVELS = ("WARNING", "ERROR", "CRITICAL")
_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
    r"\(([^)]*)\) \[([^\]]+)\] ?(.*)$"
)


@dataclass
class Options:
    tz: tzinfo | None = None            # display zone; None = system local
    extra_attrs: tuple[str, ...] = ()   # kept for every domain
    max_changes_per_entity: int = 40    # beyond this, keep head and tail
    max_errors: int = 30                # unique error-log messages
    include_config: bool = True         # automation config from the trace
    config_limit: int | None = 2000     # leave out configs longer than this; None = no limit
    merge_window: timedelta = timedelta(seconds=2)


@dataclass
class Event:
    when: datetime
    entity: str
    text: str
    detail: list[str] = field(default_factory=list)
    order: int = 0  # tie-break within the same timestamp


@dataclass
class Dropped:
    counts: Counter = field(default_factory=Counter)

    def add(self, reason: str, n: int = 1) -> None:
        if n:
            self.counts[reason] += n


# ---------------------------------------------------------------- helpers

def _ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).astimezone()
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.astimezone()


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _kept_attrs(entity_id: str, attrs: dict[str, Any], opts: Options) -> dict[str, Any]:
    names = DEFAULT_ATTRS.get(_domain(entity_id), ()) + opts.extra_attrs
    return {k: attrs[k] for k in names if k in attrs}


# ---------------------------------------------------------------- history

def missing_history(history: list[list[dict]], entities: set[str], start: datetime) -> list[Event]:
    present = {rows[0]["entity_id"] for rows in history if rows}
    return [Event(start, e, "no history recorded (excluded from the recorder, "
                  "or the entity ID is wrong); its state and attributes are unknown", order=-2)
            for e in sorted(entities - present)]


def history_events(history: list[list[dict]], start: datetime, opts: Options,
                   dropped: Dropped) -> list[Event]:
    events: list[Event] = []
    for rows in history:
        if not rows:
            continue
        entity = rows[0]["entity_id"]
        rows = sorted(rows, key=lambda r: r.get("last_updated") or r["last_changed"])
        unit = ""
        prev_state: str | None = None
        prev_attrs: dict[str, Any] = {}
        mine: list[Event] = []
        for row in rows:
            attrs = row.get("attributes") or {}
            unit = attrs.get("unit_of_measurement", unit) or ""
            state = row["state"]
            kept = _kept_attrs(entity, attrs, opts)
            if prev_state is not None and state == prev_state and kept == prev_attrs:
                dropped.add("history rows with no change to state or kept attributes")
                continue
            when = max(_ts(row.get("last_updated") or row["last_changed"]), start)
            shown = f"{state} {unit}".rstrip() if unit else state
            if prev_state is None:
                text = f"at start: {shown}"
                changed = {k: v for k, v in kept.items() if v is not None}
            elif state != prev_state:
                text = f"{prev_state} → {shown}"
                changed = {k: v for k, v in kept.items() if prev_attrs.get(k) != v}
            else:
                text = "attributes:"
                changed = {k: v for k, v in kept.items() if prev_attrs.get(k) != v}
            if changed:
                text += " (" + ", ".join(f"{k}={_fmt_value(v)}" for k, v in changed.items()) + ")"
            mine.append(Event(when, entity, text))
            prev_state, prev_attrs = state, kept

        cap = opts.max_changes_per_entity
        if len(mine) > cap:
            head, tail = mine[: cap // 2], mine[-(cap - cap // 2):]
            gap = len(mine) - cap
            dropped.add(f"changes of {entity} (busiest stretch; kept first and last {cap // 2})", gap)
            marker = Event(tail[0].when, entity, f"… {gap} more changes omitted …")
            mine = head + [marker] + tail
        events.extend(mine)
    return events


# ---------------------------------------------------------------- logbook

def _cause(entry: dict[str, Any]) -> str:
    etype = entry.get("context_event_type")
    if etype == "automation_triggered" and entry.get("context_name"):
        return f"by automation {entry['context_name']}"
    if etype == "call_service" and entry.get("context_domain"):
        return f"by service {entry['context_domain']}.{entry.get('context_service', '?')}"
    if entry.get("context_entity_id"):
        return f"by {entry.get('context_entity_id_name') or entry['context_entity_id']}"
    if entry.get("context_user_id"):
        return "by a user"
    return ""


def logbook_events(logbook: list[dict], entities: set[str], start: datetime, end: datetime,
                   state_events: list[Event], run_events: list[Event], opts: Options,
                   dropped: Dropped) -> list[Event]:
    by_entity: dict[str, list[Event]] = {}
    for ev in state_events:
        by_entity.setdefault(ev.entity, []).append(ev)
    runs: dict[str, list[datetime]] = {}
    for ev in run_events:
        runs.setdefault(ev.entity, []).append(ev.when)

    out: list[Event] = []
    for entry in logbook:
        entity = entry.get("entity_id") or ""
        is_core = entry.get("domain") == "homeassistant"
        if entity not in entities and not is_core:
            dropped.add("logbook entries for other entities")
            continue
        when = _ts(entry["when"])
        if not (start <= when <= end):
            dropped.add("logbook entries outside the window")
            continue
        cause = _cause(entry)
        # A logbook state entry repeats a history row; fold its cause into that row.
        if "state" in entry and entity in by_entity:
            match = next((ev for ev in by_entity[entity]
                          if abs(ev.when - when) <= opts.merge_window), None)
            if match:
                if cause and cause not in match.text:
                    match.text += f" [{cause}]"
                dropped.add("logbook entries merged into history lines")
                continue
        # "triggered by ..." repeats a RUN line from the traces.
        if any(abs(w - when) <= opts.merge_window for w in runs.get(entity, [])):
            dropped.add("logbook automation entries covered by a trace")
            continue
        text = entry.get("message") or entry.get("state") or ""
        name = entry.get("context_entity_id_name") or entry.get("context_name") or "\0"
        if cause and cause not in text and name not in text:
            text += f" [{cause}]"
        out.append(Event(when, entity or entry.get("name", "homeassistant"), f"logbook: {text}"))
    return out


# ---------------------------------------------------------------- traces

def _config_node(config: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The config entry for a top-level 'action/N' or 'condition/N' trace path."""
    kind, _, idx = path.partition("/")
    keys = {"action": ("actions", "action"), "condition": ("conditions", "condition")}.get(kind)
    if not keys or not idx.isdigit():
        return None
    seq = config.get(keys[0]) or config.get(keys[1]) or []
    if isinstance(seq, dict):
        seq = [seq]
    i = int(idx)
    return seq[i] if i < len(seq) and isinstance(seq[i], dict) else None


def _entities(node: dict[str, Any]) -> str:
    ent = node.get("entity_id") or (node.get("target") or {}).get("entity_id")
    return ",".join(ent) if isinstance(ent, list) else str(ent or "")


def describe_step(node: dict[str, Any] | None) -> str:
    """A short label for a config step, so the model knows what action/N was."""
    if not node:
        return ""
    if node.get("alias"):
        return _short(str(node["alias"]), 100)
    if "if" in node:
        cond = node["if"]
        return _short("if " + (cond if isinstance(cond, str) else _fmt_value(cond)), 100)
    for key in ("action", "service"):
        if isinstance(node.get(key), str):
            return " ".join(x for x in (node[key], _entities(node)) if x)
    if "condition" in node:
        cond = node["condition"]
        if cond == "template":
            return _short(f"condition {node.get('value_template', '')}", 100)
        bits = [f"condition {cond}", _entities(node)]
        for k in ("state", "above", "below", "attribute"):
            if k in node:
                bits.append(f"{k}={_fmt_value(node[k])}")
        return _short(" ".join(b for b in bits if b), 100)
    if isinstance(node.get("repeat"), dict):
        kind = next((k for k in ("for_each", "while", "until", "count") if k in node["repeat"]), "")
        return f"repeat {kind}".strip()
    for key in ("choose", "variables", "delay", "wait_template", "wait_for_trigger",
                "parallel", "sequence", "stop", "event", "scene"):
        if key in node:
            val = node[key]
            return _short(f"{key} {val}" if isinstance(val, (str, int, float)) else key, 100)
    return ""


def _result_text(path: str, step: dict[str, Any]) -> str:
    """What a step did, without its path or label."""
    result = step.get("result") or {}
    if "error" in step:
        return f"ERROR {_short(str(step['error']))}"
    if isinstance(result.get("result"), bool):
        # Conditions often explain themselves, e.g. state=38, wanted_state_below=30.
        why = {k: v for k, v in result.items() if k not in ("result", "entities")}
        extra = " (" + ", ".join(f"{k}={_fmt_value(v)}" for k, v in why.items()) + ")" if why else ""
        return ("pass" if result["result"] else "FAIL") + extra
    params = result.get("params")
    if isinstance(params, dict) and "domain" in params:
        target = params.get("target") or {}
        tgt = target.get("entity_id", "")
        tgt = ",".join(tgt) if isinstance(tgt, list) else str(tgt)
        data = params.get("service_data") or {}
        bits = [f"{params['domain']}.{params.get('service', '?')}", tgt, _fmt_value(data) if data else ""]
        return "called " + " ".join(b for b in bits if b)
    if result:
        return _short(_fmt_value(result), 160)
    return ""


def _is_restatement(step: dict[str, Any]) -> bool:
    """A condition sub-step whose result only repeats its parent's true/false."""
    result = step.get("result") or {}
    return "error" not in step and set(result) <= {"result", "entities"}


def step_lines(steps: dict[str, list[dict]], config: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for path, path_steps in steps.items():
        if path.startswith("trigger"):
            continue
        depth = path.count("/")
        if depth == 1:
            label = describe_step(_config_node(config, path))
            outcome = _result_text(path, path_steps[0]) if path_steps else ""
            branch = (steps.get(f"{path}/if") or [{}])[0].get("result") or {}
            if isinstance(branch.get("result"), bool):
                outcome = "→ true" if branch["result"] else "→ false"
            text = ": ".join(x for x in (path, label) if x)
            lines.append(f"{text} {outcome}".rstrip() if outcome.startswith("→")
                         else f"{text}: {outcome}" if outcome else f"{text}: ran")
            continue
        if path.endswith("/if"):
            continue  # folded into the parent line
        for step in path_steps:
            if "/condition/" in path or path.split("/")[-2] in ("entity_id", "conditions"):
                if _is_restatement(step):
                    continue
            outcome = _result_text(path, step)
            if outcome:
                lines.append(f"  {path}: {outcome}")
    # Repeat loops produce identical lines; count them instead.
    collapsed: list[str] = []
    counts: list[int] = []
    for line in lines:
        if collapsed and collapsed[-1] == line:
            counts[-1] += 1
        else:
            collapsed.append(line)
            counts.append(1)
    return [f"{ln} (×{n})" if n > 1 else ln for ln, n in zip(collapsed, counts)]


# script_execution codes, in words a model won't misread. "failed_single" in
# particular is not a failure of the run's steps: the run never started.
OUTCOMES = {
    "finished": "finished",
    "failed_conditions": "stopped: a condition was false",
    "failed_single": "NOT RUN: an earlier run was still in progress (mode: single)",
    "failed_max_runs": "NOT RUN: too many runs already in progress (mode: queued/parallel)",
    "cancelled": "cancelled before finishing (replaced by a newer run, or turned off)",
    "error": "stopped with an error",
    "running": "still running when collected",
}
REJECTED = ("failed_single", "failed_max_runs")


def _outcome(run: dict[str, Any]) -> str:
    if run.get("state") == "running":
        return "running"
    return run.get("script_execution") or run.get("state") or "?"


def _fold_rejected(events: list[Event], codes: list[str]) -> list[Event]:
    """Collapse back-to-back rejected triggers into one line."""
    out: list[Event] = []
    i = 0
    while i < len(events):
        j = i
        while j + 1 < len(events) and codes[j + 1] == codes[i] and codes[i] in REJECTED:
            j += 1
        if j > i:
            first = events[i]
            first.text = (f"{j - i + 1} triggers {OUTCOMES[codes[i]]}, "
                          f"until {{t:{events[j].when.isoformat()}}}")
            first.detail = []
            out.append(first)
        else:
            out.append(events[i])
        i = j + 1
    return out


def trace_events(traces: dict[str, Any], start: datetime, end: datetime,
                 opts: Options, dropped: Dropped) -> tuple[list[Event], dict[str, Any]]:
    events: list[Event] = []
    configs: dict[str, Any] = {}
    for entity, info in traces.items():
        seen: dict[tuple, str] = {}  # step signature -> first run id with it
        mine: list[Event] = []
        codes: list[str] = []
        runs = info.get("runs", [])
        for run in runs:
            when = _ts(run["timestamp"]["start"])
            if not (start <= when <= end):
                dropped.add("traces outside the window")
                continue
            trig = run.get("trigger") or ""
            steps = run.get("trace") or {}
            trig_steps = next((v for k, v in steps.items() if k.startswith("trigger")), [])
            for tstep in trig_steps:
                tv = (tstep.get("changed_variables") or {}).get("trigger") or {}
                fs, ts = tv.get("from_state") or {}, tv.get("to_state") or {}
                if fs.get("state") is not None and ts.get("state") is not None:
                    trig += f" ({fs['state']} → {ts['state']})"
                if not trig and tv.get("description"):
                    trig = tv["description"]
            code = _outcome(run)
            outcome = OUTCOMES.get(code, code)
            run_id = run.get("run_id", "")[:8]
            text = f"RUN {run_id} trigger: {trig or '?'} → {outcome}"
            config = run.get("config") or {}
            detail = step_lines(steps, config)
            if run.get("error"):
                detail.append(f"error: {_short(str(run['error']))}")
            signature = (outcome, tuple(detail))
            if detail and signature in seen:
                dropped.add("automation runs with the same steps as an earlier run "
                            "(steps shown once)")
                detail = [f"(same steps as RUN {seen[signature]})"]
            else:
                seen[signature] = run_id
            mine.append(Event(when, entity, text, detail, order=-1))
            codes.append(code)
            if opts.include_config and config:
                configs[entity] = config
        folded = _fold_rejected(mine, codes)
        dropped.add("rejected triggers folded into one line", len(mine) - len(folded))
        events.extend(folded)
        stored = info.get("stored")
        if runs and stored is not None and len(runs) >= stored:
            # Every stored trace is inside the window, so HA has likely discarded
            # earlier runs in it, including any long run that caused the rejections.
            oldest = min(_ts(r["timestamp"]["start"]) for r in runs)
            events.append(Event(start, entity,
                f"traces: WARNING HA keeps only {stored} traces for this automation and the "
                f"oldest is from {{t:{oldest.isoformat()}}}; earlier runs in the window are "
                "missing. Raise 'trace: stored_traces:' in the automation's YAML.", order=-2))
        if info.get("note"):
            events.append(Event(start, entity, f"traces: {info['note']}"))
    return events, configs


# ---------------------------------------------------------------- error log

@dataclass
class LogRecord:
    when: datetime
    level: str
    logger: str
    message: str
    count: int = 1
    last: datetime | None = None
    first: datetime | None = None  # set when the source already grouped repeats


def parse_error_log(text: str, tz: tzinfo | None) -> list[LogRecord]:
    records: list[LogRecord] = []
    tail: list[str] = []

    def finish() -> None:
        # For tracebacks, the last line names the exception; keep it.
        if records and tail:
            last = next((t.strip() for t in reversed(tail) if t.strip()), "")
            if last and last not in records[-1].message:
                records[-1].message += f" | {last}"
        tail.clear()

    for line in text.splitlines():
        m = _LOG_LINE.match(line)
        if m:
            finish()
            ts = datetime.fromisoformat(m.group(1))
            ts = ts.replace(tzinfo=tz) if tz else ts.astimezone()
            records.append(LogRecord(ts, m.group(2), m.group(4), m.group(5).strip()))
        elif records:
            tail.append(line)
    finish()
    return records


def system_log_records(entries: list[dict[str, Any]], start: datetime, end: datetime,
                       dropped: Dropped) -> list[LogRecord]:
    """Records from WebSocket system_log/list, which HA has already grouped.

    Each entry covers first_occurred..timestamp (last), with a count over that
    whole span, which can reach back before the window.
    """
    out = []
    for e in entries:
        if e.get("level") not in LEVELS:
            dropped.add("error-log records below WARNING")
            continue
        first = _ts(e.get("first_occurred") or e["timestamp"])
        last = _ts(e["timestamp"])
        if last < start or first > end:
            dropped.add("error-log records outside the window")
            continue
        msgs = [m for m in (e.get("message") or []) if m]
        message = " / ".join(dict.fromkeys(msgs)) or "(no message)"
        exc = (e.get("exception") or "").strip()
        if exc:
            final = exc.splitlines()[-1].strip()
            if final and final not in message:
                message += f" | {final}"
        rec = LogRecord(max(first, start), e["level"], e.get("name", "?"), message,
                        count=int(e.get("count") or 1), last=last)
        rec.first = first
        out.append(rec)
    return out


def error_events(text: str, entities: set[str], start: datetime, end: datetime,
                 opts: Options, dropped: Dropped,
                 system_log: list[dict[str, Any]] | None = None) -> list[Event]:
    unique: dict[tuple[str, str], LogRecord] = {}
    if system_log is not None:
        for rec in system_log_records(system_log, start, end, dropped):
            unique[(rec.logger, rec.message)] = rec
    for rec in parse_error_log(text, opts.tz):
        if rec.level not in LEVELS:
            dropped.add("error-log records below WARNING")
            continue
        if not (start <= rec.when <= end):
            dropped.add("error-log records outside the window")
            continue
        key = (rec.logger, rec.message)
        if key in unique:
            unique[key].count += 1
            unique[key].last = rec.when
            dropped.add("repeated error-log records (counted on the first)")
        else:
            unique[key] = rec

    recs = list(unique.values())
    if len(recs) > opts.max_errors:
        # Prefer records that mention a requested entity, then errors over warnings.
        def score(r: LogRecord) -> tuple[int, int]:
            mentions = any(e in r.message for e in entities)
            return (0 if mentions else 1, LEVELS.index(r.level) * -1)
        keep = sorted(recs, key=score)[: opts.max_errors]
        dropped.add("unique error-log messages over the limit", len(recs) - len(keep))
        recs = keep

    out = []
    for r in recs:
        text = f"{r.level} [{r.logger}] {_short(r.message, 300)}"
        if r.first is not None and (r.count > 1 or r.first < start):
            span = f"first at {{t:{r.first.isoformat()}}}"
            if r.count > 1:
                span = f"×{r.count} total, {span}, last at {{t:{r.last.isoformat()}}}"
            text += f" ({span})"
        elif r.count > 1:
            text += f" (×{r.count}, last at {{t:{r.last.isoformat()}}})"
        out.append(Event(r.when, "log", text))
    return out


# ---------------------------------------------------------------- render

def _fmt_time(dt: datetime, tz: tzinfo | None, multi_day: bool) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%m-%d %H:%M:%S" if multi_day else "%H:%M:%S")


def render(meta: dict[str, Any], events: list[Event], configs: dict[str, Any],
           dropped: Dropped, opts: Options) -> str:
    start, end = _ts(meta["start"]), _ts(meta["end"])
    tz = opts.tz
    multi_day = start.astimezone(tz).date() != end.astimezone(tz).date()
    zone = str(tz) if tz else (start.astimezone().tzname() or "local")

    def t(dt: datetime) -> str:
        return _fmt_time(dt, tz, multi_day)

    def t_any(dt: datetime) -> str:
        if start <= dt <= end:
            return t(dt)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# Home Assistant timeline",
        f"Window: {start.astimezone(tz):%Y-%m-%d %H:%M:%S} to {end.astimezone(tz):%Y-%m-%d %H:%M:%S} ({zone})",
        f"Entities: {', '.join(meta['entities'])}",
        *[f"Note: {n}" for n in meta.get("notes", [])],
        "Format: time, entity, then what happened. 'a → b' is a state change; "
        "values in parentheses are attributes that changed; text in [brackets] is what caused it. "
        "RUN lines are automation runs, with their steps indented below.",
        "",
    ]
    if configs:
        lines.append("## Automation configs (from traces)")
        for entity, cfg in configs.items():
            text = json.dumps(cfg, separators=(",", ":"))
            if opts.config_limit is not None and len(text) > opts.config_limit:
                lines.append(f"{entity}: left out ({len(text)} characters). "
                             "Trace steps below are labelled from it.")
                dropped.add("automation configs over the size limit (attach the YAML with -c, "
                            "or pass --config)")
            else:
                lines.append(f"{entity}: {text}")
        lines.append("")

    lines.append("## Events")
    width = max((len(e.entity) for e in events), default=10)
    pattern = re.compile(r"\{t:([^}]+)\}")
    for ev in sorted(events, key=lambda e: (e.when, e.order)):
        text = pattern.sub(lambda m: t_any(_ts(m.group(1))), ev.text)
        lines.append(f"{t(ev.when)}  {ev.entity:<{width}}  {text}")
        for d in ev.detail:
            lines.append(f"{'':>{len(t(ev.when))}}  {'':<{width}}    {d}")
    if not events:
        lines.append("(nothing recorded for these entities in this window)")

    lines += ["", "## Dropped"]
    if dropped.counts:
        lines += [f"- {n} {reason}" for reason, n in sorted(dropped.counts.items())]
    else:
        lines.append("- nothing")
    return "\n".join(lines) + "\n"


def condense(run_dir: Path, opts: Options | None = None) -> str:
    opts = opts or Options()
    meta = json.loads((run_dir / "meta.json").read_text())
    start, end = _ts(meta["start"]), _ts(meta["end"])
    entities = set(meta["entities"])
    dropped = Dropped()

    def load(name: str, default: Any) -> Any:
        p = run_dir / name
        if not p.exists():
            return default
        return p.read_text() if name.endswith(".txt") else json.loads(p.read_text())

    history = load("history.json", [])
    events = history_events(history, start, opts, dropped)
    notes = missing_history(history, entities, start)
    tevents, configs = trace_events(load("traces.json", {}), start, end, opts, dropped)
    events += logbook_events(load("logbook.json", []), entities, start, end, events, tevents,
                             opts, dropped)
    events += tevents + notes
    events += error_events(load("error_log.txt", ""), entities, start, end, opts, dropped,
                           system_log=load("system_log.json", None))
    return render(meta, events, configs, dropped, opts)

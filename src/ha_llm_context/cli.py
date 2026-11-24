"""Command line: collect, condense, ask, or run all three."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import ask as ask_mod
from .collect import collect
from .condense import Options, condense
from .config import HASettings, LLMSettings, load_dotenv, parse_when
from .pipeline import find_entities
from .references import find_references
from .tools import DEFAULT_MAX_RELATED, DEFAULT_WARN_TOKENS, ToolConfig


def _add_collect_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-e", "--entity", action="append", required=True,
                   help="entity to include (repeatable); automation.* entities also pull traces")
    p.add_argument("--since", default="2h", help="window start: 30m, 2h, 1d, or ISO time (default 2h)")
    p.add_argument("--until", default=None, help="window end: same formats (default now)")
    p.add_argument("--out", type=Path, default=None, help="run directory (default out/<timestamp>)")
    p.add_argument("--no-related", action="store_true",
                   help="don't add entities that the automations' configs refer to")
    p.add_argument("--max-related", type=int, default=30, help="related entities to add (default 30)")


def _add_condense_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tz", default=None, help="display time zone, e.g. America/New_York (default local)")
    p.add_argument("--attr", action="append", default=[], help="extra attribute to keep (repeatable)")
    p.add_argument("--max-changes", type=int, default=40, help="per-entity change limit (default 40)")
    p.add_argument("--max-errors", type=int, default=30, help="unique error-log messages (default 30)")
    cfg = p.add_mutually_exclusive_group()
    cfg.add_argument("--no-config", action="store_true", help="leave automation configs out")
    cfg.add_argument("--config", action="store_true",
                     help="include automation configs however long (default: only under 2000 characters)")


def _add_ask_args(p: argparse.ArgumentParser, required: bool) -> None:
    p.add_argument("-q", "--question", required=required, help="what to ask the model")
    p.add_argument("-c", "--context", action="append", type=Path, default=[],
                   help="file to attach, e.g. an automation's YAML (repeatable)")
    p.add_argument("--max-tokens", type=int, default=8000,
                   help="warn when the prompt is estimated above this (default 8000)")


def _options(args: argparse.Namespace) -> Options:
    return Options(
        tz=ZoneInfo(args.tz) if args.tz else None,
        extra_attrs=tuple(args.attr),
        max_changes_per_entity=args.max_changes,
        max_errors=args.max_errors,
        include_config=not args.no_config,
        config_limit=None if args.config else 2000,
    )


def _do_collect(args: argparse.Namespace) -> Path:
    end = parse_when(args.until) if args.until else datetime.now().astimezone()
    start = parse_when(args.since, now=end)
    out = args.out or Path("out") / datetime.now().strftime("%Y%m%d-%H%M%S")
    collect(HASettings.from_env(), args.entity, start, end, out,
            related=not args.no_related, max_related=args.max_related)
    print(f"Collected into {out}", file=sys.stderr)
    return out


def _do_condense(run_dir: Path, args: argparse.Namespace) -> str:
    timeline = condense(run_dir, _options(args))
    (run_dir / "timeline.txt").write_text(timeline)
    print(f"Wrote {run_dir / 'timeline.txt'} (~{ask_mod.estimate_tokens(timeline)} tokens)",
          file=sys.stderr)
    return timeline


def _do_ask(timeline: str, args: argparse.Namespace) -> None:
    messages = ask_mod.build_messages(timeline, args.question, args.context)
    tokens = sum(ask_mod.estimate_tokens(m["content"]) for m in messages)
    if tokens > args.max_tokens:
        print(f"Warning: prompt is about {tokens} tokens, over --max-tokens {args.max_tokens}. "
              "Narrow the window or entities, or raise the model's context size.", file=sys.stderr)
    print(ask_mod.ask(LLMSettings.from_env(), messages))


def _do_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import serve

    cfg = ToolConfig(HASettings.from_env(), _options(args), args.keep_raw,
                     not args.no_related, args.max_related, args.warn_tokens)
    serve(cfg, http=args.http, host=args.host, port=args.port,
          token=os.environ.get("MCP_TOKEN", ""))


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="ha-llm-context", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("find", help="search entities by ID or friendly name")
    p.add_argument("query", nargs="+", help="words that must all appear, e.g. office climate")
    p.add_argument("--domain", default="", help="only this domain, e.g. climate")

    p = sub.add_parser("refs", help="list automations, scripts, scenes, etc. that refer to an entity")
    p.add_argument("entity_id", help="exact entity ID, e.g. sensor.office_temperature")

    p = sub.add_parser("collect", help="pull raw data from Home Assistant")
    _add_collect_args(p)

    p = sub.add_parser("condense", help="turn a run directory into timeline.txt")
    p.add_argument("run_dir", type=Path)
    _add_condense_args(p)
    p.add_argument("--print", action="store_true", help="also print the timeline")

    p = sub.add_parser("ask", help="ask the model about a run directory or timeline file")
    p.add_argument("source", type=Path, help="run directory or timeline file")
    _add_ask_args(p, required=True)

    p = sub.add_parser("run", help="collect, condense, and (with -q) ask")
    _add_collect_args(p)
    _add_condense_args(p)
    _add_ask_args(p, required=False)

    p = sub.add_parser("mcp", help="run as an MCP server (stdio, or --http for a service)")
    p.add_argument("--http", action="store_true",
                   help="serve streamable HTTP at /mcp instead of stdio")
    p.add_argument("--host", default="127.0.0.1",
                   help="HTTP listen address (default 127.0.0.1; 0.0.0.0 in a container)")
    p.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    p.add_argument("--keep-raw", type=Path, default=None, metavar="DIR",
                   help="keep each raw pull and its timeline under DIR (default: keep nothing)")
    p.add_argument("--no-related", action="store_true",
                   help="don't add entities that the automations' configs refer to")
    p.add_argument("--max-related", type=int, default=DEFAULT_MAX_RELATED,
                   help=f"related entities to add (default {DEFAULT_MAX_RELATED})")
    p.add_argument("--warn-tokens", type=int, default=DEFAULT_WARN_TOKENS,
                   help=f"warn the model above this many tokens (default {DEFAULT_WARN_TOKENS})")
    _add_condense_args(p)

    args = parser.parse_args(argv)
    if args.cmd == "find":
        print(find_entities(HASettings.from_env(), " ".join(args.query), args.domain))
    elif args.cmd == "refs":
        print(find_references(HASettings.from_env(), args.entity_id.strip().lower()), end="")
    elif args.cmd == "collect":
        print(_do_collect(args))
    elif args.cmd == "condense":
        timeline = _do_condense(args.run_dir, args)
        if args.print:
            print(timeline, end="")
    elif args.cmd == "ask":
        src = args.source / "timeline.txt" if args.source.is_dir() else args.source
        _do_ask(src.read_text(), args)
    elif args.cmd == "mcp":
        _do_mcp(args)
    elif args.cmd == "run":
        run_dir = _do_collect(args)
        timeline = _do_condense(run_dir, args)
        if args.question:
            _do_ask(timeline, args)
        else:
            print(timeline, end="")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import sys
import time

from . import annotate, discover, envelope, pretty


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pj",
        description="Project Juggler — scheduler for human attention",
    )
    sub = parser.add_subparsers(dest="command")

    ls = sub.add_parser("list", help="List projects with derived state")
    ls.add_argument("--pretty", action="store_true", help="Human-readable table output")
    ls.add_argument(
        "--state",
        choices=["active", "stale", "dormant", "blocked", "archived"],
        help="Filter by state",
    )
    ls.add_argument(
        "--sort",
        choices=["last-active", "priority", "name"],
        default="last-active",
        help="Sort order (default: last-active)",
    )
    ls.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    ls.add_argument("--offset", type=int, default=0, help="Skip N results (default: 0)")

    note_p = sub.add_parser("note", help="Add a free-text note to a project")
    note_p.add_argument("project", help="Project path")
    note_p.add_argument("text", help="Note text")

    pri = sub.add_parser("prioritize", help="Set project priority")
    pri.add_argument("project", help="Project path")
    pri.add_argument("level", choices=["high", "medium", "low", "none"], help="Priority level")

    arc = sub.add_parser("archive", help="Archive a project")
    arc.add_argument("project", help="Project path")

    tag_p = sub.add_parser("tag", help="Add a tag to a project")
    tag_p.add_argument("project", help="Project path")
    tag_p.add_argument("tag", help="Tag name")

    return parser


def _cmd_list(args: argparse.Namespace) -> None:
    start = time.monotonic()
    projects, total = discover.discover(
        state_filter=args.state,
        sort=args.sort,
        limit=args.limit,
        offset=args.offset,
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    if args.pretty:
        pretty.print_projects(projects, total, args.offset, args.limit)
    else:
        env = envelope.ok(
            projects,
            total=total,
            offset=args.offset,
            limit=args.limit,
            latency_ms=latency_ms,
        )
        print(envelope.to_json(env))


def _cmd_annotate(action) -> None:
    start = time.monotonic()
    event = action()
    latency_ms = int((time.monotonic() - start) * 1000)
    env = envelope.ok(event, latency_ms=latency_ms)
    print(envelope.to_json(env))


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        _cmd_list(args)
    elif args.command == "note":
        _cmd_annotate(lambda: annotate.note(args.project, args.text))
    elif args.command == "prioritize":
        _cmd_annotate(lambda: annotate.prioritize(args.project, args.level))
    elif args.command == "archive":
        _cmd_annotate(lambda: annotate.archive(args.project))
    elif args.command == "tag":
        _cmd_annotate(lambda: annotate.tag(args.project, args.tag))

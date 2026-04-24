from __future__ import annotations

import argparse
import sys
import time

from . import discover, envelope, pretty


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


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        _cmd_list(args)

from __future__ import annotations

import argparse
import sys
import time

from . import annotate, cass_facade, discover, envelope, pretty, resume, schedule
from . import search as search_mod


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
    ls.add_argument("--tag", help="Filter by tag")
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

    next_p = sub.add_parser("next", help="What to work on next (scored recommendations)")
    next_p.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")
    next_p.add_argument("--pretty", action="store_true", help="Human-readable output")

    stat = sub.add_parser("status", help="Deep view of a project: sessions, notes, resume cmd")
    stat.add_argument("project", help="Project name, path, or id prefix")
    stat.add_argument("--pretty", action="store_true", help="Human-readable output")
    stat.add_argument("--sessions", type=int, default=10, help="Max sessions to show (default: 10)")

    res = sub.add_parser("resume", help="Output cd + agent --resume for most recent session")
    res.add_argument("project", help="Project name, path, or id prefix")

    srch = sub.add_parser("search", help="Search projects by keyword")
    srch.add_argument("query", help="Search query")
    srch.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    srch.add_argument("--pretty", action="store_true", help="Human-readable output")

    return parser


def _cmd_list(args: argparse.Namespace) -> None:
    start = time.monotonic()
    projects, total = discover.discover(
        state_filter=args.state,
        tag_filter=args.tag,
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


def _cmd_next(args: argparse.Namespace) -> None:
    start = time.monotonic()
    projects, _ = discover.discover(limit=9999)
    scored = schedule.score_projects(projects)[: args.limit]
    latency_ms = int((time.monotonic() - start) * 1000)

    if args.pretty:
        pretty.print_next(scored)
    else:
        env = envelope.ok(scored, limit=args.limit, latency_ms=latency_ms)
        print(envelope.to_json(env))


def _cmd_status(args: argparse.Namespace) -> None:
    start = time.monotonic()
    project = discover.resolve_project(args.project)
    if project is None:
        env = envelope.err(f"No project matching {args.project!r}")
        print(envelope.to_json(env))
        sys.exit(1)

    sessions = cass_facade.project_sessions(project["path"], limit=args.sessions)
    resume_cmd = None
    if sessions:
        latest = sessions[0]
        resume_cmd = resume.full_resume_command(
            project["path"], latest["agent"], latest["session_id"],
        )

    status_data = {
        **project,
        "sessions": sessions,
        "resume_cmd": resume_cmd,
    }
    latency_ms = int((time.monotonic() - start) * 1000)

    if args.pretty:
        pretty.print_status(status_data)
    else:
        env = envelope.ok(status_data, latency_ms=latency_ms)
        print(envelope.to_json(env))


def _cmd_resume(args: argparse.Namespace) -> None:
    project = discover.resolve_project(args.project)
    if project is None:
        env = envelope.err(f"No project matching {args.project!r}")
        print(envelope.to_json(env))
        sys.exit(1)

    sessions = cass_facade.project_sessions(project["path"], limit=1)
    if not sessions:
        env = envelope.err(f"No sessions found for {project['name']}")
        print(envelope.to_json(env))
        sys.exit(1)

    latest = sessions[0]
    cmd = resume.full_resume_command(
        project["path"], latest["agent"], latest["session_id"],
    )
    print(cmd)


def _cmd_search(args: argparse.Namespace) -> None:
    start = time.monotonic()
    results = search_mod.search(args.query, limit=args.limit)
    latency_ms = int((time.monotonic() - start) * 1000)

    if args.pretty:
        pretty.print_search(results, args.query)
    else:
        env = envelope.ok(
            results, query=args.query, total=len(results), latency_ms=latency_ms,
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
    elif args.command == "next":
        _cmd_next(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "resume":
        _cmd_resume(args)
    elif args.command == "search":
        _cmd_search(args)

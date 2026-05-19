from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from . import annotate, discover, envelope, pretty, resume, schedule
from . import search as search_mod
from .session_store import get_store

SEARCH_HELP = """\
Search examples:
  pj search sport --pretty
  pj search --here sport --pretty
  pj search football soccer --pretty
  pj search football soccer --match all --project epic-odds --pretty
  pj search 'foot(ball)?|soccer' --regex --project epic-odds --pretty
  pj search sport --sort relevance --pretty
  pj search soccer --sort oldest --pretty

Query strategy:
  Separate words are separate terms. Use this for exploratory agent searches.
  A quoted multi-word query is an exact substring phrase and can miss related sessions.
  Use --project to search within one project before broadening.
  Use --here to infer --project from the current working directory.
  Use --match all when every term must appear; default is --match any.
  Use --regex for alternatives, stems, and spelling variants.
"""


# --- Usage hints (shown when required args are missing) ---

_USAGE = {
    "show": """\
Usage: pj show <project> [--pretty] [--sessions N]

  Drill into a project — sessions, notes, resume command.
  <project> is a name, path, or ID prefix from `pj list`.

  Examples:
    pj show hermes --pretty
    pj show 0b66 --sessions 20

  Journey: pj list → pj show <project> → pj chat <session_id>""",

    "chat": """\
Usage: pj chat <session_id> [--pretty] [--no-tools] [--roles ROLES] [--last N]

  Display a full session conversation.
  <session_id> is from `pj show <project>` output.

  Examples:
    pj chat 2af1c985 --pretty
    pj chat 2af1c985 --pretty --no-tools --last 10
    pj chat 2af1c985 --roles user,assistant
    pj chat 2af1c985 --all-branches --pretty

  Journey: pj list → pj show <project> → pj chat <session_id>""",

    "search": """\
Usage: pj search <query...> [--pretty] [--limit N] [--sort newest|relevance|oldest]

  Search projects and session content by keyword.

""" + SEARCH_HELP,

    "resume": """\
Usage: pj resume <project>

  Output cd + agent --resume command for the most recent session.
  <project> is a name, path, or ID prefix from `pj list`.

  Examples:
    pj resume hermes
    eval $(pj resume hermes)    # resume directly""",

    "note": """\
Usage: pj note <project> <text>

  Add a free-text note to a project.
  <project> is a name, path, or ID prefix from `pj list`.

  Examples:
    pj note hermes "blocked on API key"
    pj note 0b66 "cache bug fixed, needs tests" """,

    "prioritize": """\
Usage: pj prioritize <project> <level>

  Set project priority. <level> is high, medium, low, or none.

  Examples:
    pj prioritize hermes high
    pj prioritize 0b66 none""",

    "tag": """\
Usage: pj tag <project> <tag>

  Add a tag to a project.

  Examples:
    pj tag hermes research
    pj tag 0b66 hackathon""",

    "archive": """\
Usage: pj archive <project>

  Archive a project (excludes from recommendations).

  Examples:
    pj archive hermes
    pj archive 0b66""",
}


def _missing_arg(command: str) -> None:
    """Print educational usage hint and exit."""
    hint = _USAGE.get(command)
    if hint:
        print(hint, file=sys.stderr)
    sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pj",
        description="Project Juggler — scheduler for human attention",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    ls.add_argument("--detail", action="store_true", help="Include hours, models, first session (slower)")
    ls.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    ls.add_argument("--offset", type=int, default=0, help="Skip N results (default: 0)")

    note_p = sub.add_parser("note", help="Add a free-text note to a project")
    note_p.add_argument("project", nargs="?", help="Project name, path, or ID prefix")
    note_p.add_argument("text", nargs="?", help="Note text")

    pri = sub.add_parser("prioritize", help="Set project priority")
    pri.add_argument("project", nargs="?", help="Project name, path, or ID prefix")
    pri.add_argument("level", nargs="?", choices=["high", "medium", "low", "none"], help="Priority level")

    arc = sub.add_parser("archive", help="Archive a project")
    arc.add_argument("project", nargs="?", help="Project name, path, or ID prefix")

    tag_p = sub.add_parser("tag", help="Add a tag to a project")
    tag_p.add_argument("project", nargs="?", help="Project name, path, or ID prefix")
    tag_p.add_argument("tag_name", nargs="?", help="Tag name")

    next_p = sub.add_parser("next", help="What to work on next (scored recommendations)")
    next_p.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")
    next_p.add_argument("--pretty", action="store_true", help="Human-readable output")

    # "show" is the primary name; "status" kept as alias
    show = sub.add_parser("show", help="Drill into a project: sessions, notes, resume cmd")
    show.add_argument("project", nargs="?", help="Project name, path, or ID prefix")
    show.add_argument("--pretty", action="store_true", help="Human-readable output")
    show.add_argument("--sessions", type=int, default=10, help="Max sessions to show (default: 10)")

    res = sub.add_parser("resume", help="Output cd + agent --resume for most recent session")
    res.add_argument("project", nargs="?", help="Project name, path, or ID prefix")

    srch = sub.add_parser(
        "search",
        help="Search projects by keyword",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=SEARCH_HELP,
    )
    srch.add_argument("query", nargs="*", help="Search query term(s)")
    srch.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    srch.add_argument(
        "--sort",
        choices=["newest", "relevance", "oldest"],
        default="newest",
        help="Sort order: newest, relevance, or oldest (default: newest)",
    )
    srch.add_argument("--project", help="Restrict search to project name, path, or ID prefix")
    srch.add_argument(
        "--here",
        action="store_true",
        help="Restrict search to the discovered project containing the current directory",
    )
    srch.add_argument(
        "--match",
        choices=["any", "all"],
        default="any",
        help="For multiple terms, match any term or require all terms (default: any)",
    )
    srch.add_argument("--regex", action="store_true", help="Treat query terms as regular expressions")
    srch.add_argument("--pretty", action="store_true", help="Human-readable output")

    chat_p = sub.add_parser("chat", help="Display a full session conversation")
    chat_p.add_argument("session_id", nargs="?", help="Session ID or prefix (from `pj show` output)")
    chat_p.add_argument("--pretty", action="store_true", help="Render as markdown")
    chat_p.add_argument(
        "--all-branches", action="store_true",
        help="Show all branches including abandoned forks",
    )
    chat_p.add_argument(
        "--no-tools", action="store_true",
        help="Strip tool calls and tool results",
    )
    chat_p.add_argument(
        "--roles", type=str, default=None,
        help="Comma-separated roles to include (e.g. user,assistant)",
    )
    chat_p.add_argument("--limit", type=int, default=None, help="Max messages")
    chat_p.add_argument("--offset", type=int, default=0, help="Skip first N messages")
    chat_p.add_argument("--last", type=int, default=None, help="Show only last N messages")

    census_p = sub.add_parser("census", help="Generate or serve the project census dashboard")
    census_p.add_argument("census_action", nargs="?", choices=["serve"], help="Start the live dashboard server")
    census_p.add_argument("--limit", type=int, default=10000, help="Max projects (default: 10000)")
    census_p.add_argument("--host", default="127.0.0.1", help="Serve host (default: 127.0.0.1)")
    census_p.add_argument("--port", type=int, default=8765, help="Serve port (default: 8765)")
    census_p.add_argument(
        "--check-interval",
        type=int,
        default=60,
        help="Seconds between session signature checks while serving (default: 60)",
    )

    return parser


def _cmd_list(args: argparse.Namespace) -> None:
    start = time.monotonic()
    projects, total = discover.discover(
        state_filter=args.state,
        tag_filter=args.tag,
        sort=args.sort,
        limit=args.limit,
        offset=args.offset,
        detail=args.detail,
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


def _cmd_show(args: argparse.Namespace) -> None:
    if not args.project:
        _missing_arg("show")

    start = time.monotonic()
    project = discover.resolve_project(args.project)
    if project is None:
        env = envelope.err(f"No project matching {args.project!r}")
        print(envelope.to_json(env))
        sys.exit(1)

    sessions = get_store().project_sessions(project["path"], limit=args.sessions)
    resume_cmd = None
    if sessions:
        latest = sessions[0]
        resume_cmd = resume.full_resume_command(
            project["path"], latest["agent"], latest["session_id"],
        )
        # Enrich sessions with harness version and per-message models
        sids = [s["session_id"] for s in sessions]
        details = get_store().session_details(sids)
        for s in sessions:
            d = details.get(s["session_id"], {})
            s["versions"] = d.get("versions", [])
            s["models"] = d.get("models", [])

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
    if not args.project:
        _missing_arg("resume")

    project = discover.resolve_project(args.project)
    if project is None:
        env = envelope.err(f"No project matching {args.project!r}")
        print(envelope.to_json(env))
        sys.exit(1)

    sessions = get_store().project_sessions(project["path"], limit=1)
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
    if not args.query:
        _missing_arg("search")

    start = time.monotonic()
    project_filter = args.project
    if args.here:
        if args.project:
            env = envelope.err("Use either --project or --here, not both", source="search")
            print(envelope.to_json(env))
            sys.exit(1)
        current_project = discover.resolve_project_for_cwd()
        if current_project is None:
            env = envelope.err("Current directory is not inside a discovered project", source="search")
            print(envelope.to_json(env))
            sys.exit(1)
        project_filter = current_project["path"]

    try:
        results = search_mod.search(
            args.query,
            limit=args.limit,
            sort=args.sort,
            project=project_filter,
            match=args.match,
            regex=args.regex,
        )
    except ValueError as exc:
        env = envelope.err(str(exc), source="search")
        print(envelope.to_json(env))
        sys.exit(1)
    latency_ms = int((time.monotonic() - start) * 1000)

    if args.pretty:
        pretty.print_search(results, args.query)
    else:
        env = envelope.ok(
            results,
            query=args.query,
            project=project_filter,
            here=args.here,
            match=args.match,
            regex=args.regex,
            sort=args.sort,
            total=len(results),
            latency_ms=latency_ms,
        )
        print(envelope.to_json(env))


def _cmd_chat(args: argparse.Namespace) -> None:
    if not args.session_id:
        _missing_arg("chat")

    start = time.monotonic()
    roles = set(args.roles.split(",")) if args.roles else None

    result = get_store().get_session(
        args.session_id,
        all_branches=args.all_branches,
        include_tools=not args.no_tools,
        roles=roles,
    )

    if result is None:
        env = envelope.err(f"Session {args.session_id!r} not found")
        print(envelope.to_json(env))
        sys.exit(1)

    messages = result["messages"]

    # --last takes from the end before offset/limit
    if args.last is not None:
        messages = messages[-args.last:]

    total = len(messages)
    messages = messages[args.offset:]
    if args.limit is not None:
        messages = messages[: args.limit]

    result["messages"] = messages
    latency_ms = int((time.monotonic() - start) * 1000)

    if args.pretty:
        pretty.print_chat(result)
    else:
        env = envelope.ok(
            result,
            total_messages=total,
            offset=args.offset,
            limit=args.limit,
            latency_ms=latency_ms,
        )
        print(envelope.to_json(env))


def _cmd_census(args: argparse.Namespace) -> None:
    if args.census_action == "serve":
        from . import census_server

        census_server.serve(
            host=args.host,
            port=args.port,
            limit=args.limit,
            check_interval=args.check_interval,
        )
        return

    from . import census

    snap = census.snapshot(limit=args.limit)
    print(envelope.to_json(envelope.ok(snap["rows"], **snap["meta"])))


def _cmd_annotate(action) -> None:
    start = time.monotonic()
    event = action()
    latency_ms = int((time.monotonic() - start) * 1000)
    env = envelope.ok(event, latency_ms=latency_ms)
    print(envelope.to_json(env))


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    # "status" is a hidden alias for "show"
    raw = argv if argv is not None else sys.argv[1:]
    if raw and raw[0] == "status":
        raw = ["show"] + raw[1:]
    args = parser.parse_args(raw)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        _cmd_list(args)
    elif args.command == "note":
        if not args.project or not args.text:
            _missing_arg("note")
        _cmd_annotate(lambda: annotate.note(args.project, args.text))
    elif args.command == "prioritize":
        if not args.project or not args.level:
            _missing_arg("prioritize")
        _cmd_annotate(lambda: annotate.prioritize(args.project, args.level))
    elif args.command == "archive":
        if not args.project:
            _missing_arg("archive")
        _cmd_annotate(lambda: annotate.archive(args.project))
    elif args.command == "tag":
        if not args.project or not args.tag_name:
            _missing_arg("tag")
        _cmd_annotate(lambda: annotate.tag(args.project, args.tag_name))
    elif args.command == "next":
        _cmd_next(args)
    elif args.command == "show":
        _cmd_show(args)
    elif args.command == "resume":
        _cmd_resume(args)
    elif args.command == "search":
        _cmd_search(args)
    elif args.command == "chat":
        _cmd_chat(args)
    elif args.command == "census":
        _cmd_census(args)

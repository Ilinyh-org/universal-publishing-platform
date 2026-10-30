"""`publisher` command line interface.

Roadmap step 1 ships read-only inspection only:

    publisher list-tasks [--status STATUS] [--limit N] [--demo]

`--demo` runs against a seeded in-memory database, so the CLI works with
no configuration and no infrastructure (docs/PLAN.md, step 1).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from . import __version__
from .core.config import Settings
from .db.demo import populate_demo_data
from .db.models import Base, TaskStatus
from .db.repositories.tasks import list_tasks
from .db.session import create_engine, make_session_factory

_DEMO_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
_STATUS_CHOICES = [status.value for status in TaskStatus] + ["all"]


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publisher", description="Universal Publishing Platform CLI"
    )
    parser.add_argument("--version", action="version", version=f"publisher {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-tasks",
        aliases=["list-pending"],
        help="list publication tasks (read-only; defaults to pending)",
    )
    list_parser.add_argument(
        "--status", default="pending", choices=_STATUS_CHOICES, help="filter by task status"
    )
    list_parser.add_argument(
        "--limit", type=_positive_int, default=20, help="maximum rows to show"
    )
    list_parser.add_argument(
        "--demo", action="store_true", help="use a seeded in-memory database"
    )
    list_parser.set_defaults(handler=_run_list_tasks)
    return parser


def _title_of(content_json: dict[str, Any] | None) -> str:
    if not content_json:
        return ""
    title = content_json.get("title") or ""
    if not title:
        body = content_json.get("body") or ""
        title = body.splitlines()[0] if body else ""
    return title[:40]


async def _run_list_tasks(settings: Settings, args: argparse.Namespace) -> int:
    status = None if args.status == "all" else TaskStatus(args.status)
    database_url = _DEMO_DATABASE_URL if args.demo else settings.database_url
    engine = create_engine(database_url)
    factory = make_session_factory(engine)
    try:
        if args.demo:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with factory() as session:
                await populate_demo_data(session)
        try:
            async with factory() as session:
                tasks = await list_tasks(session, status=status, limit=args.limit)
        except OperationalError:
            print(
                "error: database not initialized or unreachable; "
                "run database migrations (roadmap step 2) or use --demo.",
                file=sys.stderr,
            )
            return 2
    finally:
        await engine.dispose()

    header = f"{'ID':8} {'STATUS':11} {'ACCOUNT':18} {'TYPE':12} {'ATT':>3}  {'SCHEDULED':25} TITLE"
    print(header)
    for task in tasks:
        scheduled = task.scheduled_at.isoformat(timespec="seconds") if task.scheduled_at else "-"
        row = (
            f"{task.id[:8]:8} {task.status:11} {task.account.key:18} "
            f"{task.post.post_type:12} {task.attempt_count:>3}  {scheduled:25} "
            f"{_title_of(task.post.content_json)}"
        )
        print(row)
    print(f"showing {len(tasks)} task(s) (status={args.status}, limit={args.limit})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"error: invalid configuration: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(args.handler(settings, args))

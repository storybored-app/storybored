"""`python -m storybored` / `storybored` — run the server on STORYBORED_PORT.

Subcommands: `python -m storybored relocate <dest>` moves the data directory
(offline maintenance op — refuses while the server is running)."""

import argparse
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

import uvicorn

from storybored.config import Settings

log = logging.getLogger("storybored")

#: hosts that keep the server reachable only from this machine
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def relocate(settings: Settings, dest_arg: str) -> int:
    """Move DATA_DIR to a new location and print the env line to set.

    Offline op: refuses when the database is locked (server still running).
    Returns a process exit code."""
    src = settings.data_path
    dest = Path(dest_arg).expanduser().resolve()
    if not src.exists():
        print(f"nothing to move: data directory {src} does not exist")
        return 1
    if dest == src:
        print(f"data is already at {dest}")
        return 1
    if dest.is_relative_to(src):
        print("destination is inside the current data directory — pick a path outside it")
        return 1
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        print(f"destination {dest} already exists and is not an empty directory")
        return 1

    db = settings.db_path
    if db.exists():
        try:
            conn = sqlite3.connect(db, timeout=0.2)
            try:
                conn.execute("BEGIN IMMEDIATE")  # write lock = nobody else has one
                conn.rollback()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            print("the database is locked — is StoryBored still running? Stop it first.")
            return 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.rmdir()  # empty dir: remove so shutil.move renames instead of nesting
    shutil.move(str(src), str(dest))
    print(f"moved {src} -> {dest}")
    print("Point StoryBored at the new location: set this in your .env")
    print(f"DATA_DIR={dest}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="storybored", description="StoryBored server")
    parser.add_argument(
        "--demo", action="store_true", help="create the demo project before serving"
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "interface to bind (default: this machine only). Use 0.0.0.0 to "
            "expose on your LAN — there is NO password, so only on a trusted network"
        ),
    )
    sub = parser.add_subparsers(dest="command")
    relocate_parser = sub.add_parser(
        "relocate", help="move the data directory (run while the server is stopped)"
    )
    relocate_parser.add_argument("dest", help="new location for the data directory")
    args = parser.parse_args()

    settings = Settings()

    if args.command == "relocate":
        sys.exit(relocate(settings, args.dest))

    # Precedence: --host flag > STORYBORED_HOST env > 127.0.0.1 (loopback).
    host = args.host or settings.storybored_host or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS:
        log.warning(
            "StoryBored is exposed on the network with NO authentication "
            "— only do this on a trusted LAN"
        )

    if args.demo:
        from sqlmodel import Session

        from storybored.db import create_db_engine, init_db
        from storybored.seed.demo import create_demo

        engine = create_db_engine(settings)
        init_db(engine)
        with Session(engine, expire_on_commit=False) as session:
            project = create_demo(session)
        engine.dispose()
        print(f"demo project created: #{project.id} {project.title}")

    from storybored.main import create_app

    uvicorn.run(create_app(settings), host=host, port=settings.storybored_port)


if __name__ == "__main__":
    main()

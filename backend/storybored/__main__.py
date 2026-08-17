"""`python -m storybored` / `storybored` — run the server on STORYBORED_PORT.

Also hosts the offline pack linter: `python -m storybored validate-pack <dir>`
(see engine/validate.py and docs/WORKFLOWS.md)."""

import argparse
import logging
import sys

import uvicorn

from storybored.config import Settings

log = logging.getLogger("storybored")

#: hosts that keep the server reachable only from this machine
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main() -> None:
    # subcommand dispatch before the server arg parser (which owns the flags)
    if len(sys.argv) > 1 and sys.argv[1] == "validate-pack":
        from storybored.engine.validate import main as validate_main

        raise SystemExit(validate_main(sys.argv[2:]))

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
    args = parser.parse_args()

    settings = Settings()

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

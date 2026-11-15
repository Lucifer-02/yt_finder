"""Run the local web application with one queue-owning server process."""

import argparse
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> None:
    from .config import WebConfig, find_repository

    parser = argparse.ArgumentParser(
        prog="yt-web",
        description="Local transcript collection and search web application.",
    )
    parser.add_argument(
        "--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1"
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="yt-finder repository with its root .venv",
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--search-db", type=Path, default=None)
    parser.add_argument(
        "--state-db", type=Path, default=None, help="separate web jobs database"
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="rotating JSONL processing log; default: beside state DB in logs/",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="server console level; processing file always includes DEBUG",
    )
    args = parser.parse_args(argv)
    root = (args.root or find_repository()).expanduser().resolve()
    configuration = WebConfig(
        root,
        args.db or root / "data/index.sqlite3",
        args.search_db or root / "data/search.sqlite3",
        args.state_db or root / "data/web.sqlite3",
        args.log_file,
        args.log_level,
    )
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        configuration.validate()
        import uvicorn

        from .http import create_http_app

        app = create_http_app(configuration)

        class _GracefulServer(uvicorn.Server):
            def handle_exit(self, sig: int, frame: Any) -> None:
                close_streams = getattr(app.state, "close_streams", None)
                if close_streams is not None:
                    close_streams()
                super().handle_exit(sig, frame)

        uvicorn_config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            workers=1,
            timeout_graceful_shutdown=10,
            log_level=args.log_level.lower(),
        )
        _GracefulServer(uvicorn_config).run()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

"""Scoped CLI logging; no configuration or dependency on either core project."""

import argparse
import logging
import os
import sys
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from yt_workflows.logfiles import (
    CONTEXT_FIELDS as CONTEXT_FIELDS,
)
from yt_workflows.logfiles import (
    DEFAULT_BACKUPS,
    DEFAULT_MAX_BYTES,
    LockedRotatingFileHandler,
    SafeExternalHandler,
    validate_log_path,
)
from yt_workflows.logfiles import (
    LogFormatter as LogFormatter,
)
from yt_workflows.logfiles import (
    redact as redact,
)

NAMESPACES = ("yt_cli", "yt_workflows", "transcript_collector", "video_search")


class RunContext(logging.Filter):
    """Attach one invocation's identity to every record handled by its sinks."""

    def __init__(self, command: str):
        super().__init__()
        self.fields = {"run_id": uuid4().hex, "command": command, "pid": os.getpid()}

    def filter(self, record: logging.LogRecord) -> bool:
        record.__dict__.update(self.fields)
        return True


def add_logging_arguments(parser: argparse.ArgumentParser, *, inherited=False) -> None:
    parser.add_argument(
        "--log-file",
        default=argparse.SUPPRESS if inherited else "data/logs/cli.jsonl",
        help="rotating detailed JSONL log (default: data/logs/cli.jsonl)",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=argparse.SUPPRESS if inherited else "INFO",
        help="stderr log level; file always includes DEBUG (default: INFO)",
    )


def validate_logging_arguments(args, parser):
    protected = [getattr(args, name, None) for name in ("db", "search_db", "cookies")]
    try:
        validate_log_path(args.log_file, protected)
    except ValueError as exc:
        parser.error(str(exc))


@contextmanager
def command_logging(
    command: str,
    log_file: str | None = "data/logs/cli.jsonl",
    log_level: str | None = "INFO",
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUPS,
    handlers: Sequence[logging.Handler] | None = None,
    replace_defaults: bool = False,
):
    """Attach owned and caller-supplied handlers temporarily for one invocation."""
    owned: list[logging.Handler] = []
    attached: list[logging.Handler] = []
    file_path: Path | None = None
    run_context = RunContext(command)
    try:
        if not replace_defaults:
            if log_file is not None:
                file_path = Path(log_file).expanduser().resolve()
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = LockedRotatingFileHandler(
                    file_path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
                file_handler.setLevel(logging.DEBUG)
                file_handler.setFormatter(LogFormatter(json_lines=True))
                owned.append(file_handler)
            if log_level is not None:
                console = logging.StreamHandler(sys.stderr)
                console.setLevel(log_level)
                console.setFormatter(LogFormatter(json_lines=False))
                owned.append(console)

        attached.extend(owned)
        if handlers:
            attached.extend(SafeExternalHandler(target) for target in handlers)
        for handler in attached:
            handler.addFilter(run_context)
    except BaseException:
        for handler in owned:
            handler.close()
        raise

    saved = []
    for name in NAMESPACES:
        logger = logging.getLogger(name)
        saved.append((logger, logger.level, logger.propagate))
        logger.setLevel(logging.DEBUG)
        for handler in attached:
            logger.addHandler(handler)
        # Keep existing handlers and parent/root handlers available to embedders.
    logger = logging.getLogger("yt_cli.commands")
    start = time.perf_counter()
    try:
        if file_path is not None:
            logger.info(
                "%s started; detailed log: %s",
                command,
                file_path,
                extra={"phase": "command_started"},
            )
        else:
            logger.info(
                "%s started",
                command,
                extra={"phase": "command_started"},
            )
        yield
    except BaseException as exc:
        logger.log(
            logging.WARNING if isinstance(exc, KeyboardInterrupt) else logging.ERROR,
            "%s stopped (%s)",
            command,
            type(exc).__name__,
            extra={
                "phase": "command_failed",
                "elapsed_seconds": time.perf_counter() - start,
            },
        )
        raise
    else:
        logger.info(
            "%s completed",
            command,
            extra={
                "phase": "command_completed",
                "elapsed_seconds": time.perf_counter() - start,
            },
        )
    finally:
        for logger, level, propagate in saved:
            for handler in attached:
                logger.removeHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
        for handler in owned:
            handler.close()

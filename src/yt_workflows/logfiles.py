"""Bounded rotating files, handler specs, and redaction shared by applications."""

import copy
import json
import logging
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal, cast

from .paths import aliases, protected_variants

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS = ("text", "jsonl")
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 3
CONTEXT_FIELDS = (
    "run_id",
    "command",
    "pid",
    "worker_pid",
    "phase",
    "job_id",
    "request_id",
    "video_id",
    "model",
    "elapsed_seconds",
    "completed",
    "total",
)
_STANDARD_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1000,
    "kib": 1024,
    "m": 1024 * 1024,
    "mb": 1000 * 1000,
    "mib": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1000 * 1000 * 1000,
    "gib": 1024 * 1024 * 1024,
}


def redact(value: object) -> str:
    """Do not persist credentials, signed download URLs, or exception payloads."""
    text = str(value)
    text = re.sub(
        r"(?i)\b(cookies?|authorization)\b\s*[:=]\s*[^\r\n]*",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"https?://[^\s<>\"']+", "[URL redacted]", text)
    return re.sub(
        r"(?i)\b(cookies?|authorization|token|access_token|secret|api[_-]?key|password|signature)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1=[redacted]",
        text,
    )


class LogFormatter(logging.Formatter):
    """Structured text or JSONL formatter that redacts sensitive fields."""

    def __init__(self, *, json_lines: bool):
        super().__init__()
        self.json_lines = json_lines

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, str | int | float] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for name in CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                fields[name] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            # Exception text can contain transcript/query bodies. Keep only its type.
            error_type = record.exc_info[0]
            fields["error_type"] = error_type.__name__ if error_type else "Exception"
        if self.json_lines:
            return json.dumps(fields, ensure_ascii=False)
        context = " ".join(
            f"{key}={fields[key]}" for key in CONTEXT_FIELDS if key in fields
        )
        return (
            f"{fields['timestamp']} {fields['level']} {fields['logger']} "
            f"{fields['message']}" + (f" [{context}]" if context else "")
        )


@dataclass(frozen=True)
class HandlerSpec:
    """Parsed handler specification from CLI/web options."""

    kind: Literal["null", "stream", "file"]
    stream: Literal["stderr", "stdout"] | None = None
    path: Path | None = None
    level: str = "INFO"
    fmt: Literal["text", "jsonl"] = "text"
    max_bytes: int = DEFAULT_MAX_BYTES
    backups: int = DEFAULT_BACKUPS


def parse_byte_size(raw: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]*)\s*", raw)
    if not match:
        raise ValueError(f"Invalid byte size in log handler: {raw!r}")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"Invalid byte size unit in log handler: {raw!r}")
    size = amount * _SIZE_UNITS[unit]
    if size <= 0:
        raise ValueError("Log max_bytes must be greater than zero.")
    return size


def _parse_stream_spec(
    kind: Literal["stderr", "stdout"], options: list[str]
) -> HandlerSpec:
    level = "INFO"
    fmt: Literal["text", "jsonl"] = "text"
    remaining = list(options)
    if remaining and remaining[0].upper() in LOG_LEVELS:
        level = remaining.pop(0).upper()
    if remaining and remaining[0].lower() in LOG_FORMATS:
        fmt = cast(Literal["text", "jsonl"], remaining.pop(0).lower())
    if remaining:
        raise ValueError(f"Invalid stream log handler option: {remaining[0]!r}")
    return HandlerSpec(kind="stream", stream=kind, level=level, fmt=fmt)


def _parse_file_spec(rest: str) -> HandlerSpec:
    if not rest.strip():
        raise ValueError("Log file path cannot be empty.")
    parts = rest.split(":")
    if (
        len(parts) >= 2
        and len(parts[0]) == 1
        and parts[0].isalpha()
        and parts[1].startswith(("/", "\\"))
    ):
        parts = [f"{parts[0]}:{parts[1]}", *parts[2:]]
    raw_path = parts[0].strip()
    if not raw_path:
        raise ValueError("Log file path cannot be empty.")
    remaining = parts[1:]
    level = "DEBUG"
    fmt: Literal["text", "jsonl"] = "jsonl"
    max_bytes = DEFAULT_MAX_BYTES
    backups = DEFAULT_BACKUPS
    if remaining and remaining[0].upper() in LOG_LEVELS:
        level = remaining.pop(0).upper()
    if remaining and remaining[0].lower() in LOG_FORMATS:
        fmt = cast(Literal["text", "jsonl"], remaining.pop(0).lower())
    if remaining:
        max_bytes = parse_byte_size(remaining.pop(0))
    if remaining:
        raw_backups = remaining.pop(0)
        try:
            backups = int(raw_backups)
        except ValueError as exc:
            raise ValueError(
                f"Invalid backup count in log handler: {raw_backups!r}"
            ) from exc
        if backups < 0:
            raise ValueError("Log backup count cannot be negative.")
    if remaining:
        raise ValueError(f"Unsupported file log handler option: {remaining[0]!r}")
    return HandlerSpec(
        kind="file",
        path=Path(raw_path),
        level=level,
        fmt=fmt,
        max_bytes=max_bytes,
        backups=backups,
    )


def parse_handler_spec(spec: str | HandlerSpec) -> HandlerSpec:
    """Parse a compact handler string: null | stderr|stdout[:...] | file:PATH[:...]."""
    if isinstance(spec, HandlerSpec):
        return spec
    text = spec.strip()
    if not text:
        raise ValueError("Log handler specification cannot be empty.")
    lowered = text.lower()
    if lowered in {"null", "none"}:
        return HandlerSpec(kind="null")
    if lowered.startswith("file:"):
        return _parse_file_spec(text[5:])
    parts = text.split(":")
    head = parts[0].lower()
    if head in {"stderr", "console"}:
        return _parse_stream_spec("stderr", parts[1:])
    if head == "stdout":
        return _parse_stream_spec("stdout", parts[1:])
    raise ValueError(
        f"Unknown log handler {spec!r}. Expected null, stderr, stdout, or file:PATH."
    )


def validate_log_path(
    log_file: str | Path,
    protected_paths: Iterable[str | Path | None],
    *,
    backup_count: int = DEFAULT_BACKUPS,
) -> None:
    """Guard every file this handler can open, before creating directories or files."""
    if not str(log_file).strip():
        raise ValueError("Log file path cannot be empty.")
    path = Path(log_file).expanduser().resolve()
    writable_paths = [
        path,
        Path(str(path) + ".lock"),
        *(Path(str(path) + f".{number}") for number in range(1, backup_count + 1)),
    ]
    protected = {
        variant
        for item in protected_paths
        if item is not None
        for variant in protected_variants(item)
    }
    for candidate in writable_paths:
        target = candidate.resolve()
        if any(aliases(source, target) for source in protected):
            raise ValueError(
                "Log files must differ from every input, cache, report and SQLite sidecar path."
            )


class LockedRotatingFileHandler(RotatingFileHandler):
    """Serialize rollover/write and reopen the active pathname after every lock."""

    def __init__(self, filename, **kwargs):
        super().__init__(Path(filename).expanduser().resolve(), delay=True, **kwargs)
        self.lock_path = Path(self.baseFilename + ".lock")

    def emit(self, record):
        if fcntl is None:
            super().emit(record)
            return
        try:
            with self.lock_path.open("a") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
                    super().emit(record)
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            self.handleError(record)


def build_handler_from_spec(
    spec: str | HandlerSpec, *, locked_file: bool = True
) -> logging.Handler | None:
    """Create a configured stdlib handler from a HandlerSpec, or None for 'null'."""
    parsed = parse_handler_spec(spec)
    if parsed.kind == "null":
        return None
    formatter = LogFormatter(json_lines=(parsed.fmt == "jsonl"))
    if parsed.kind == "stream":
        stream = sys.stdout if parsed.stream == "stdout" else sys.stderr
        handler = logging.StreamHandler(stream)
        handler.setLevel(parsed.level)
        handler.setFormatter(formatter)
        return handler
    assert parsed.path is not None
    path = parsed.path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if locked_file:
        handler = LockedRotatingFileHandler(
            path,
            maxBytes=parsed.max_bytes,
            backupCount=parsed.backups,
            encoding="utf-8",
        )
    else:
        handler = RotatingFileHandler(
            path,
            maxBytes=parsed.max_bytes,
            backupCount=parsed.backups,
            encoding="utf-8",
            delay=True,
        )
    handler.setLevel(parsed.level)
    handler.setFormatter(formatter)
    return handler


class SafeExternalHandler(logging.Handler):
    """Forward sanitized LogRecords to a caller-provided handler without closing it."""

    def __init__(self, target: logging.Handler):
        super().__init__(logging.NOTSET)
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.target.level:
            return
        try:
            sanitized = copy.copy(record)
            sanitized.msg = redact(record.getMessage())
            sanitized.args = ()
            for key, value in list(sanitized.__dict__.items()):
                if key not in _STANDARD_RECORD_FIELDS and isinstance(value, str):
                    sanitized.__dict__[key] = redact(value)
            if record.exc_info:
                error_type = record.exc_info[0]
                type_name = error_type.__name__ if error_type else "Exception"
                sanitized.exc_text = None
                if error_type is not None:
                    try:
                        safe_exc: BaseException = error_type(type_name)
                        safe_type = error_type
                    except Exception:
                        safe_exc = RuntimeError(type_name)
                        safe_type = RuntimeError
                    sanitized.exc_info = (safe_type, safe_exc, record.exc_info[2])
                else:
                    sanitized.exc_info = None
            self.target.handle(sanitized)
        except Exception:
            self.handleError(record)

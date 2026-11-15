"""Application-owned rotating diagnostics; core libraries only emit LogRecords."""

import asyncio
import json
import logging
import re
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from traceback import extract_tb
from typing import Any

from yt_workflows.logfiles import SafeExternalHandler

from .config import WebConfig

_context: ContextVar[dict[str, str]] = ContextVar("yt_web_log_context", default={})
_standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_secret_keys = {
    "authorization",
    "cookie",
    "cookies",
    "password",
    "secret",
    "token",
    "access_token",
    "api_key",
    "query",
    "text",
    "transcript",
    "embedding",
}
_url = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_headers = re.compile(r"\b(authorization|cookies?)\b\s*[:=]\s*[^\r\n]*", re.IGNORECASE)
_credentials = re.compile(
    r"\b(authorization|cookies?|password|secret|token|access_token|api_key)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _credentials.sub(
            r"\1=[REDACTED]", _headers.sub(r"\1=[REDACTED]", _url.sub("[URL]", value))
        )[:32000]
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).lower() in _secret_keys
            else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value[:100]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return sanitize(str(value))


@contextmanager
def log_context(**values: str):
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, str]:
    return dict(_context.get())


def record_payload(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
            timespec="milliseconds"
        ),
        "level": record.levelname,
        "logger": record.name,
        "message": sanitize(record.getMessage()),
        "pid": record.process,
    }
    for key, value in record.__dict__.items():
        if key not in _standard and not key.startswith("_"):
            payload[key] = sanitize({key: value})[key]
    if record.exc_info:
        payload["error_type"] = (
            record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        )
        payload["traceback"] = sanitize(
            "".join(
                f"  File {frame.filename!r}, line {frame.lineno}, in {frame.name}\n"
                for frame in extract_tb(record.exc_info[2])
            )
            + payload["error_type"]
        )
    return payload


def relay_worker_log(payload: dict[str, Any]) -> None:
    fields = {
        key: value
        for key, value in payload.items()
        if key not in {"message", "logger", "level", "pid"}
    }
    level = logging.getLevelNamesMapping().get(
        str(payload.get("level", "INFO")), logging.INFO
    )
    record = logging.makeLogRecord(
        {
            **fields,
            "name": str(payload.get("logger", "yt_web.worker")),
            "msg": str(payload.get("message", "")),
            "args": (),
            "levelno": level,
            "levelname": logging.getLevelName(level),
            "process": payload.get("pid"),
        }
    )
    logging.getLogger("yt_web.worker").handle(record)


def _prepare_entry(
    payload: dict[str, Any], *, run_id: str, context: dict[str, str]
) -> dict[str, Any]:
    entry = sanitize(
        {
            **payload,
            **{key: value for key, value in context.items() if key != "store_id"},
        }
    )
    entry.update(id=uuid.uuid4().hex, run_id=run_id)
    entry.setdefault("timestamp", datetime.now(UTC).isoformat(timespec="milliseconds"))
    entry.setdefault("level", "INFO")
    entry.setdefault("logger", "yt_web.worker")
    message = json.dumps(entry, ensure_ascii=False, allow_nan=False)
    if len(message.encode("utf-8")) > 64 * 1024:
        entry = {
            key: value
            for key, value in entry.items()
            if key
            in {
                "id",
                "run_id",
                "timestamp",
                "level",
                "logger",
                "message",
                "phase",
                "job_id",
                "request_id",
                "video_id",
            }
        }
        entry["truncated_details"] = True
    return entry


def _filter_entries(
    records: list[dict[str, Any]],
    *,
    after: str | None = None,
    level: str = "INFO",
    job_id: str | None = None,
    limit: int = 200,
    file_path: Path | None = None,
) -> dict[str, Any]:
    reset = bool(after)
    if after:
        for index, record in enumerate(records):
            if record["id"] == after:
                records = records[index + 1 :]
                reset = False
                break
    cursor = records[-1]["id"] if records else after
    threshold = logging.getLevelNamesMapping().get(level, logging.INFO)
    selected = [
        record
        for record in records
        if logging.getLevelNamesMapping().get(record.get("level", "INFO"), logging.INFO)
        >= threshold
        and (not job_id or record.get("job_id") == job_id)
    ]
    return {
        "items": selected[-limit:],
        "next_cursor": cursor,
        "reset": reset,
        "truncated": len(selected) > limit,
        "file": str(file_path) if file_path is not None else None,
    }


def _enqueue_live(
    queue: asyncio.Queue[dict[str, Any] | None], item: dict[str, Any] | None
) -> None:
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with suppress(asyncio.QueueFull):
        queue.put_nowait(item)


class LogStreamHub:
    """In-memory ring buffer and thread-safe SSE fan-out for live web logs."""

    def __init__(self, *, max_history: int = 500):
        self._lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)
        self._subscribers: set[
            tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any] | None]]
        ] = set()
        self._closed = False

    def publish(self, entry: dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            self._history.append(entry)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_enqueue_live, queue, entry)

    def read(
        self,
        *,
        after: str | None = None,
        level: str = "INFO",
        job_id: str | None = None,
        limit: int = 200,
        file_path: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            records = list(self._history)
        return _filter_entries(
            records,
            after=after,
            level=level,
            job_id=job_id,
            limit=limit,
            file_path=file_path,
        )

    def subscribe(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        after: str | None = None,
        level: str = "INFO",
        job_id: str | None = None,
        limit: int = 200,
        file_path: Path | None = None,
    ) -> tuple[dict[str, Any], asyncio.Queue[dict[str, Any] | None]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1000)
        with self._lock:
            if self._closed:
                queue.put_nowait(None)
            else:
                self._subscribers.add((loop, queue))
            records = list(self._history)
        initial = _filter_entries(
            records,
            after=after,
            level=level,
            job_id=job_id,
            limit=limit,
            file_path=file_path,
        )
        return initial, queue

    def unsubscribe(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        with self._lock:
            self._subscribers.discard((loop, queue))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for loop, queue in subscribers:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_enqueue_live, queue, None)


class LogStore(logging.Handler):
    """One owner writes JSONL; the HTTP viewer reads a bounded retained tail."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backups: int = 3,
        console_level: str | None = None,
        on_entry: Callable[[dict[str, Any]], None] | None = None,
    ):
        super().__init__(logging.DEBUG)
        self.path = path.expanduser().resolve()
        self.identifier = uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.writer = RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
            delay=True,
        )
        self.writer.setFormatter(logging.Formatter("%(message)s"))
        self.backups = backups
        self.previous: tuple[int, bool] | None = None
        self.failed = False
        self.on_entry = on_entry
        self.console = logging.StreamHandler(sys.stderr) if console_level else None
        if self.console is not None:
            self.console.setLevel(console_level or "INFO")
            self.console.setFormatter(logging.Formatter("%(message)s"))

    def attach(self) -> None:
        logger = logging.getLogger("yt_web")
        owner = next(
            (
                handler
                for handler in logger.handlers
                if getattr(handler, "_yt_web_scoped", False)
                or isinstance(handler, LogStore)
            ),
            None,
        )
        self.previous = (
            getattr(owner, "previous", None)
            if owner is not None
            else (logger.level, logger.propagate)
        )
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        context = current_context()
        if context.get("store_id") != self.identifier:
            return
        self.publish(record_payload(record))

    def publish(self, payload: dict[str, Any]) -> None:
        """Worker records arrive separately from progress and never change counters."""
        published: dict[str, Any] | None = None
        self.acquire()
        try:
            entry = _prepare_entry(
                payload, run_id=self.identifier, context=current_context()
            )
            message = json.dumps(entry, ensure_ascii=False, allow_nan=False)
            self.writer.emit(
                logging.LogRecord("yt_web.file", logging.INFO, "", 0, message, (), None)
            )
            severity = logging.getLevelNamesMapping().get(
                str(entry["level"]), logging.INFO
            )
            if self.console is not None and severity >= self.console.level:
                labels = " ".join(
                    f"{key}={entry[key]}"
                    for key in ("phase", "job_id", "request_id", "video_id")
                    if entry.get(key)
                )
                text = f"{entry['timestamp']} {entry['level']} {entry['logger']} {entry.get('message', '')} {labels}".rstrip()
                self.console.emit(
                    logging.LogRecord("yt_web.console", severity, "", 0, text, (), None)
                )
            published = entry
        except Exception:
            # Diagnostics must not convert a committed operation into a failed job.
            if not self.failed:
                sys.stderr.write("YT Finder: không ghi được file nhật ký.\n")
                self.failed = True
        finally:
            self.release()
        if published is not None and self.on_entry is not None:
            self.on_entry(published)

    @staticmethod
    def _tail(path: Path, size: int = 512 * 1024) -> list[dict[str, Any]]:
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                offset = max(0, stream.tell() - size)
                stream.seek(offset)
                if offset:
                    stream.readline()
                lines = stream.read(size).splitlines()
        except FileNotFoundError:
            return []
        records = []
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    records.append(record)
            except ValueError:
                continue
        return records

    def read(
        self,
        *,
        after: str | None = None,
        level: str = "INFO",
        job_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.acquire()
        try:
            records: list[dict[str, Any]] = []
            for number in range(self.backups, -1, -1):
                records.extend(
                    self._tail(Path(f"{self.path}.{number}") if number else self.path)
                )
        finally:
            self.release()
        return _filter_entries(
            records,
            after=after,
            level=level,
            job_id=job_id,
            limit=limit,
            file_path=self.path,
        )

    def snapshot(self) -> bytes:
        """A download must not race appends/rotation or stream a changing inode."""
        self.acquire()
        try:
            if self.writer.stream is not None:
                self.writer.flush()
            with self.path.open("rb") as stream:
                # One record may exceed the rotation threshold, so permit that
                # record's bounded diagnostic payload without reading unbounded files.
                data = stream.read(self.writer.maxBytes + 512 * 1024)
                # Do not return a partial JSON line if an old/external file exceeds the bound.
                return data[: data.rfind(b"\n") + 1]
        finally:
            self.release()

    def close(self) -> None:
        if self.previous is not None:
            logger = logging.getLogger("yt_web")
            logger.removeHandler(self)
            # Other live injected apps may still own this namespace.
            if not any(
                getattr(handler, "_yt_web_scoped", False)
                or isinstance(handler, LogStore)
                for handler in logger.handlers
            ):
                logger.setLevel(self.previous[0])
                logger.propagate = self.previous[1]
            self.previous = None
        self.writer.close()
        if self.console is not None:
            self.console.close()
        super().close()


class _StoreContextFilter(logging.Filter):
    """Restrict attached web handlers to records emitted in this app's context."""

    def __init__(self, store_id: str):
        super().__init__()
        self.store_id = store_id

    def filter(self, record: logging.LogRecord) -> bool:
        return current_context().get("store_id") == self.store_id


class _StreamBridgeHandler(logging.Handler):
    """Push sanitized LogRecords into LogStreamHub when LogStore is replaced."""

    def __init__(self, hub: LogStreamHub, run_id: str):
        super().__init__(logging.DEBUG)
        self.hub = hub
        self.run_id = run_id

    def emit(self, record: logging.LogRecord) -> None:
        entry = _prepare_entry(
            record_payload(record), run_id=self.run_id, context=current_context()
        )
        self.hub.publish(entry)


class WebLogSession:
    """Manage default LogStore, custom handlers, and live SSE streaming."""

    def __init__(
        self,
        config: WebConfig,
        *,
        handlers: Sequence[logging.Handler] | None = None,
        replace_defaults: bool = False,
    ):
        self.hub = LogStreamHub()
        use_default_store = not replace_defaults
        self.store: LogStore | None = (
            LogStore(
                config.log_path,
                console_level=config.log_level,
                on_entry=self.hub.publish,
            )
            if use_default_store
            else None
        )
        self.identifier = (
            self.store.identifier if self.store is not None else uuid.uuid4().hex
        )
        self.file_path: Path | None = (
            self.store.path if self.store is not None else None
        )
        self._owned: list[logging.Handler] = []
        self._extra: list[logging.Handler] = []
        self._previous: tuple[int, bool] | None = None
        scope = _StoreContextFilter(self.identifier)
        try:
            if self.store is None:
                bridge = _StreamBridgeHandler(self.hub, self.identifier)
                bridge.addFilter(scope)
                setattr(bridge, "_yt_web_scoped", True)
                setattr(bridge, "previous", None)
                self._owned.append(bridge)
                self._extra.append(bridge)
            if handlers:
                for target in handlers:
                    wrapped = SafeExternalHandler(target)
                    wrapped.addFilter(scope)
                    setattr(wrapped, "_yt_web_scoped", True)
                    setattr(wrapped, "previous", None)
                    self._extra.append(wrapped)
        except BaseException:
            self.hub.close()
            for handler in self._owned:
                handler.close()
            if self.store is not None:
                self.store.close()
            raise

    def read(
        self,
        *,
        after: str | None = None,
        level: str = "INFO",
        job_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if self.store is not None:
            return self.store.read(after=after, level=level, job_id=job_id, limit=limit)
        return self.hub.read(
            after=after,
            level=level,
            job_id=job_id,
            limit=limit,
            file_path=self.file_path,
        )

    def subscribe(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        after: str | None = None,
        level: str = "INFO",
        job_id: str | None = None,
        limit: int = 200,
    ) -> tuple[dict[str, Any], asyncio.Queue[dict[str, Any] | None]]:
        initial, queue = self.hub.subscribe(
            loop,
            after=after,
            level=level,
            job_id=job_id,
            limit=limit,
            file_path=self.file_path,
        )
        if self.store is not None and (not after or initial["reset"]):
            disk = self.store.read(after=after, level=level, job_id=job_id, limit=limit)
            seen = {item["id"] for item in disk["items"]}
            merged = list(disk["items"]) + [
                item for item in initial["items"] if item["id"] not in seen
            ]
            initial = {
                "items": merged[-limit:],
                "next_cursor": merged[-1]["id"] if merged else disk["next_cursor"],
                "reset": disk["reset"],
                "truncated": disk["truncated"] or len(merged) > limit,
                "file": disk["file"],
            }
        return initial, queue

    def attach(self) -> None:
        logger = logging.getLogger("yt_web")
        owner = next(
            (
                handler
                for handler in logger.handlers
                if getattr(handler, "_yt_web_scoped", False)
                or isinstance(handler, LogStore)
            ),
            None,
        )
        self._previous = (
            getattr(owner, "previous", None)
            if owner is not None
            else (logger.level, logger.propagate)
        )
        if self.store is not None:
            self.store.attach()
        logger.setLevel(logging.DEBUG)
        for handler in self._extra:
            setattr(handler, "previous", self._previous)
            logger.addHandler(handler)

    def close(self) -> None:
        self.hub.close()
        if self.store is not None:
            self.store.close()
        logger = logging.getLogger("yt_web")
        for handler in self._extra:
            logger.removeHandler(handler)
        if self._previous is not None:
            if not any(
                getattr(handler, "_yt_web_scoped", False)
                or isinstance(handler, LogStore)
                for handler in logger.handlers
            ):
                logger.setLevel(self._previous[0])
                logger.propagate = self._previous[1]
            self._previous = None
        for handler in self._owned:
            handler.close()

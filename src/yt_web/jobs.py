"""Durable local collection queue; job state never touches transcript SQLite."""

import json
import logging
import sqlite3
import threading
import uuid
from _thread import RLock
from collections.abc import Callable
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import IO, Any, Protocol

from .config import WebConfig
from .gateways import CollectorProcessGateway
from .logs import log_context
from .processes import WorkerCancelled

logger = logging.getLogger(__name__)

STATE_COLUMNS = {
    "jobs": (
        "id",
        "status",
        "target",
        "request",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "progress",
        "result",
        "error",
    ),
    "events": ("id", "job_id", "kind", "payload", "created_at"),
}

TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


def now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if tables or version:
                if (
                    version != 1
                    or tables != set(STATE_COLUMNS)
                    or any(
                        tuple(
                            row[1] for row in db.execute(f"PRAGMA table_info({table})")
                        )
                        != columns
                        for table, columns in STATE_COLUMNS.items()
                    )
                ):
                    raise ValueError(
                        "Web state database không tương thích; chọn --state-db khác."
                    )
            else:
                db.executescript("""
                    BEGIN;
                    CREATE TABLE jobs(
                        id TEXT PRIMARY KEY,status TEXT NOT NULL,target TEXT NOT NULL,request TEXT NOT NULL,
                        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,
                        progress TEXT NOT NULL,result TEXT,error TEXT);
                    CREATE TABLE events(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(id),
                        kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
                    CREATE INDEX events_job ON events(job_id,id);
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            db.execute("PRAGMA journal_mode=WAL")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("request", "progress", "result"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        identifier, timestamp = uuid.uuid4().hex, now()
        progress = {"position": 0, "total": 0, "title": "", "counts": {}}
        with closing(self.connect()) as db, db:
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                >= 64
            ):
                raise ValueError(
                    "Hàng đợi đã đầy. Hãy đợi các tác vụ hiện tại hoàn tất."
                )
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL,NULL,?,NULL,NULL)",
                (
                    identifier,
                    "queued",
                    request.get("target") or "",
                    json.dumps(request),
                    timestamp,
                    timestamp,
                    json.dumps(progress),
                ),
            )
        return self.get(identifier)

    def get(self, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return self.decode(row)

    def list(self, limit: int = 50, *, kind: str | None = None) -> list[dict[str, Any]]:
        condition = ""
        if kind == "collect":
            condition = (
                " WHERE coalesce(json_extract(request,'$.kind'),'collect') = 'collect'"
            )
        with closing(self.connect()) as db:
            return [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM jobs"
                    + condition
                    + " ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            ]

    def recover(self) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE jobs SET status='interrupted',error=?,finished_at=?,updated_at=? WHERE status='running'",
                (
                    "Ứng dụng đã dừng trước khi tác vụ hoàn tất. Có thể chạy lại để dùng cache đã lưu.",
                    now(),
                    now(),
                ),
            )

    def next(self) -> str | None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status='running',started_at=?,updated_at=? WHERE id=?",
                (now(), now(), row[0]),
            )
            return row[0]

    def finish(
        self,
        identifier: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE jobs SET status=?,result=?,error=?,finished_at=?,updated_at=? WHERE id=?",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    now(),
                    now(),
                    identifier,
                ),
            )

    def event(self, identifier: str, payload: dict[str, Any]) -> None:
        with closing(self.connect()) as db, db:
            progress = json.loads(
                db.execute(
                    "SELECT progress FROM jobs WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
            kind = payload.get("kind", "message")
            if kind == "video_started":
                progress.update(
                    position=payload["position"],
                    total=payload["total"],
                    title=payload["title"],
                )
            if kind == "video_finished":
                progress.update(
                    position=payload["position"],
                    total=payload["total"],
                    title=payload.get("title") or payload.get("video_id", ""),
                )
                for key, value in (payload.get("counts") or {}).items():
                    progress["counts"][key] = progress["counts"].get(key, 0) + value
            if kind == "summary":
                progress["counts"] = payload.get("counts") or {}
            db.execute(
                "INSERT INTO events(job_id,kind,payload,created_at) VALUES(?,?,?,?)",
                (identifier, kind, json.dumps(payload, ensure_ascii=False), now()),
            )
            db.execute(
                "UPDATE jobs SET progress=?,updated_at=? WHERE id=?",
                (json.dumps(progress, ensure_ascii=False), now(), identifier),
            )

    def events(self, identifier: str, after: int = 0) -> dict[str, Any]:
        self.get(identifier)
        with closing(self.connect()) as db:
            items = [
                {**dict(row), "payload": json.loads(row["payload"])}
                for row in db.execute(
                    "SELECT id,kind,payload,created_at FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT 200",
                    (identifier, after),
                )
            ]
        return {"items": items, "next_cursor": items[-1]["id"] if items else after}


class ModelOwner(Protocol):
    model_lock: RLock

    def close(self) -> None: ...


class JobManager:
    def __init__(
        self,
        config: WebConfig,
        search: ModelOwner,
        *,
        collector_factory: Callable[[], Any] | None = None,
    ):
        self.store = JobStore(config.state_db)
        self.search = search
        self.collector_factory = collector_factory or (
            lambda: CollectorProcessGateway(config)
        )
        self.log_store_id = ""
        self._wake, self._stopping = threading.Event(), threading.Event()
        self._state_lock = threading.Lock()
        self._active: tuple[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._ownership: IO[bytes] | None = None

    def start(self) -> None:
        if self._ownership is not None:
            raise ValueError("Hàng đợi đã được khởi động.")
        import fcntl

        # Lock the database inode itself so symlink/hardlink aliases share ownership.
        # SQLite uses byte-range locks, independent of this application flock.
        lock = self.store.path.open("rb")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise ValueError(
                "Một ứng dụng web khác đang sử dụng state database này."
            ) from exc
        self._ownership = lock
        try:
            self.store.recover()
        except BaseException:
            self._ownership.close()
            self._ownership = None
            raise
        self._thread = threading.Thread(
            target=self._run, name="yt-web-collections", daemon=True
        )
        self._thread.start()
        self._wake.set()
        logger.info("Collection queue started", extra={"phase": "queue_started"})

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        job = self.store.create(request)
        logger.info(
            "Job queued",
            extra={
                "phase": "job_queued",
                "job_id": job["id"],
                "kind": request.get("kind", "collect"),
            },
        )
        self._wake.set()
        return job

    def cancel(self, identifier: str) -> dict[str, Any]:
        with self._state_lock:
            job = self.store.get(identifier)
            logger.info(
                "Cancellation requested",
                extra={
                    "phase": "job_cancel_requested",
                    "job_id": identifier,
                    "status": job["status"],
                },
            )
            if job["status"] == "queued":
                self.store.finish(identifier, "cancelled")
            elif job["status"] == "running":
                if self._active is not None and self._active[0] == identifier:
                    self._active[1].cancel()
                else:
                    self.store.finish(identifier, "cancelled")
        self._wake.set()
        return self.store.get(identifier)

    def _run(self) -> None:
        while not self._stopping.is_set():
            identifier, gateway = None, None
            try:
                with self._state_lock:
                    identifier = self.store.next()
                    gateway = self.collector_factory() if identifier else None
                    self._active = (identifier, gateway) if identifier else None
            except Exception as exc:
                with log_context(store_id=self.log_store_id, job_id=identifier or ""):
                    logger.exception(
                        "Cannot start job", extra={"phase": "job_factory_failed"}
                    )
                    if identifier is not None:
                        self._finish_safely(identifier, "failed", error=str(exc))
                self._stopping.wait(1)
                continue
            if identifier is None or gateway is None:
                self._wake.wait(1)
                self._wake.clear()
                continue
            with log_context(
                store_id=self.log_store_id, job_id=identifier, request_id=identifier
            ):
                try:
                    self._execute(identifier, gateway)
                finally:
                    with self._state_lock:
                        self._active = None

    def _execute(self, identifier: str, gateway: Any) -> None:
        started = perf_counter()
        try:
            request = self.store.get(identifier)["request"]
            logger.info(
                "Job started",
                extra={
                    "phase": "job_started",
                    "kind": request.get("kind", "collect"),
                    "mode": request.get("mode"),
                },
            )
            context = (
                self._generation_slot(gateway)
                if request.get("mode") == "generate"
                else nullcontext()
            )
            with context:
                if request.get("mode") == "generate":
                    logger.info(
                        "Closing search session before model work",
                        extra={"phase": "model_handoff"},
                    )
                    self.search.close()
                result = gateway.collect(
                    request, on_event=lambda event: self.store.event(identifier, event)
                )
            if gateway.cancelled.is_set():
                self._finish_safely(identifier, "cancelled", result=result)
            else:
                counts = result.get("counts", {})
                status = (
                    "partial"
                    if counts.get("errors") or counts.get("blocked")
                    else "completed"
                )
                self._finish_safely(identifier, status, result=result)
                logger.info(
                    "Job finished",
                    extra={
                        "phase": "job_finished",
                        "status": status,
                        "counts": counts,
                        "elapsed_seconds": perf_counter() - started,
                    },
                )
        except WorkerCancelled:
            self._finish_safely(identifier, "cancelled")
            logger.info(
                "Job cancelled",
                extra={
                    "phase": "job_cancelled",
                    "elapsed_seconds": perf_counter() - started,
                },
            )
        except Exception as exc:
            cancelled = gateway.cancelled.is_set()
            self._finish_safely(
                identifier,
                "cancelled" if cancelled else "failed",
                error=None if cancelled else str(exc),
            )
            logger.exception(
                "Job stopped (%s)",
                type(exc).__name__,
                extra={
                    "phase": "job_failed",
                    "elapsed_seconds": perf_counter() - started,
                },
            )

    @contextmanager
    def _generation_slot(self, gateway: Any):
        logger.info("Waiting for model ownership", extra={"phase": "model_lock_wait"})
        while not self.search.model_lock.acquire(timeout=0.1):
            if gateway.cancelled.is_set() or self._stopping.is_set():
                raise WorkerCancelled("Đã hủy tác vụ trước khi chạy model.")
        logger.info("Model ownership acquired", extra={"phase": "model_lock_acquired"})
        try:
            if gateway.cancelled.is_set() or self._stopping.is_set():
                raise WorkerCancelled("Đã hủy tác vụ trước khi chạy model.")
            yield
        finally:
            self.search.model_lock.release()
            logger.debug(
                "Model ownership released", extra={"phase": "model_lock_released"}
            )

    def _finish_safely(self, identifier: str, status: str, **fields: Any) -> None:
        try:
            self.store.finish(identifier, status, **fields)
            logger.debug(
                "Job state committed",
                extra={
                    "phase": "job_state_committed",
                    "job_id": identifier,
                    "status": status,
                },
            )
        except Exception:
            # Preserve the queue thread during transient storage failures. A job
            # still marked running is recovered as interrupted on next startup.
            logger.exception("Cannot persist terminal state for job %s", identifier)

    def close(self) -> None:
        logger.info("Stopping job queue", extra={"phase": "queue_stopping"})
        self._stopping.set()
        with self._state_lock:
            if self._active is not None:
                self._active[1].cancel()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                with self._state_lock:
                    if self._active is not None:
                        self._active[1].force_stop()
                self._thread.join(timeout=10)
        if self._ownership is not None and (
            self._thread is None or not self._thread.is_alive()
        ):
            self._ownership.close()
            self._ownership = None

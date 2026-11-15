"""HTTP/UI adapter over read-only library views and isolated service workers."""

import asyncio
import json
import logging
import sqlite3
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import Library
from .config import WebConfig, database_files
from .gateways import SearchProcessGateway
from .jobs import JobManager
from .logs import LogStore, WebLogSession, log_context
from .processes import WorkerError

logger = logging.getLogger(__name__)
ModelName = Literal["minilm", "e5-small", "qwen3", "jina-v5", "embeddinggemma"]


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target: str = Field(min_length=1, max_length=4096)
    mode: Literal["direct", "generate"] = "direct"
    language: str | None = Field(
        default="vi", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
    )
    refresh: bool = False
    engine: Literal["whisper"] | None = None
    engine_model: str | None = Field(default=None, min_length=1, max_length=4096)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    cookies: str | None = Field(default=None, max_length=4096)
    db: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def mode_options(self) -> "CollectionInput":
        if self.mode == "direct":
            if not self.language:
                raise ValueError("Chế độ trực tiếp cần một mã ngôn ngữ.")
            if self.engine or self.engine_model:
                raise ValueError(
                    "Engine và model chỉ dùng khi tạo transcript từ audio."
                )
        elif self.language is not None:
            raise ValueError(
                "Tạo transcript từ audio tự nhận dạng ngôn ngữ; hãy bỏ language."
            )
        return self


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=10000)
    top: int = Field(default=5, ge=1)
    moments_per_video: int = Field(default=3, ge=1)
    language: str = Field(
        default="vi", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
    )
    channel: str | None = Field(default=None, max_length=500)
    video_ids: list[str] | None = None

    model: ModelName = "minilm"
    transcript_source: Literal["auto", "youtube", "generate"] = "auto"
    source_mode: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal["hybrid", "semantic", "keyword", "exact"] = "hybrid"
    rerank: bool | None = None
    min_similarity: float | None = Field(default=None, ge=-1, le=1)
    device: Literal["cpu", "cuda"] | None = None
    batch_size: int = Field(default=16, ge=1)
    offline: bool = False
    db: str | None = Field(default=None, max_length=4096)
    search_db: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def search_options(self) -> "SearchInput":
        if self.mode not in {"hybrid", "semantic"} and self.min_similarity is not None:
            raise ValueError(
                "Ngưỡng cosine chỉ dùng cho tìm kiếm kết hợp hoặc vector (ngữ nghĩa)."
            )
        if self.mode != "hybrid" and self.rerank is True:
            raise ValueError("Sắp xếp lại Cross-Encoder chỉ dùng cho chế độ kết hợp.")
        return self


def aliases(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def protect_paths(read_paths: list[Path], write_paths: list[Path]) -> None:
    protected = [candidate for path in read_paths for candidate in database_files(path)]
    for writable in write_paths:
        if any(aliases(writable, path) for path in protected):
            raise ValueError(
                "Đường dẫn đầu ra trùng dữ liệu đầu vào hoặc file đang được ứng dụng sử dụng."
            )
        protected.extend(database_files(writable))


def create_http_app(
    config: WebConfig | None = None,
    *,
    search_gateway: Any = None,
    collector_factory: Any = None,
    log_handlers: Sequence[logging.Handler] | None = None,
    replace_default_logs: bool = False,
) -> FastAPI:
    configuration = config or WebConfig.defaults()
    configuration.validate()
    library = Library(configuration.db)
    search = search_gateway or SearchProcessGateway(configuration)
    jobs: JobManager | None = None

    log_session: WebLogSession | None = None
    logs: LogStore | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal jobs, log_session, logs
        jobs = JobManager(
            configuration,
            search,
            collector_factory=collector_factory,
        )
        # The delayed file handler only writes once queue ownership is acquired.
        log_session = WebLogSession(
            configuration,
            handlers=log_handlers,
            replace_defaults=replace_default_logs,
        )
        logs = log_session.store
        jobs.log_store_id = log_session.identifier
        log_session.attach()
        started = False
        loop = asyncio.get_running_loop()

        def close_streams() -> None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(log_session.hub.close)

        app.state.close_streams = close_streams
        try:
            with log_context(store_id=log_session.identifier):
                jobs.start()
                started = True
                app.state.jobs = jobs
                app.state.logs = logs
                if log_session.file_path is not None:
                    logger.info(
                        "Web application started; detailed log: %s",
                        log_session.file_path,
                        extra={"phase": "application_started"},
                    )
                else:
                    logger.info(
                        "Web application started",
                        extra={"phase": "application_started"},
                    )
            yield
        finally:
            if started:
                with log_context(store_id=log_session.identifier):
                    await asyncio.to_thread(jobs.close)
                    await asyncio.to_thread(getattr(search, "shutdown", search.close))
                    logger.info(
                        "Web application stopped",
                        extra={"phase": "application_stopped"},
                    )
            log_session.close()

    app = FastAPI(
        title="YT Finder",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = urlparse(str(request.base_url))
            parsed = urlparse(origin) if origin else None
            if request.headers.get("x-yt-request") != "1" or (
                parsed is not None
                and (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc)
            ):
                return JSONResponse(
                    {"detail": "Yêu cầu phải đến từ ứng dụng trên máy này."},
                    status_code=403,
                )
        request_id = uuid.uuid4().hex
        with log_context(
            store_id=log_session.identifier if log_session else "",
            request_id=request_id,
        ):
            started = perf_counter()
            tracked = request.method == "POST"
            if tracked:
                logger.info(
                    "HTTP operation started",
                    extra={
                        "phase": "http_started",
                        "method": request.method,
                        "path": request.url.path,
                    },
                )
            try:
                response = await call_next(request)
            except Exception as exc:
                logger.exception(
                    "HTTP operation failed (%s)",
                    type(exc).__name__,
                    extra={
                        "phase": "http_failed",
                        "path": request.url.path,
                        "elapsed_seconds": perf_counter() - started,
                    },
                )
                raise
            if tracked:
                logger.info(
                    "HTTP operation completed",
                    extra={
                        "phase": "http_completed",
                        "status": response.status_code,
                        "elapsed_seconds": perf_counter() - started,
                    },
                )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://i.ytimg.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing_job(request: Request, error: KeyError):
        return JSONResponse({"detail": "Không tìm thấy tác vụ."}, status_code=404)

    @app.exception_handler(WorkerError)
    async def worker_failure(request: Request, error: WorkerError):
        return JSONResponse(
            {"detail": str(error)},
            status_code=400 if error.kind in {"ValueError", "TypeError"} else 503,
        )

    @app.exception_handler(ValueError)
    async def invalid_data(request: Request, error: ValueError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, error: sqlite3.Error):
        return JSONResponse(
            {"detail": f"Không đọc/ghi được dữ liệu: {error}"}, status_code=409
        )

    def manager() -> JobManager:
        if jobs is None:
            raise HTTPException(503, "Ứng dụng đang khởi động.")
        return jobs

    def resolved(value: str) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else configuration.root / path).resolve()

    def log_paths() -> list[Path]:
        return configuration.log_files

    def validate_runtime_paths(db: Path, cache: Path) -> None:
        WebConfig(
            configuration.root,
            db,
            cache,
            configuration.state_db,
            configuration.log_file,
            configuration.log_level,
        ).validate()

    @app.get("/api/logs")
    def read_logs(
        after: str | None = None,
        level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO",
        job_id: str | None = None,
        limit: int = Query(200, ge=1, le=500),
    ):
        if log_session is None:
            raise HTTPException(503, "Nhật ký chưa sẵn sàng.")
        return log_session.read(after=after, level=level, job_id=job_id, limit=limit)

    @app.get("/api/logs/stream")
    async def stream_logs(
        request: Request,
        after: str | None = None,
        level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO",
        job_id: str | None = None,
        limit: int = Query(200, ge=1, le=500),
    ):
        if log_session is None:
            raise HTTPException(503, "Nhật ký chưa sẵn sàng.")
        cursor = after or request.headers.get("last-event-id")
        loop = asyncio.get_running_loop()
        initial, queue = await asyncio.to_thread(
            log_session.subscribe,
            loop,
            after=cursor,
            level=level,
            job_id=job_id,
            limit=limit,
        )
        threshold = logging.getLevelNamesMapping().get(level, logging.INFO)
        active_session = log_session

        def format_sse(
            event_name: str, payload: Any, event_id: str | None = None
        ) -> str:
            lines = []
            if event_id:
                lines.append(f"id: {event_id}")
            lines.append(f"event: {event_name}")
            lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
            return "\n".join(lines) + "\n\n"

        async def event_stream():
            seen: set[str] = set()
            seen_order: deque[str] = deque()
            try:
                yield format_sse(
                    "meta",
                    {
                        "file": initial["file"],
                        "truncated": initial["truncated"],
                    },
                )
                if initial["reset"]:
                    yield format_sse("reset", {})
                for item in initial["items"]:
                    item_id = item["id"]
                    seen.add(item_id)
                    seen_order.append(item_id)
                    yield format_sse("log", item, item_id)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if entry is None:
                        break
                    entry_id = entry["id"]
                    if entry_id in seen:
                        continue
                    if len(seen_order) >= 1000:
                        seen.discard(seen_order.popleft())
                    seen.add(entry_id)
                    seen_order.append(entry_id)
                    severity = logging.getLevelNamesMapping().get(
                        str(entry.get("level", "INFO")), logging.INFO
                    )
                    if severity < threshold:
                        continue
                    if job_id and entry.get("job_id") != job_id:
                        continue
                    yield format_sse("log", entry, entry_id)
            finally:
                active_session.hub.unsubscribe(loop, queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/logs/download")
    def download_logs():
        if log_session is None:
            raise HTTPException(503, "Nhật ký chưa sẵn sàng.")
        if logs is None:
            raise HTTPException(404, "Nhật ký dạng Web chưa được bật.")
        try:
            snapshot = logs.snapshot()
        except FileNotFoundError:
            raise HTTPException(404, "Chưa có file nhật ký.")
        return Response(
            snapshot,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="web.jsonl"'},
        )

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "components": {
                "collector": configuration.interpreter(
                    "transcript_collector"
                ).is_file(),
                "search": configuration.interpreter("video_search").is_file(),
            },
        }

    @app.get("/api/library")
    def library_summary():
        return library.summary()

    @app.get("/api/videos")
    def library_videos(
        channel: str | None = None,
        q: str = "",
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=100),
    ):
        return library.videos(channel=channel, query=q, page=page, limit=limit)

    @app.post("/api/search")
    def search_transcripts(body: SearchInput):
        started = perf_counter()
        selected_db = resolved(body.db) if body.db else configuration.db
        selected_cache = (
            resolved(body.search_db) if body.search_db else configuration.search_db
        )
        validate_runtime_paths(selected_db, selected_cache)
        protect_paths(
            [configuration.db, configuration.state_db, *log_paths()], [selected_cache]
        )
        # Keep web requests responsive while a generation job owns the GPU.
        if not search.model_lock.acquire(blocking=False):
            raise HTTPException(
                409,
                "Mô hình đang được dùng cho tác vụ khác. Hãy thử lại sau khi tác vụ hoàn tất.",
            )
        try:
            options = body.model_dump(exclude={"query", "db", "search_db"})
            options.update(db=str(selected_db), search_db=str(selected_cache))
            logger.info(
                "Search requested",
                extra={
                    "phase": "search_requested",
                    "query_length": len(body.query),
                    "model": body.model,
                    "language": body.language,
                    "top": body.top,
                },
            )
            results = search.search_with_options(body.query, options)
            return {
                "query": body.query,
                "mode": body.mode,
                "rerank": body.mode == "hybrid" and body.rerank is not False,
                "moments_per_video": body.moments_per_video,
                "results": results,
                "elapsed_seconds": round(perf_counter() - started, 3),
            }
        finally:
            search.model_lock.release()

    @app.post("/api/collections", status_code=202)
    def start_collection(body: CollectionInput):
        request = body.model_dump(exclude_none=True)
        if body.mode == "generate":
            request["language"] = None
        selected_db = resolved(body.db) if body.db else configuration.db
        validate_runtime_paths(selected_db, configuration.search_db)
        if body.db:
            request["db"] = str(selected_db)
        if body.cookies:
            request["cookies"] = str(resolved(body.cookies))
            protect_paths([resolved(body.cookies)], [selected_db, *log_paths()])
        return manager().submit(request)

    @app.get("/api/collections")
    def list_collections():
        return {"items": manager().store.list(kind="collect")}

    @app.get("/api/collections/{identifier}")
    def get_collection(identifier: str):
        job = manager().store.get(identifier)
        if job["request"].get("kind") is not None:
            raise KeyError(identifier)
        return job

    @app.get("/api/collections/{identifier}/events")
    def collection_events(identifier: str, after: int = Query(0, ge=0)):
        get_collection(identifier)
        return manager().store.events(identifier, after)

    @app.post("/api/collections/{identifier}/cancel")
    def cancel_collection(identifier: str):
        get_collection(identifier)
        return manager().cancel(identifier)

    static = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(
            static / "index.html", headers={"Cache-Control": "no-cache"}
        )

    return app

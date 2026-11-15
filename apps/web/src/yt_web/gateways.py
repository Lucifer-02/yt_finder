"""Application gateways retain process lifetime and shared model coordination."""

import logging
import tempfile
import threading
from contextlib import nullcontext

from .config import WebConfig
from .ports import EventHandler, Payload
from .processes import ProcessWorker, WorkerCancelled, WorkerError

logger = logging.getLogger(__name__)


class CollectorProcessGateway:
    CANCEL_GRACE_SECONDS = 10

    def __init__(self, config: WebConfig):
        self.config = config
        self.cancelled = threading.Event()
        self._worker: ProcessWorker | None = None
        self._cancel_timer: threading.Timer | None = None

    def collect(
        self, request: Payload, *, on_event: EventHandler | None = None
    ) -> Payload:
        if self.cancelled.is_set():
            raise WorkerCancelled("Đã hủy tác vụ.")
        context = (
            tempfile.TemporaryDirectory(prefix="yt-web-audio-")
            if request.get("mode") == "generate"
            else nullcontext(None)
        )
        try:
            with context as directory:
                environment = {"TMPDIR": directory} if directory else None
                if directory:
                    logger.info(
                        "Temporary audio directory created",
                        extra={"phase": "audio_directory_created"},
                    )
                arguments = {**request, "db": str(request.get("db") or self.config.db)}
                return self._call(
                    "transcript_collector",
                    "yt_web.workers.collector",
                    "collect",
                    arguments,
                    on_event,
                    environment,
                )
        finally:
            if request.get("mode") == "generate":
                logger.info(
                    "Temporary audio directory cleaned",
                    extra={"phase": "audio_directory_cleaned"},
                )

    def _call(self, component, module, command, arguments, on_event, environment=None):
        worker = ProcessWorker(self.config, component, module, extra_env=environment)
        self._worker = worker
        try:
            if self.cancelled.is_set():
                raise WorkerCancelled("Đã hủy tác vụ.")

            def receive(event: Payload) -> None:
                if self.cancelled.is_set():
                    worker.interrupt()
                if on_event is not None:
                    on_event(event)

            return worker.call(command, arguments, on_event=receive)
        finally:
            if self._cancel_timer is not None:
                self._cancel_timer.cancel()
            worker.close()
            self._worker = None

    def cancel(self) -> None:
        self.cancelled.set()
        if self._worker is not None:
            self._worker.interrupt()
            timer = threading.Timer(self.CANCEL_GRACE_SECONDS, self._worker.force_stop)
            timer.daemon = True
            self._cancel_timer = timer
            timer.start()

    def force_stop(self) -> None:
        if self._worker is not None:
            self._worker.force_stop()


class SearchProcessGateway:
    def __init__(self, config: WebConfig):
        self.config = config
        self.model_lock = threading.RLock()
        self._worker = ProcessWorker(config, "video_search", "yt_web.workers.search")
        self._configuration: Payload | None = None

    def search(
        self,
        query: str,
        top: int = 5,
        min_similarity: float | None = None,
        *,
        moments_per_video: int = 3,
    ) -> list[Payload]:
        return self.search_with_options(
            query,
            {
                "top": top,
                "min_similarity": min_similarity,
                "moments_per_video": moments_per_video,
            },
        )

    def search_with_options(self, query: str, options: Payload) -> list[Payload]:
        settings = {
            key: value
            for key, value in options.items()
            if key not in {"top", "min_similarity", "moments_per_video"}
        }
        configuration = {
            "db": str(self.config.db),
            "search_db": str(self.config.search_db),
            **settings,
        }
        with self.model_lock:
            try:
                if self._configuration != configuration:
                    if self._configuration is not None:
                        self._worker.close()
                    logger.info(
                        "Configuring search worker",
                        extra={
                            "phase": "search_configure",
                            "model": configuration.get("model", "minilm"),
                        },
                    )
                    self._worker.call("configure", configuration)
                    self._configuration = configuration
                return self._worker.call(
                    "search",
                    {
                        "query": query,
                        **{
                            key: value
                            for key, value in options.items()
                            if key in {"top", "min_similarity", "moments_per_video"}
                        },
                    },
                )
            except WorkerError:
                self.close()
                raise

    def shutdown(self) -> None:
        """Release a stuck RPC before waiting for its model ownership lock."""
        self._worker.interrupt()
        self._worker.force_stop()
        self.close()

    def close(self) -> None:
        with self.model_lock:
            self._worker.close()
            self._worker = ProcessWorker(
                self.config, "video_search", "yt_web.workers.search"
            )
            self._configuration = None

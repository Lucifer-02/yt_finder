"""CLI-owned search resource lifetime and filter/ranker orchestration."""

import logging
from contextlib import ExitStack
from time import perf_counter
from types import TracebackType
from typing import Protocol

from video_search.cache import EmbeddingStore
from video_search.config import SearchConfig
from video_search.corpus import Corpus
from video_search.events import ProgressHandler
from video_search.filtering import VideoFilter
from video_search.models import DEFAULT_RERANKER, MODELS
from video_search.search import SearchEngine, SearchResult

logger = logging.getLogger(__name__)


class Searcher(Protocol):
    def search(
        self,
        query: str,
        top: int = 5,
        min_similarity: float | None = None,
        *,
        moments_per_video: int = 3,
    ) -> list[SearchResult]: ...


class SearchSession:
    """One reusable session per worker, used sequentially on its owning thread.

    Owns read-only corpus and separate writable cache. Models and matrices stay
    loaded between searches; VideoFilter detects new collector commits and passes
    selected video snapshots to the database-independent SearchEngine.
    """

    def __init__(
        self, config: SearchConfig, *, on_progress: ProgressHandler | None = None
    ):
        source_path, cache_path = config.validate()
        self._started = perf_counter()
        logger.info(
            "Search session starting: db=%s read_only=true cache=%s model=%s language=%s source=%s source_mode=%s device=%s batch_size=%s mode=%s rerank=%s",
            source_path,
            cache_path,
            config.model,
            config.language,
            config.transcript_source,
            config.source_mode,
            config.device or "auto",
            config.batch_size,
            config.mode,
            config.use_rerank,
            extra={"phase": "session_start"},
        )
        self.config = config
        self._resources = ExitStack()
        self._closed = False
        self._engine: SearchEngine | None = None
        try:
            corpus = Corpus(str(source_path))
            self._resources.callback(corpus.close)
            self.channel_id = (
                corpus.channel_id(config.channel) if config.channel else None
            )
            self._filter = VideoFilter(corpus)
            cache = EmbeddingStore(str(cache_path))
            self._resources.callback(cache.close)
            self._engine = SearchEngine(
                cache,
                model_name=MODELS.get(config.model, config.model),
                batch_size=config.batch_size,
                device=config.device,
                reranker_name=DEFAULT_RERANKER if config.use_rerank else None,
                mode=config.mode,
                on_progress=on_progress,
            )
        except BaseException as error:
            logger.error(
                "Search session initialization failed: error_type=%s",
                type(error).__name__,
                extra={"phase": "session_failed"},
            )
            self.close()
            raise

    def search(
        self,
        query: str,
        top: int = 5,
        min_similarity: float | None = None,
        *,
        moments_per_video: int = 3,
    ) -> list[SearchResult]:
        if self._closed or self._engine is None:
            raise RuntimeError("Search session is closed.")
        return self._engine.search(
            query,
            self._filter.select(self.config.filters()),
            top=top,
            min_similarity=min_similarity,
            moments_per_video=moments_per_video,
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._resources.close()
            finally:
                self._engine = None
                logger.info(
                    "Search session closed: elapsed_seconds=%.3f",
                    perf_counter() - self._started,
                    extra={"phase": "session_close"},
                )

    def __enter__(self) -> "SearchSession":
        if self._closed:
            raise RuntimeError("Search session is closed.")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

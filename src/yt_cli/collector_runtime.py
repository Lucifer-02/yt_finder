"""Application-owned collector setup, dispatch and resource lifetime."""

import logging
from dataclasses import dataclass
from time import perf_counter

from transcript_collector.config import CollectionRequest
from transcript_collector.engines import create_engine
from transcript_collector.errors import VideoNotReady
from transcript_collector.events import CollectionEvent, EventHandler, emit
from transcript_collector.pipeline import (
    _empty_counts,
    index_channel,
    index_playlist,
    index_video,
)
from transcript_collector.store import TranscriptStore
from transcript_collector.youtube import YouTube, playlist_url, video_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionResult:
    scope: str
    counts: dict[str, int]

    @property
    def failed(self) -> bool:
        return bool(self.counts["errors"] or self.counts["blocked"])


def collect(
    request: CollectionRequest, *, on_event: EventHandler | None = None
) -> CollectionResult:
    """Run synchronously in the calling worker; own and close the SQLite store.

    No terminal output, environment changes or process exits. Partial failures
    and blocking are returned in counts; setup failures raise normal exceptions.
    """
    request.validate()
    started = perf_counter()
    logger.info(
        "Collection started; mode=%s language=%s",
        request.mode,
        request.language,
        extra={"phase": "collection_start"},
    )
    url = video_url(request.target)
    playlist = playlist_url(request.target) if not url else None
    scope = "single video" if url else "playlist" if playlist else "channel /videos"
    transcriber = (
        create_engine(
            request.engine or "whisper",
            request.engine_model or "medium",
            request.device,
        )
        if request.mode == "generate"
        else None
    )
    source = YouTube(request.language, request.cookies)
    emit(on_event, CollectionEvent("message", message=f"Mode: {request.mode}"))
    if transcriber:
        emit(
            on_event, CollectionEvent("message", message=f"Engine: {transcriber.name}")
        )
    store = None
    try:
        if url:
            channel, video = source.resolve_video(url)
            emit(
                on_event,
                CollectionEvent(
                    "message", message=f"Video: {video.title} ({video.id})"
                ),
            )
            emit(
                on_event,
                CollectionEvent(
                    "message", message=f"Channel: {channel.title} ({channel.id})"
                ),
            )
            store = TranscriptStore(request.db)
            store.save_channel(channel)
            counts = index_video(
                source,
                store,
                video,
                request.language,
                request.refresh,
                transcriber,
                mode=request.mode,
                on_event=on_event,
            )
            emit(
                on_event, CollectionEvent("summary", scope=scope, counts=counts.copy())
            )
        elif playlist:
            emit(on_event, CollectionEvent("message", message=f"Playlist: {playlist}"))
            store = TranscriptStore(request.db)
            counts = index_playlist(
                source,
                store,
                playlist,
                request.language,
                request.refresh,
                transcriber,
                mode=request.mode,
                on_event=on_event,
            )
        else:
            channel = source.resolve(request.target)
            emit(
                on_event,
                CollectionEvent(
                    "message", message=f"Channel: {channel.title} ({channel.id})"
                ),
            )
            store = TranscriptStore(request.db)
            counts = index_channel(
                source,
                store,
                channel,
                request.language,
                request.refresh,
                transcriber,
                mode=request.mode,
                on_event=on_event,
            )
        logger.info(
            "Collection finished; scope=%s counts=%s elapsed=%.3fs",
            scope,
            counts,
            perf_counter() - started,
            extra={
                "phase": "collection_end",
                "counts": counts.copy(),
                "elapsed_seconds": perf_counter() - started,
            },
        )
        return CollectionResult(scope, counts)
    except VideoNotReady as exc:
        counts = _empty_counts()
        counts.update(listed=1, unavailable=1)
        emit(on_event, CollectionEvent("message", message=str(exc)))
        emit(on_event, CollectionEvent("summary", scope=scope, counts=counts.copy()))
        logger.info(
            "Collection finished; scope=%s counts=%s elapsed=%.3fs",
            scope,
            counts,
            perf_counter() - started,
            extra={
                "phase": "collection_end",
                "counts": counts.copy(),
                "elapsed_seconds": perf_counter() - started,
            },
        )
        return CollectionResult(scope, counts)
    except KeyboardInterrupt:
        logger.info(
            "Collection cancelled; committed transcripts retained",
            extra={"phase": "cancel"},
        )
        raise
    except Exception as exc:
        logger.error(
            "Collection failed; reason=%s elapsed=%.3fs",
            type(exc).__name__,
            perf_counter() - started,
            extra={
                "phase": "collection_error",
                "elapsed_seconds": perf_counter() - started,
            },
        )
        raise
    finally:
        if store is not None:
            store.close()
            logger.debug(
                "Collection database closed", extra={"phase": "database_close"}
            )

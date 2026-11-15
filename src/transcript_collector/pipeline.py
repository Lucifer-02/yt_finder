"""Collect videos sequentially and commit each transcript separately."""

import logging
import re
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from time import perf_counter

from .contracts import (
    Channel,
    ChannelSource,
    PlaylistSource,
    TranscriptionEngine,
    TranscriptRepository,
    Video,
    VideoSource,
)
from .errors import (
    CaptionsUnavailable,
    SourceBlocked,
    SourceError,
    TranscriptionError,
    VideoNotReady,
)
from .events import CollectionEvent, EventHandler, emit

logger = logging.getLogger(__name__)


def _validate_options(
    mode: str,
    language: str | None,
    transcriber: TranscriptionEngine | None,
) -> None:
    if mode not in {"direct", "generate"}:
        raise ValueError("Choose direct or generate as collection mode.")
    if mode == "direct" and (
        not language
        or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language)
    ):
        raise ValueError("Direct mode requires one --language, for example vi or en.")
    if mode == "generate" and transcriber is None:
        raise ValueError("Generate mode requires a transcription engine.")
    if mode == "generate" and (
        transcriber is not None
        and (
            not re.fullmatch(r"[A-Za-z0-9_-]+", transcriber.name)
            or transcriber.name in {"youtube", "legacy"}
        )
    ):
        raise ValueError("Invalid audio transcription engine name.")


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(
        (
            "listed",
            "cached",
            "indexed",
            "unavailable",
            "errors",
            "retained",
            "audio_downloaded",
            "audio_errors",
            "blocked",
            "deferred",
        ),
        0,
    )


def index_video(
    source: VideoSource,
    store: TranscriptRepository,
    video: Video,
    language: str | None,
    refresh: bool = False,
    transcriber: TranscriptionEngine | None = None,
    *,
    mode: str,
    position: int = 1,
    total: int = 1,
    on_event: EventHandler | None = None,
) -> dict[str, int]:
    """Collect one video; its owning channel must already be saved in the store.

    Commit inventory before fetching captions/audio, then commit the transcript
    atomically. URL, channel and playlist collection use this lifecycle.
    """
    _validate_options(mode, language, transcriber)
    transcript_source = "youtube" if mode == "direct" else "generate"
    config = "" if mode == "direct" else transcriber.config if transcriber else ""
    counts = _empty_counts()
    counts["listed"] = 1
    started = perf_counter()
    logger.info(
        "Video %s started (%d/%d); mode=%s language=%s refresh=%s",
        video.id,
        position,
        total,
        mode,
        language,
        refresh,
        extra={"phase": "video_start", "video_id": video.id, "language": language},
    )
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", video.id):
            raise SourceError(f"Invalid video ID: {video.id}")
        store.save_inventory([video])
        logger.info(
            "Video %s inventory committed",
            video.id,
            extra={"phase": "inventory_commit", "video_id": video.id},
        )
        cached = not refresh and store.cached(
            video.id,
            transcript_source,
            config,
            language=language if mode == "direct" else None,
            source_mode=transcriber.name
            if mode == "generate" and transcriber
            else None,
        )
        logger.info(
            "Video %s transcript cache %s; source=%s language=%s",
            video.id,
            "hit" if cached else "miss",
            transcript_source,
            language,
            extra={
                "phase": "cache_hit" if cached else "cache_miss",
                "video_id": video.id,
                "language": language,
            },
        )
        logger.debug(
            "Video %s cache config=%s engine=%s",
            video.id,
            config,
            transcriber.name if transcriber else None,
            extra={"phase": "cache_check", "video_id": video.id},
        )
        emit(
            on_event,
            CollectionEvent(
                "video_started",
                video_id=video.id,
                title=video.title,
                position=position,
                total=total,
                cached=cached,
            ),
        )
        if cached:
            counts["cached"] = 1
            emit(
                on_event,
                CollectionEvent(
                    "video_finished",
                    video_id=video.id,
                    title=video.title,
                    position=position,
                    total=total,
                    cached=True,
                    counts=counts.copy(),
                ),
            )
            logger.info(
                "Video %s completed from cache; elapsed=%.3fs",
                video.id,
                perf_counter() - started,
                extra={
                    "phase": "cache_hit",
                    "video_id": video.id,
                    "elapsed_seconds": perf_counter() - started,
                },
            )
            return counts
        if mode == "direct":
            logger.info(
                "Video %s fetching captions; language=%s",
                video.id,
                language,
                extra={
                    "phase": "caption_fetch",
                    "video_id": video.id,
                    "language": language,
                },
            )
            segments = source.transcript(video.id)
            actual_language = source.transcript_language
            source_mode = source.transcript_mode
        else:
            if transcriber is None:
                raise TranscriptionError("Generation requires a transcription engine.")
            try:
                with tempfile.TemporaryDirectory(prefix="yt-collect-") as temporary:
                    logger.info(
                        "Video %s temporary audio directory created",
                        video.id,
                        extra={"phase": "audio_temp_create", "video_id": video.id},
                    )
                    try:
                        audio = source.download_audio(video.id, temporary)
                        counts["audio_downloaded"] = 1
                    except VideoNotReady:
                        raise
                    except (SourceError, OSError):
                        counts["audio_errors"] = 1
                        raise
                    segments = transcriber.transcript(audio)
                    actual_language = transcriber.transcript_language
                    source_mode = transcriber.name
            finally:
                logger.info(
                    "Video %s temporary audio context exited",
                    video.id,
                    extra={"phase": "cleanup", "video_id": video.id},
                )
        if not any(s.text.strip() for s in segments):
            raise CaptionsUnavailable("Transcript contains no text")
        logger.info(
            "Video %s transcript ready; language=%s source=%s source_mode=%s segments=%d",
            video.id,
            actual_language,
            transcript_source,
            source_mode,
            len(segments),
            extra={
                "phase": "transcript_ready",
                "video_id": video.id,
                "language": actual_language,
                "source_mode": source_mode,
                "segments": len(segments),
            },
        )
        store.save_video(
            video,
            transcript_source,
            config,
            segments,
            language=actual_language,
            source_mode=source_mode,
        )
        counts["indexed"] = 1
    except (
        SourceError,
        TranscriptionError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        if isinstance(exc, (CaptionsUnavailable, VideoNotReady)):
            counts["unavailable"] = 1
        else:
            counts["errors"] = 1
        if store.has_transcript(video.id):
            counts["retained"] = 1
        if isinstance(exc, SourceBlocked):
            counts["blocked"] = 1
        logger.warning(
            "Video %s processing stopped; reason=%s blocked=%s retained=%s elapsed=%.3fs",
            video.id,
            type(exc).__name__,
            bool(counts["blocked"]),
            bool(counts["retained"]),
            perf_counter() - started,
            extra={
                "phase": "video_error",
                "video_id": video.id,
                "elapsed_seconds": perf_counter() - started,
            },
        )
        emit(on_event, CollectionEvent("message", message=str(exc), video_id=video.id))
    logger.info(
        "Video %s finished; counts=%s elapsed=%.3fs",
        video.id,
        counts,
        perf_counter() - started,
        extra={
            "phase": "video_end",
            "video_id": video.id,
            "counts": counts.copy(),
            "elapsed_seconds": perf_counter() - started,
        },
    )
    emit(
        on_event,
        CollectionEvent(
            "video_finished",
            video_id=video.id,
            title=video.title,
            position=position,
            total=total,
            counts=counts.copy(),
        ),
    )
    return counts


def index_playlist(
    source: PlaylistSource,
    store: TranscriptRepository,
    url: str,
    language: str | None,
    refresh: bool = False,
    transcriber: TranscriptionEngine | None = None,
    *,
    mode: str,
    on_event: EventHandler | None = None,
) -> dict[str, int]:
    """Resolve each playlist video's owner, then use the single-video lifecycle."""
    _validate_options(mode, language, transcriber)
    started = perf_counter()
    logger.info("Playlist listing started", extra={"phase": "listing_start"})
    video_ids = source.playlist_videos(url)
    logger.info(
        "Playlist listing finished; videos=%d elapsed=%.3fs",
        len(video_ids),
        perf_counter() - started,
        extra={"phase": "listing_end", "elapsed_seconds": perf_counter() - started},
    )
    return _index_videos(
        source,
        store,
        video_ids,
        language,
        refresh,
        transcriber,
        mode=mode,
        scope="playlist; unique listed videos",
        resolve=source.resolve_video,
        on_event=on_event,
    )


def index_channel(
    source: ChannelSource,
    store: TranscriptRepository,
    channel: Channel,
    language: str | None,
    refresh: bool = False,
    transcriber: TranscriptionEngine | None = None,
    *,
    mode: str,
    on_event: EventHandler | None = None,
) -> dict[str, int]:
    """List the channel's videos tab and collect each video using index_video."""
    _validate_options(mode, language, transcriber)
    store.save_channel(channel)
    started = perf_counter()
    logger.info(
        "Channel %s /videos listing started",
        channel.id,
        extra={"phase": "listing_start", "channel_id": channel.id},
    )
    videos = source.videos(channel)
    logger.info(
        "Channel %s listing finished; videos=%d elapsed=%.3fs",
        channel.id,
        len(videos),
        perf_counter() - started,
        extra={
            "phase": "listing_end",
            "channel_id": channel.id,
            "elapsed_seconds": perf_counter() - started,
        },
    )
    store.save_inventory(videos)
    return _index_videos(
        source,
        store,
        videos,
        language,
        refresh,
        transcriber,
        mode=mode,
        scope="channel /videos; all listed entries",
        on_event=on_event,
    )


def _index_videos(
    source: VideoSource,
    store: TranscriptRepository,
    videos: Sequence[Video | str],
    language: str | None,
    refresh: bool,
    transcriber: TranscriptionEngine | None,
    *,
    mode: str,
    scope: str,
    resolve: Callable[[str], tuple[Channel, Video]] | None = None,
    on_event: EventHandler | None = None,
) -> dict[str, int]:
    """Process yt-dlp's listed videos using one shared collection loop."""
    counts = _empty_counts()
    transcript_source = "youtube" if mode == "direct" else "generate"
    config = "" if mode == "direct" else transcriber.config if transcriber else ""
    for n, entry in enumerate(videos, 1):
        video_id = entry if isinstance(entry, str) else entry.id
        try:
            if isinstance(entry, str):
                if not refresh and store.cached(
                    video_id,
                    transcript_source,
                    config,
                    language=language if mode == "direct" else None,
                    source_mode=transcriber.name
                    if mode == "generate" and transcriber
                    else None,
                ):
                    logger.info(
                        "Video %s completed from cache (skipped resolve)",
                        video_id,
                        extra={"phase": "cache_hit", "video_id": video_id},
                    )
                    emit(
                        on_event,
                        CollectionEvent(
                            "video_started",
                            video_id=video_id,
                            position=n,
                            total=len(videos),
                            cached=True,
                        ),
                    )
                    cached_counts = _empty_counts()
                    cached_counts["listed"] = 1
                    cached_counts["cached"] = 1
                    emit(
                        on_event,
                        CollectionEvent(
                            "video_finished",
                            video_id=video_id,
                            position=n,
                            total=len(videos),
                            cached=True,
                            counts=cached_counts.copy(),
                        ),
                    )
                    counts["cached"] += 1
                    continue
                if resolve is None:
                    raise ValueError("A video resolver is required for video IDs.")
                logger.info(
                    "Video %s metadata resolution started",
                    video_id,
                    extra={"phase": "metadata_resolve", "video_id": video_id},
                )
                channel, video = resolve(f"https://www.youtube.com/watch?v={entry}")
                store.save_channel(channel)
            else:
                video = entry
            result = index_video(
                source,
                store,
                video,
                language,
                refresh,
                transcriber,
                mode=mode,
                position=n,
                total=len(videos),
                on_event=on_event,
            )
        except (SourceError, OSError, ValueError, sqlite3.Error) as exc:
            logger.warning(
                "Video %s metadata/inventory failed; reason=%s",
                video_id,
                type(exc).__name__,
                extra={"phase": "metadata_resolve", "video_id": video_id},
            )
            result = _empty_counts()
            result["unavailable" if isinstance(exc, VideoNotReady) else "errors"] = 1
            result["blocked"] = int(isinstance(exc, SourceBlocked))
            result["retained"] = int(store.has_transcript(video_id))
            emit(
                on_event,
                CollectionEvent(
                    "video_finished",
                    message=f"[{n}/{len(videos)}] {video_id}: {exc}",
                    video_id=video_id,
                    position=n,
                    total=len(videos),
                    counts=result.copy(),
                ),
            )
        for key, value in result.items():
            counts[key] += value
        if result["blocked"]:
            logger.warning(
                "Collection stopped after video %s: source blocked",
                video_id,
                extra={"phase": "source_blocked", "video_id": video_id},
            )
            break
    counts["listed"] = len(videos)
    counts["deferred"] = len(videos) - sum(
        counts[key] for key in ("cached", "indexed", "unavailable", "errors")
    )
    logger.info(
        "Collection summary; scope=%s counts=%s",
        scope,
        counts,
        extra={"phase": "collection_summary", "counts": counts.copy()},
    )
    emit(on_event, CollectionEvent("summary", scope=scope, counts=counts.copy()))
    return counts

"""Select videos and one usable transcript each in a read-only SQLite snapshot."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING

from .documents import TranscriptSegment, VideoTranscript

if TYPE_CHECKING:
    from .corpus import Corpus

logger = logging.getLogger(__name__)
YOUTUBE_MODES = ("manual", "auto_generated", "translated", "unknown")


def validate_filters(source: str, language: str, source_mode: str | None) -> None:
    if source not in {"auto", "youtube", "generate"}:
        raise ValueError("Unknown transcript source; use auto, youtube or generate.")
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        raise ValueError("Specify one transcript language, for example vi or en.")
    if source_mode is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source_mode):
            raise ValueError("Invalid source mode.")
        if source == "youtube" and source_mode not in YOUTUBE_MODES:
            raise ValueError("Unknown YouTube source mode.")
        if source == "generate" and source_mode in {
            *YOUTUBE_MODES,
            "legacy",
            "youtube",
        }:
            raise ValueError("Generate source mode must be an engine name.")


@dataclass(frozen=True)
class VideoFilters:
    channel: str | None = None
    language: str = "vi"
    transcript_source: str = "auto"
    source_mode: str | None = None
    video_ids: tuple[str, ...] | None = None

    def validate(self) -> None:
        validate_filters(self.transcript_source, self.language, self.source_mode)
        if self.video_ids is not None and any(
            not video_id.strip() for video_id in self.video_ids
        ):
            raise ValueError("Video IDs cannot be empty.")


class VideoFilter:
    """Read from a Corpus on its owning thread; never load a model or write a cache."""

    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self._signature: tuple[int, VideoFilters] | None = None
        self._videos: tuple[VideoTranscript, ...] = ()

    def select(self, filters: VideoFilters = VideoFilters()) -> list[VideoTranscript]:
        filters.validate()
        started = perf_counter()
        version = (self.corpus.revision(), filters)
        if version == self._signature:
            return list(self._videos)
        logger.info(
            "Selecting videos: reload=%s id_filtered=%s",
            self._signature is not None,
            filters.video_ids is not None,
            extra={
                "phase": "corpus_reload"
                if self._signature is not None
                else "corpus_load"
            },
        )
        db = self.corpus.db
        db.execute("BEGIN")
        try:
            channel_id = (
                self.corpus.channel_id(filters.channel) if filters.channel else None
            )
            query = """
                SELECT t.*,v.title,v.description,v.channel_id,c.title AS channel_title
                FROM transcripts t JOIN videos v ON v.id=t.video_id JOIN channels c ON c.id=v.channel_id
                WHERE (? IS NULL OR v.channel_id=?) AND (?='auto' OR t.source=?)
                  AND (lower(t.language)=lower(?) OR substr(lower(t.language),1,length(?)+1)=lower(?)||'-')
                  AND (? IS NULL OR t.source_mode=?)
                  AND ((t.source='youtube' AND t.source_mode IN ('manual','auto_generated','translated','unknown'))
                    OR (t.source='generate' AND (? IS NOT NULL OR t.source_mode='whisper')))
                """
            parameters = (
                channel_id,
                channel_id,
                filters.transcript_source,
                filters.transcript_source,
                filters.language,
                filters.language,
                filters.language,
                filters.source_mode,
                filters.source_mode,
                filters.source_mode,
            )
            if filters.video_ids is None:
                versions = db.execute(query, parameters).fetchall()
            else:
                # Bound ID batches stay below SQLite's parameter limit; [] selects nothing.
                ids = list(dict.fromkeys(filters.video_ids))
                versions = []
                for offset in range(0, len(ids), 500):
                    batch = ids[offset : offset + 500]
                    placeholders = ",".join("?" for _ in batch)
                    versions.extend(
                        db.execute(
                            query + f" AND v.id IN ({placeholders})",
                            (*parameters, *batch),
                        ).fetchall()
                    )
            versions.sort(
                key=lambda row: (
                    row["video_id"],
                    0 if row["source"] == "youtube" else 1,
                    YOUTUBE_MODES.index(row["source_mode"])
                    if row["source"] == "youtube"
                    else 0,
                    row["language"].casefold() != filters.language.casefold(),
                    row["language"],
                    row["source_mode"],
                )
            )
            result: list[VideoTranscript] = []
            seen: set[str] = set()
            for version_row in versions:
                video_id = version_row["video_id"]
                if video_id in seen:
                    continue
                rows = db.execute(
                    """SELECT text,start,duration FROM transcript_parts
                    WHERE video_id=? AND language=? AND source=? AND source_mode=? ORDER BY start,position""",
                    (
                        video_id,
                        version_row["language"],
                        version_row["source"],
                        version_row["source_mode"],
                    ),
                ).fetchall()
                segments = tuple(
                    TranscriptSegment(row["text"], row["start"], row["duration"])
                    for row in rows
                    if row["text"].strip()
                )
                if segments:
                    result.append(
                        VideoTranscript(
                            video_id=video_id,
                            channel_id=version_row["channel_id"],
                            channel_title=version_row["channel_title"],
                            title=version_row["title"],
                            description=version_row["description"],
                            transcript_source=version_row["source"],
                            language=version_row["language"],
                            source_mode=version_row["source_mode"],
                            transcript_config=version_row["config"],
                            segments=segments,
                        )
                    )
                    seen.add(video_id)
            logger.info(
                "Videos selected: candidate_transcripts=%d selected_videos=%d elapsed_seconds=%.3f",
                len(versions),
                len(result),
                perf_counter() - started,
                extra={"phase": "corpus_ready", "total": len(result)},
            )
        finally:
            db.rollback()
        self._signature = version
        self._videos = tuple(result)
        return result

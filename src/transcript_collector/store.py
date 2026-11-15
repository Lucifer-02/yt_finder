"""SQLite results, keyed by video, actual language and transcript source."""

import logging
import math
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from .contracts import Channel, Segment, Video

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
COLUMNS = {
    "channels": {"id", "title", "url"},
    "videos": {"id", "channel_id", "title", "description"},
    "transcripts": {
        "video_id",
        "language",
        "source",
        "source_mode",
        "config",
        "updated_at",
    },
    "transcript_parts": {
        "video_id",
        "language",
        "source",
        "source_mode",
        "position",
        "start",
        "duration",
        "text",
    },
}


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


class TranscriptStore:
    def __init__(self, path: str):
        db_path = Path(path).expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), timeout=20)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {
                r[0]
                for r in self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if tables or version:
                if (
                    tables != set(COLUMNS.keys())
                    or version not in {1, SCHEMA_VERSION}
                    or any(
                        {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                        != (columns - {"source_mode"} if version == 1 else columns)
                        for table, columns in COLUMNS.items()
                    )
                ):
                    raise ValueError(
                        "Incompatible collector database; choose a new --db path. Existing data was not migrated or deleted."
                    )
                if version == 1:
                    self._migrate_v1()
            else:
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE channels(id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL);
                    CREATE TABLE videos(id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
                        title TEXT NOT NULL, description TEXT NOT NULL);
                    CREATE INDEX videos_channel ON videos(channel_id);
                    CREATE TABLE transcripts(video_id TEXT NOT NULL REFERENCES videos(id),
                        language TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('youtube','generate')),
                        source_mode TEXT NOT NULL, config TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY(video_id,language,source,source_mode),
                        CHECK(source <> 'youtube' OR source_mode IN ('manual','auto_generated','translated','unknown')));
                    CREATE INDEX transcripts_source ON transcripts(source,language,video_id);
                    CREATE TABLE transcript_parts(video_id TEXT NOT NULL, language TEXT NOT NULL,
                        source TEXT NOT NULL, source_mode TEXT NOT NULL,
                        position INTEGER NOT NULL CHECK(position>=0),
                        start REAL NOT NULL CHECK(start>=0), duration REAL NOT NULL CHECK(duration>=0),
                        text TEXT NOT NULL, PRIMARY KEY(video_id,language,source,source_mode,position),
                        FOREIGN KEY(video_id,language,source,source_mode) REFERENCES transcripts(video_id,language,source,source_mode));
                    PRAGMA user_version=2;
                    COMMIT;
                """)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            logger.info(
                "Collector database ready; schema=%d WAL enabled",
                SCHEMA_VERSION,
                extra={"phase": "database_open"},
            )
        except (ValueError, sqlite3.Error):
            self.db.close()
            raise

    def _migrate_v1(self) -> None:
        """Convert our known v1 contract in one transaction; preserve all segments."""
        self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE transcripts_v2(video_id TEXT NOT NULL REFERENCES videos(id),
                language TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('youtube','generate')),
                source_mode TEXT NOT NULL, config TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(video_id,language,source,source_mode),
                CHECK(source <> 'youtube' OR source_mode IN ('manual','auto_generated','translated','unknown')));
            CREATE TABLE transcript_parts_v2(video_id TEXT NOT NULL, language TEXT NOT NULL,
                source TEXT NOT NULL, source_mode TEXT NOT NULL, position INTEGER NOT NULL CHECK(position>=0),
                start REAL NOT NULL CHECK(start>=0), duration REAL NOT NULL CHECK(duration>=0), text TEXT NOT NULL,
                PRIMARY KEY(video_id,language,source,source_mode,position),
                FOREIGN KEY(video_id,language,source,source_mode) REFERENCES transcripts_v2(video_id,language,source,source_mode));
            INSERT INTO transcripts_v2
                SELECT video_id,language,CASE WHEN source='youtube' THEN 'youtube' ELSE 'generate' END,
                       CASE WHEN source='youtube' THEN 'unknown' ELSE source END,config,updated_at FROM transcripts;
            INSERT INTO transcript_parts_v2
                SELECT video_id,language,CASE WHEN source='youtube' THEN 'youtube' ELSE 'generate' END,
                       CASE WHEN source='youtube' THEN 'unknown' ELSE source END,position,start,duration,text FROM transcript_parts;
            DROP TABLE transcript_parts;
            DROP TABLE transcripts;
            ALTER TABLE transcripts_v2 RENAME TO transcripts;
            ALTER TABLE transcript_parts_v2 RENAME TO transcript_parts;
            CREATE INDEX transcripts_source ON transcripts(source,language,video_id);
            PRAGMA user_version=2;
        """)

        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError(
                "Collector migration found orphaned data; no changes committed."
            )
        self.db.commit()
        logger.info(
            "Collector database migration committed; schema=2",
            extra={"phase": "schema_migration"},
        )

    def save_channel(self, channel: Channel) -> None:
        identifier(channel.id)
        with self.db:
            self.db.execute(
                """INSERT INTO channels VALUES(?,?,?) ON CONFLICT(id)
                DO UPDATE SET title=excluded.title,url=excluded.url""",
                (channel.id, channel.title, channel.url),
            )

    def _save_inventory(self, videos: list[Video]) -> None:
        for video in videos:
            identifier(video.id)
            identifier(video.channel_id)
        self.db.executemany(
            """INSERT INTO videos VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            channel_id=excluded.channel_id,title=excluded.title,
            description=CASE WHEN excluded.description='' THEN videos.description ELSE excluded.description END""",
            [(v.id, v.channel_id, v.title, v.description) for v in videos],
        )

    def save_inventory(self, videos: list[Video]) -> None:
        with self.db:
            self._save_inventory(videos)
        logger.debug(
            "Inventory committed; videos=%d",
            len(videos),
            extra={"phase": "inventory_commit"},
        )

    def cached(
        self,
        video_id: str,
        source: str,
        config: str,
        *,
        language: str | None = None,
        source_mode: str | None = None,
    ) -> bool:
        if source == "youtube":
            if not language:
                raise ValueError("YouTube cache lookup requires a language.")
            language = language.casefold()
            condition = (
                "(lower(language)=? OR substr(lower(language),1,length(?)+1)=?||'-')"
            )
            params = (video_id, source, language, language, language)
        else:
            condition = "config=? AND source_mode=?"
            params = (video_id, source, config, source_mode)
        return (
            self.db.execute(
                "SELECT 1 FROM transcripts t WHERE video_id=? AND source=? AND "
                + condition
                + " AND EXISTS(SELECT 1 FROM transcript_parts p WHERE p.video_id=t.video_id "
                "AND p.language=t.language AND p.source=t.source AND p.source_mode=t.source_mode) LIMIT 1",
                params,
            ).fetchone()
            is not None
        )

    def has_transcript(self, video_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM transcripts WHERE video_id=? LIMIT 1", (video_id,)
            ).fetchone()
            is not None
        )

    def save_video(
        self,
        video: Video,
        source: str,
        config: str,
        segments: list[Segment],
        *,
        language: str,
        source_mode: str,
    ) -> None:
        identifier(source_mode)
        identifier(language)
        if (
            source not in {"youtube", "generate"}
            or (
                source == "youtube"
                and source_mode
                not in {"manual", "auto_generated", "translated", "unknown"}
            )
            or not any(s.text.strip() for s in segments)
        ):
            raise ValueError("A valid source and non-empty transcript are required.")
        for segment in segments:
            if (
                not math.isfinite(segment.start)
                or not math.isfinite(segment.duration)
                or segment.start < 0
                or segment.duration < 0
            ):
                raise ValueError("Transcript timing must be finite and nonnegative.")
        started = perf_counter()
        logger.debug(
            "Video %s transcript transaction started; segments=%d",
            video.id,
            len(segments),
            extra={
                "phase": "transcript_transaction",
                "video_id": video.id,
                "segments": len(segments),
            },
        )
        try:
            with self.db:
                self._save_inventory([video])
                key = (video.id, language, source, source_mode)
                self.db.execute(
                    "DELETE FROM transcript_parts WHERE video_id=? AND language=? AND source=? AND source_mode=?",
                    key,
                )
                self.db.execute(
                    """INSERT INTO transcripts VALUES(?,?,?,?,?,?) ON CONFLICT(video_id,language,source,source_mode)
                    DO UPDATE SET config=excluded.config,updated_at=excluded.updated_at""",
                    (*key, config, datetime.now(UTC).isoformat()),
                )
                self.db.executemany(
                    "INSERT INTO transcript_parts VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (*key, i, s.start, s.duration, s.text)
                        for i, s in enumerate(segments)
                    ],
                )
        except BaseException:
            logger.warning(
                "Video %s transcript transaction rolled back; previous transcript retained",
                video.id,
                extra={"phase": "transcript_rollback", "video_id": video.id},
            )
            raise
        logger.info(
            "Video %s transcript committed; language=%s source=%s source_mode=%s segments=%d elapsed=%.3fs",
            video.id,
            language,
            source,
            source_mode,
            len(segments),
            perf_counter() - started,
            extra={
                "phase": "transcript_commit",
                "video_id": video.id,
                "language": language,
                "source_mode": source_mode,
                "segments": len(segments),
                "elapsed_seconds": perf_counter() - started,
            },
        )

    def close(self) -> None:
        self.db.close()

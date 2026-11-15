"""Read-only collector connection and schema validation; selection is in filtering."""

import logging
import sqlite3
from pathlib import Path
from time import perf_counter

from .documents import Window, video_windows
from .filtering import VideoFilter, VideoFilters
from .filtering import validate_filters as validate_filters

logger = logging.getLogger(__name__)


class Corpus:
    def __init__(self, path: str):
        started = perf_counter()
        self.db = sqlite3.connect(
            Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True
        )
        self.db.row_factory = sqlite3.Row
        try:
            self._validate_schema()
            logger.info(
                "Transcript schema validated: version=2 read_only=true elapsed_seconds=%.3f",
                perf_counter() - started,
                extra={"phase": "schema_validated"},
            )
        except (ValueError, sqlite3.Error) as error:
            logger.error(
                "Transcript schema validation failed: error_type=%s",
                type(error).__name__,
                extra={"phase": "schema_failed"},
            )
            self.db.close()
            raise

    def _validate_schema(self) -> None:
        required = {
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
        for table, columns in required.items():
            actual = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if missing := columns - actual:
                raise ValueError(
                    f"Transcript database is missing {table} columns: {', '.join(sorted(missing))}. Collector schema v2 is required."
                )
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError(
                "Collector schema version 2 is required; search does not migrate input databases."
            )

    def channel_id(self, value: str) -> str:
        rows = self.db.execute(
            "SELECT id FROM channels WHERE id=? OR url=?",
            (value, value.rstrip("/")),
        ).fetchall()
        if not rows:
            folded = value.casefold()
            rows = [
                row
                for row in self.db.execute("SELECT id, title FROM channels").fetchall()
                if row["title"].casefold() == folded
            ]
        if len(rows) != 1:
            raise ValueError("Channel not found or ambiguous; use its UC ID.")
        return rows[0]["id"]

    def windows(
        self,
        channel_id: str | None = None,
        source: str = "auto",
        language: str = "vi",
        source_mode: str | None = None,
    ) -> list[Window]:
        """Compatibility adapter for window snapshots; selection lives in VideoFilter."""
        return video_windows(
            VideoFilter(self).select(
                VideoFilters(
                    channel=channel_id,
                    transcript_source=source,
                    language=language,
                    source_mode=source_mode,
                )
            )
        )

    def close(self) -> None:
        self.db.close()

    def revision(self) -> int:
        return self.db.execute("PRAGMA data_version").fetchone()[0]

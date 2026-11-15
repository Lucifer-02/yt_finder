"""Read-only library views over the shared transcript schema v2."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Library:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(
            self.path.expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=20
        )
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError(
                    "Database transcript cần schema phiên bản 2. Hãy chạy collector để chuẩn bị dữ liệu."
                )
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
                if not columns <= {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                }:
                    raise ValueError(
                        "Database transcript không tương thích schema phiên bản 2."
                    )
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    def summary(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "has_database": False,
                "stats": {
                    "channels": 0,
                    "videos": 0,
                    "transcripts": 0,
                    "segments": 0,
                    "last_updated": None,
                },
                "channels": [],
                "languages": [],
                "source_modes": [],
            }
        with self.connect() as db:
            stats = {
                name: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for name, table in (
                    ("channels", "channels"),
                    ("videos", "videos"),
                    ("transcripts", "transcripts"),
                    ("segments", "transcript_parts"),
                )
            }
            stats["last_updated"] = db.execute(
                "SELECT max(updated_at) FROM transcripts"
            ).fetchone()[0]
            channels = [
                dict(row)
                for row in db.execute("""
                SELECT c.id,c.title,count(v.id) AS videos,
                  (SELECT count(*) FROM transcripts t JOIN videos tv ON tv.id=t.video_id WHERE tv.channel_id=c.id) AS transcripts
                FROM channels c LEFT JOIN videos v ON v.channel_id=c.id
                GROUP BY c.id,c.title ORDER BY c.title
            """)
            ]
            languages = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT language FROM transcripts ORDER BY language"
                )
            ]
            modes = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT source_mode FROM transcripts ORDER BY source_mode"
                )
            ]
            return {
                "has_database": True,
                "stats": stats,
                "channels": channels,
                "languages": languages,
                "source_modes": modes,
            }

    def videos(
        self,
        *,
        channel: str | None = None,
        query: str = "",
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": [], "total": 0, "page": page, "limit": limit}
        where, parameters = [], []
        if channel:
            where.append("v.channel_id=?")
            parameters.append(channel)
        if query:
            where.append("instr(lower(v.title),lower(?))>0")
            parameters.append(query)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            total = db.execute(
                "SELECT count(*) FROM videos v" + clause, parameters
            ).fetchone()[0]
            rows = db.execute(
                """
                SELECT v.id,v.title,v.channel_id,c.title AS channel_title,
                   (SELECT count(*) FROM transcripts t WHERE t.video_id=v.id) AS transcripts,
                   (SELECT group_concat(DISTINCT language) FROM transcripts t WHERE t.video_id=v.id) AS languages
                FROM videos v JOIN channels c ON c.id=v.channel_id
            """
                + clause
                + " ORDER BY v.title,v.id LIMIT ? OFFSET ?",
                [*parameters, limit, (page - 1) * limit],
            )
            items = [
                {
                    **dict(row),
                    "languages": (row["languages"] or "").split(",")
                    if row["languages"]
                    else [],
                }
                for row in rows
            ]
            return {"items": items, "total": total, "page": page, "limit": limit}

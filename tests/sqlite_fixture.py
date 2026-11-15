"""Build collector schema v2 inputs independently of the collector implementation."""

import sqlite3


class SQLiteFixture:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE channels(id TEXT PRIMARY KEY, title TEXT, url TEXT);
            CREATE TABLE videos(id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, description TEXT);
            CREATE TABLE transcripts(video_id TEXT, language TEXT, source TEXT, source_mode TEXT,
                config TEXT, updated_at TEXT, PRIMARY KEY(video_id,language,source,source_mode));
            CREATE TABLE transcript_parts(video_id TEXT, language TEXT, source TEXT, source_mode TEXT,
                position INTEGER, start REAL, duration REAL, text TEXT,
                PRIMARY KEY(video_id,language,source,source_mode,position));
            PRAGMA user_version=2;
        """)

    def save_channel(self, channel):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?,?)",
                (channel.id, channel.title, channel.url),
            )

    def save_video(
        self, video, source, config, segments, *, language="vi", source_mode=None
    ):
        mode = source_mode or ("manual" if source == "youtube" else source)
        source = "youtube" if source == "youtube" else "generate"
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO videos VALUES(?,?,?,?)",
                (video.id, video.channel_id, video.title, video.description),
            )
            key = (video.id, language, source, mode)
            self.db.execute(
                "INSERT OR REPLACE INTO transcripts VALUES(?,?,?,?,?,?)",
                (*key, config, "timestamp"),
            )
            self.db.execute(
                "DELETE FROM transcript_parts WHERE video_id=? AND language=? AND source=? AND source_mode=?",
                key,
            )
            self.db.executemany(
                "INSERT INTO transcript_parts VALUES(?,?,?,?,?,?,?,?)",
                [
                    (*key, i, s.start, s.duration, s.text)
                    for i, s in enumerate(segments)
                ],
            )

    def close(self):
        self.db.close()

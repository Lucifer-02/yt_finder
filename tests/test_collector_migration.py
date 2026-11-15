"""Migrate only the known collector schema, retaining transcript contents."""

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.store import TranscriptStore


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "results.sqlite3"

    def v1(self, orphan=False):
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE channels(id TEXT PRIMARY KEY,title TEXT,url TEXT);
                CREATE TABLE videos(id TEXT PRIMARY KEY,channel_id TEXT,title TEXT,description TEXT);
                CREATE TABLE transcripts(video_id TEXT,language TEXT,source TEXT,config TEXT,updated_at TEXT,
                    PRIMARY KEY(video_id,language,source));
                CREATE TABLE transcript_parts(video_id TEXT,language TEXT,source TEXT,position INTEGER,start REAL,duration REAL,text TEXT,
                    PRIMARY KEY(video_id,language,source,position));
                CREATE TABLE untouched(note TEXT);
                INSERT INTO untouched VALUES('keep');
                INSERT INTO channels VALUES('c','channel','url');
                INSERT INTO videos VALUES('v','c','title','description');
                INSERT INTO transcripts VALUES('v','vi','youtube','vi,en','timestamp');
                INSERT INTO transcripts VALUES('v','vi','whisper','medium','timestamp');
                INSERT INTO transcript_parts VALUES('v','vi','youtube',0,1.5,2.5,'caption');
                INSERT INTO transcript_parts VALUES('v','vi','whisper',0,2,3,'speech');
                PRAGMA user_version=1;
            """)
            if orphan:
                db.execute(
                    "INSERT INTO transcript_parts VALUES('absent','vi','youtube',0,0,1,'orphan')"
                )
                db.commit()

    def test_migration_preserves_data_and_records_unknown_for_old_youtube(self):
        self.v1()
        with contextlib.closing(TranscriptStore(str(self.path))) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
            rows = [
                tuple(r)
                for r in store.db.execute(
                    "SELECT source,source_mode,config,updated_at FROM transcripts ORDER BY source"
                )
            ]
            self.assertEqual(
                rows,
                [
                    ("generate", "whisper", "medium", "timestamp"),
                    ("youtube", "unknown", "vi,en", "timestamp"),
                ],
            )
            parts = [
                tuple(r)
                for r in store.db.execute(
                    "SELECT source,source_mode,start,duration,text FROM transcript_parts ORDER BY source"
                )
            ]
            self.assertEqual(
                parts,
                [
                    ("generate", "whisper", 2, 3, "speech"),
                    ("youtube", "unknown", 1.5, 2.5, "caption"),
                ],
            )
            self.assertTrue(store.cached("v", "youtube", "", language="vi"))
            self.assertTrue(
                store.cached("v", "generate", "medium", source_mode="whisper")
            )
            self.assertFalse(
                store.cached("v", "generate", "medium", source_mode="other-engine")
            )
            self.assertEqual(
                store.db.execute("SELECT note FROM untouched").fetchone()[0], "keep"
            )
            self.assertEqual(
                store.db.execute("PRAGMA foreign_key_check").fetchall(), []
            )
        with contextlib.closing(TranscriptStore(str(self.path))) as store:
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM transcripts").fetchone()[0], 2
            )

    def test_failed_migration_rolls_back_schema_and_contents(self):
        self.v1(orphan=True)
        before = self.path.read_bytes()
        with self.assertRaises(sqlite3.IntegrityError):
            TranscriptStore(str(self.path))
        self.assertEqual(before, self.path.read_bytes())

    def test_engine_modes_and_subtitle_modes_have_independent_segments(self):
        with contextlib.closing(TranscriptStore(str(self.path))) as store:
            store.save_channel(Channel("c", "channel", "url"))
            for source, mode in [
                ("youtube", "manual"),
                ("youtube", "translated"),
                ("generate", "whisper"),
                ("generate", "other-engine"),
            ]:
                store.save_video(
                    Video("v", "c", "title"),
                    source,
                    "model",
                    [Segment(mode, 0, 1)],
                    language="vi",
                    source_mode=mode,
                )
            rows = [
                tuple(r)
                for r in store.db.execute(
                    "SELECT source,source_mode,text FROM transcript_parts ORDER BY source,source_mode"
                )
            ]
            self.assertEqual(
                rows,
                [
                    ("generate", "other-engine", "other-engine"),
                    ("generate", "whisper", "whisper"),
                    ("youtube", "manual", "manual"),
                    ("youtube", "translated", "translated"),
                ],
            )
            self.assertEqual(
                store.db.execute("PRAGMA foreign_key_check").fetchall(), []
            )

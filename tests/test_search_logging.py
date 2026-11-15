"""Search diagnostics explain cache/reload work without recording user content."""

import contextlib
import hashlib
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from video_search.config import SearchConfig
from yt_cli.search_session import SearchSession

from tests.fakes import fake_model, fake_reranker
from tests.sqlite_fixture import SQLiteFixture


class SearchLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "input.sqlite3"
        self.cache = Path(temporary.name) / "cache.sqlite3"
        with contextlib.closing(SQLiteFixture(self.source)) as fixture:
            fixture.db.executescript("""
                INSERT INTO channels VALUES('c','private channel','url');
                INSERT INTO videos VALUES('v','c','private title','');
                INSERT INTO transcripts VALUES('v','vi','youtube','manual','','now');
                INSERT INTO transcript_parts VALUES('v','vi','youtube','manual',0,0,60,'private transcript secret');
            """)
        self.model = fake_model()
        module = types.ModuleType("sentence_transformers")
        setattr(module, "SentenceTransformer", Mock(return_value=self.model))
        setattr(module, "CrossEncoder", Mock(return_value=fake_reranker()))
        patcher = patch.dict("sys.modules", {"sentence_transformers": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = SearchConfig(db=str(self.source), search_db=str(self.cache))

    def test_diagnostics_explain_cache_reload_and_keep_content_private(self):
        before = hashlib.sha256(self.source.read_bytes()).digest()
        events = []
        with self.assertLogs(level="DEBUG") as captured:
            with SearchSession(self.config, on_progress=events.append) as service:
                self.assertEqual(len(service.search("private query secret")), 1)
                service.search("another private question")
            self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)
            # A fresh session exercises persisted splits and vectors.
            with SearchSession(self.config) as service:
                service.search("private query secret")
                with contextlib.closing(sqlite3.connect(self.source)) as writer:
                    writer.execute(
                        "UPDATE transcript_parts SET text='updated private secret'"
                    )
                    writer.commit()
                service.search("private query secret")
        phases = {getattr(record, "phase", None) for record in captured.records}
        self.assertTrue(
            {
                "session_start",
                "schema_validated",
                "corpus_ready",
                "model_ready",
                "split_cache",
                "embedding_cache",
                "embedding_progress",
                "prepare_reuse",
                "corpus_reload",
                "semantic_ready",
                "rerank_ready",
                "query_ready",
                "session_close",
            }.issubset(phases)
        )
        self.assertEqual(events[-1].completed, events[-1].total)
        logs = "\n".join(captured.output)
        self.assertIn("hits=1 misses=0", logs)
        self.assertIn("hits=0 misses=1", logs)
        for secret in (
            "private query",
            "private transcript",
            "private title",
            "private channel",
            "updated private",
        ):
            self.assertNotIn(secret, logs)
        self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_failed_reload_reports_error_type_and_closes_without_exception_text(self):
        with (
            self.assertLogs(level="INFO") as captured,
            self.assertRaisesRegex(RuntimeError, "provider private"),
            SearchSession(self.config) as service,
        ):
            service.search("private query secret")
            with contextlib.closing(sqlite3.connect(self.source)) as writer:
                writer.execute(
                    "UPDATE transcript_parts SET text='updated private secret'"
                )
                writer.commit()
            self.model.encode.side_effect = RuntimeError(
                "provider private query secret"
            )
            service.search("private query secret")
        phases = [getattr(record, "phase", None) for record in captured.records]
        self.assertIn("corpus_reload", phases)
        self.assertIn("query_failed", phases)
        self.assertEqual(phases[-1], "session_close")
        logs = "\n".join(captured.output)
        self.assertIn("RuntimeError", logs)
        self.assertNotIn("provider private", logs)
        self.assertNotIn("private query", logs)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            service.search("anything")

    def test_schema_failure_reports_session_failure_and_cleanup(self):
        with contextlib.closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA user_version=1")
        with (
            self.assertLogs(level="INFO") as captured,
            self.assertRaises(ValueError),
        ):
            SearchSession(self.config)
        phases = [getattr(record, "phase", None) for record in captured.records]
        self.assertIn("session_failed", phases)
        self.assertEqual(phases[-1], "session_close")

    def test_cache_cannot_target_live_input_sidecars(self):
        with contextlib.closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE videos SET description='committed'")
            writer.commit()
            wal = Path(str(self.source) + "-wal")
            before = wal.read_bytes()
            alias = self.source.with_name("input-link.sqlite3")
            alias.symlink_to(self.source)
            for source in (self.source, alias):
                for suffix in ("-wal", "-shm", "-journal"):
                    with (
                        self.subTest(source=source, suffix=suffix),
                        self.assertRaises(ValueError),
                    ):
                        SearchConfig(
                            db=str(source), search_db=str(source) + suffix
                        ).validate()
            self.assertEqual(wal.read_bytes(), before)
            self.assertEqual(writer.execute("PRAGMA quick_check").fetchone(), ("ok",))

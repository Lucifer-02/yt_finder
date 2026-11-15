"""Application APIs work without CLI, terminal streams, network or real models."""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import types
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from transcript_collector.config import CollectionRequest
from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.errors import SourceBlocked
from transcript_collector.events import EventDeliveryError
from transcript_collector.store import TranscriptStore
from video_search.config import SearchConfig
from video_search.models import DEFAULT_RERANKER
from yt_cli.collector_runtime import collect
from yt_cli.search_session import SearchSession

from tests.fakes import fake_model, fake_reranker
from tests.sqlite_fixture import SQLiteFixture


class CollectionRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "collector.sqlite3"
        self.channel = Channel(
            "UCtest", "Channel", "https://www.youtube.com/channel/UCtest"
        )
        self.video = Video("AbCdEf12_-3", self.channel.id, "Video")
        self.source = Mock(transcript_language="vi", transcript_mode="manual")
        self.source.resolve.return_value = self.channel
        self.source.resolve_video.return_value = self.channel, self.video
        self.source.videos.return_value = [self.video]
        self.source.playlist_videos.return_value = [self.video.id]
        self.source.transcript.return_value = [Segment("caption", 0, 2)]
        self.enterContext(
            patch("yt_cli.collector_runtime.YouTube", return_value=self.source)
        )

    def request(self, target="@channel", **kwargs):
        return CollectionRequest(
            target=target, mode="direct", db=str(self.path), language="vi", **kwargs
        )

    def test_every_scope_works_silently_and_returns_serializable_events_and_results(
        self,
    ):
        environment = dict(os.environ)
        stdout, stderr = io.StringIO(), io.StringIO()
        targets = (
            ("@channel", "channel /videos"),
            (f"https://www.youtube.com/watch?v={self.video.id}", "single video"),
            ("https://www.youtube.com/playlist?list=PL_test", "playlist"),
        )
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            for target, scope in targets:
                events = []
                result = collect(self.request(target), on_event=events.append)
                self.assertEqual(result.scope, scope)
                self.assertFalse(result.failed)
                finished = [event for event in events if event.kind == "video_finished"]
                self.assertEqual(len(finished), 1)
                self.assertEqual(finished[0].video_id, self.video.id)
                self.assertEqual(events[-1].kind, "summary")
                self.assertEqual(events[-1].counts, result.counts)
                json.dumps(asdict(result))
                json.dumps([asdict(event) for event in events])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(dict(os.environ), environment)
        self.source.transcript.assert_called_once()

    def test_invalid_requests_fail_before_source_engine_or_database_creation(self):
        requests = (
            CollectionRequest("@channel", "direct"),
            CollectionRequest("@channel", "direct", language="vi,en"),
            CollectionRequest("@channel", "direct", language="vi", engine="whisper"),
            CollectionRequest("@channel", "generate", language="vi"),
            CollectionRequest("", "direct", language="vi"),
            CollectionRequest("@channel", "other"),
        )
        with (
            patch("yt_cli.collector_runtime.YouTube") as source,
            patch("yt_cli.collector_runtime.create_engine") as engine,
            patch("yt_cli.collector_runtime.TranscriptStore") as store,
        ):
            for request in requests:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    collect(request)
            source.assert_not_called()
            engine.assert_not_called()
            store.assert_not_called()

    def test_blocking_is_returned_with_counts_and_closes_owned_store(self):
        stores = []

        def open_store(path):
            store = TranscriptStore(path)
            stores.append(store)
            return store

        self.source.transcript.side_effect = SourceBlocked("429")
        with patch("yt_cli.collector_runtime.TranscriptStore", side_effect=open_store):
            result = collect(self.request())
        self.assertTrue(result.failed)
        self.assertEqual(result.counts["blocked"], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            stores[0].db.execute("SELECT 1")

    def test_observer_failure_is_not_misclassified_and_committed_work_survives(self):
        self.source.videos.return_value += [Video("second", self.channel.id, "Second")]

        def observer(event):
            if event.kind == "video_finished":
                raise ValueError("event sink unavailable")

        with self.assertRaises(EventDeliveryError) as exc:
            collect(self.request(), on_event=observer)
        self.assertIsInstance(exc.exception.__cause__, ValueError)
        self.source.transcript.assert_called_once()
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT video_id FROM transcripts").fetchall(),
                [(self.video.id,)],
            )

    def test_interrupt_closes_store_cleans_audio_and_keeps_previous_commit(self):
        self.source.videos.return_value += [Video("second", self.channel.id, "Second")]
        engine = Mock(config="model", transcript_language="en")
        engine.name = "whisper"
        engine.transcript.side_effect = [
            [Segment("generated", 0, 2)],
            KeyboardInterrupt,
        ]
        audio_paths = []

        def download(video_id, directory):
            path = Path(directory) / "audio.webm"
            path.write_bytes(b"audio")
            audio_paths.append(path)
            return path

        self.source.download_audio.side_effect = download
        request = CollectionRequest("@channel", "generate", db=str(self.path))
        with (
            patch("yt_cli.collector_runtime.create_engine", return_value=engine),
            self.assertRaises(KeyboardInterrupt),
        ):
            collect(request)
        self.assertEqual(len(audio_paths), 2)
        self.assertTrue(all(not path.parent.exists() for path in audio_paths))
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT video_id FROM transcripts").fetchall(),
                [(self.video.id,)],
            )


class SearchSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "transcripts.sqlite3"
        self.cache = self.root / "search.sqlite3"
        with contextlib.closing(SQLiteFixture(self.source)) as fixture:
            fixture.db.executescript("""
                INSERT INTO channels VALUES('c','channel','url');
                INSERT INTO videos VALUES('v','c','title','');
                INSERT INTO transcripts VALUES('v','vi','youtube','manual','','timestamp');
                INSERT INTO transcript_parts VALUES('v','vi','youtube','manual',0,0,60,'initial');
            """)
        module = types.ModuleType("sentence_transformers")
        self.model = fake_model()
        self.loader = Mock(return_value=self.model)
        self.reranker = fake_reranker()
        self.reranker_loader = Mock(return_value=self.reranker)
        setattr(module, "SentenceTransformer", self.loader)
        setattr(module, "CrossEncoder", self.reranker_loader)
        self.enterContext(patch.dict("sys.modules", {"sentence_transformers": module}))
        self.config = SearchConfig(db=str(self.source), search_db=str(self.cache))

    def test_reuses_model_returns_data_and_never_writes_transcript_or_environment(self):
        before = hashlib.sha256(self.source.read_bytes()).digest()
        environment = dict(os.environ)
        stdout, stderr, events = io.StringIO(), io.StringIO(), []
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            SearchSession(self.config, on_progress=events.append) as session,
        ):
            first = session.search("first")
            second = session.search("second")
            self.assertEqual(first[0]["video_id"], "v")
            self.assertEqual(second[0]["source_mode"], "manual")
            json.dumps(second)
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(
            [asdict(event) for event in events], [{"completed": 1, "total": 1}]
        )
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        session.close()  # Idempotent, including after a context exit.
        with self.assertRaisesRegex(RuntimeError, "closed"):
            session.search("after close")

    def test_search_modes_resolve_model_and_rerank_defaults(self):
        before = hashlib.sha256(self.source.read_bytes()).digest()
        for mode, query in (("keyword", "initial"), ("exact", "init")):
            with self.subTest(mode=mode):
                with SearchSession(replace(self.config, mode=mode)) as session:
                    self.assertEqual(session.search(query)[0]["video_id"], "v")
                    self.assertEqual(session.search("absent"), [])
                self.loader.assert_not_called()
                self.reranker_loader.assert_not_called()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)
        for bad in (
            SearchConfig(mode="unknown"),
            SearchConfig(mode="fuzzy"),
            SearchConfig(mode="semantic", rerank=True),
            SearchConfig(mode="keyword", rerank=True),
            SearchConfig(mode="exact", rerank=True),
        ):
            with self.subTest(config=bad), self.assertRaises(ValueError):
                SearchSession(bad)
        for rerank in (None, False):
            with self.subTest(rerank=rerank):
                self.reranker_loader.reset_mock()
                with SearchSession(replace(self.config, rerank=rerank)) as session:
                    session.search("initial")
                if rerank is None:
                    self.reranker_loader.assert_called_once_with(
                        DEFAULT_RERANKER, device=None, max_length=512
                    )
                else:
                    self.reranker_loader.assert_not_called()

    def test_session_detects_new_collector_commit(self):
        with SearchSession(self.config) as session:
            self.assertEqual(session.search("first")[0]["text"], "initial")
            with contextlib.closing(sqlite3.connect(self.source)) as writer, writer:
                writer.execute("UPDATE transcript_parts SET text='updated'")
            self.assertEqual(session.search("second")[0]["text"], "updated")
        self.assertEqual(self.loader.call_count, 1)

    def test_aliases_are_rejected_by_api_before_any_cache_write(self):
        before = self.source.read_bytes()
        paths = [self.source, self.root / "symlink", self.root / "hardlink"]
        paths[1].symlink_to(self.source)
        paths[2].hardlink_to(self.source)
        with patch("yt_cli.search_session.EmbeddingStore") as cache:
            for path in paths:
                config = replace(self.config, search_db=str(path))
                with (
                    self.subTest(path=path),
                    self.assertRaisesRegex(ValueError, "different"),
                ):
                    SearchSession(config)
            cache.assert_not_called()
        self.assertEqual(self.source.read_bytes(), before)

    def test_constructor_failure_closes_both_opened_connections(self):
        corpus, cache = Mock(), Mock()
        with (
            patch("yt_cli.search_session.Corpus", return_value=corpus),
            patch("yt_cli.search_session.EmbeddingStore", return_value=cache),
            patch(
                "yt_cli.search_session.SearchEngine",
                side_effect=RuntimeError("setup failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "setup failed"),
        ):
            SearchSession(self.config)
        corpus.close.assert_called_once()
        cache.close.assert_called_once()

    def test_invalid_filters_and_limits_fail_before_model_load(self):
        with self.assertRaises(ValueError):
            SearchSession(replace(self.config, transcript_source="legacy"))
        with SearchSession(self.config) as session:
            for query, top, minimum in (
                ("", 5, None),
                ("query", 0, None),
                ("query", 5, 2.0),
            ):
                with self.subTest(query=query, top=top), self.assertRaises(ValueError):
                    session.search(query, top, minimum)
        self.loader.assert_not_called()

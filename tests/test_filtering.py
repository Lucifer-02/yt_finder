"""Selection and ranking are independently usable across the video-list boundary."""

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import types
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch

from video_search.cache import EmbeddingStore
from video_search.corpus import Corpus
from video_search.filtering import VideoFilter, VideoFilters
from video_search.search import SearchEngine
from yt_cli.search import main as search_main

from tests.fakes import fake_model
from tests.sqlite_fixture import SQLiteFixture


class FilteringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "transcripts.sqlite3"
        with contextlib.closing(SQLiteFixture(self.path)) as fixture:
            fixture.db.executescript("""
                INSERT INTO channels VALUES('c1','channel one','https://youtube.com/channel/c1');
                INSERT INTO channels VALUES('c2','channel two','https://youtube.com/channel/c2');
                INSERT INTO videos VALUES('a','c1','first','description a');
                INSERT INTO videos VALUES('b','c2','second','description b');
                INSERT INTO videos VALUES('inventory','c1','no transcript','');
                INSERT INTO transcripts VALUES('a','vi','youtube','manual','','now');
                INSERT INTO transcripts VALUES('a','vi','youtube','auto_generated','','now');
                INSERT INTO transcripts VALUES('a','en-US','youtube','manual','','now');
                INSERT INTO transcripts VALUES('b','vi','youtube','manual','','now');
                INSERT INTO transcript_parts VALUES('a','vi','youtube','manual',0,120,5,'insulin text');
                INSERT INTO transcript_parts VALUES('a','vi','youtube','auto_generated',0,120,5,'automatic text');
                INSERT INTO transcript_parts VALUES('a','en-US','youtube','manual',0,120,5,'English text');
                INSERT INTO transcript_parts VALUES('b','vi','youtube','manual',0,120,5,'insulin text');
            """)
        self.corpus = Corpus(str(self.path))
        self.addCleanup(self.corpus.close)
        self.selector = VideoFilter(self.corpus)

    def test_ids_intersect_filters_and_empty_or_unknown_ids_never_widen_scope(self):
        self.assertEqual([v.video_id for v in self.selector.select()], ["a", "b"])
        selected = self.selector.select(
            VideoFilters(channel="channel one", video_ids=("a", "b"))
        )
        self.assertEqual([v.video_id for v in selected], ["a"])
        self.assertEqual(selected[0].description, "description a")
        self.assertEqual(selected[0].source_mode, "manual")
        self.assertEqual(selected[0].segments[0].start, 120)
        for ids in ((), ("missing",), ("inventory",)):
            self.assertEqual(self.selector.select(VideoFilters(video_ids=ids)), [])
        ids = tuple(f"missing-{i}" for i in range(1200)) + ("b", "b")
        self.assertEqual(
            [v.video_id for v in self.selector.select(VideoFilters(video_ids=ids))],
            ["b"],
        )
        self.assertEqual(
            self.selector.select(VideoFilters(video_ids=("a",), language="en"))[
                0
            ].language,
            "en-US",
        )
        self.assertEqual(
            self.selector.select(
                VideoFilters(video_ids=("a",), source_mode="auto_generated")
            )[0]
            .segments[0]
            .text,
            "automatic text",
        )

    def test_selection_returns_immutable_snapshots_and_refreshes_after_commit(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        selected = self.selector.select(VideoFilters(video_ids=("a",)))
        selected.clear()
        old = self.selector.select(VideoFilters(video_ids=("a",)))
        self.assertEqual(len(old), 1)
        with self.assertRaises(FrozenInstanceError):
            setattr(old[0], "title", "changed")
        with self.assertRaises(FrozenInstanceError):
            setattr(old[0].segments[0], "text", "changed")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)
        with contextlib.closing(sqlite3.connect(self.path)) as writer, writer:
            writer.execute(
                "UPDATE transcript_parts SET text='new text' WHERE video_id='a' AND language='vi'"
            )
        new = self.selector.select(VideoFilters(video_ids=("a",)))
        self.assertEqual(old[0].segments[0].text, "insulin text")
        self.assertEqual(new[0].segments[0].text, "new text")

    def test_ranker_searches_only_supplied_videos_after_database_is_closed(self):
        a = self.selector.select(VideoFilters(video_ids=("a",)))
        b = self.selector.select(VideoFilters(video_ids=("b",)))
        self.corpus.close()
        model = fake_model()
        module = types.ModuleType("sentence_transformers")
        loader = Mock(return_value=model)
        setattr(module, "SentenceTransformer", loader)
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            for mode in ("keyword", "semantic"):
                with (
                    self.subTest(mode=mode),
                    contextlib.closing(EmbeddingStore(":memory:")) as cache,
                ):
                    engine = SearchEngine(cache, mode=mode)
                    for videos, expected in ((a, "a"), (b, "b"), ([], None), (a, "a")):
                        found = engine.search("insulin", videos)
                        self.assertEqual(
                            [r["video_id"] for r in found],
                            [expected] if expected else [],
                        )
                        if found:
                            self.assertEqual(found[0]["start"], 120)
                    if mode == "keyword":
                        loader.assert_not_called()
        # Identical transcript text with distinct video titles produces two distinct vectors plus three queries.
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(model.encode.call_count, 5)

    def test_cli_repeated_video_id_selects_only_requested_videos(self):
        for ids, expected in (
            (("a",), ["a"]),
            (("b", "a", "b"), ["a", "b"]),
            (("missing",), []),
        ):
            output = io.StringIO()
            arguments = [
                "insulin",
                "--mode",
                "keyword",
                "--json",
                "--db",
                str(self.path),
                "--search-db",
                str(self.root / "cache.sqlite3"),
                "--log-file",
                str(self.root / "cli.jsonl"),
            ]
            for video_id in ids:
                arguments.extend(["--video-id", video_id])
            with (
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                search_main(arguments)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["video_ids"], list(ids))
            self.assertEqual(
                sorted(r["video_id"] for r in payload["results"]), expected
            )

    def test_blank_ids_are_rejected_before_database_or_model_is_opened(self):
        for ids in (("",), (" ",)):
            with self.assertRaises(ValueError):
                self.selector.select(VideoFilters(video_ids=ids))

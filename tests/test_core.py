import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.youtube import (
    YouTube,
    _ExtractLogger,
)
from video_search.cache import EmbeddingStore
from video_search.corpus import Corpus
from video_search.documents import Window
from video_search.filtering import VideoFilter
from video_search.search import SearchEngine, lexical_scores, search
from yt_cli.search import main as search_main

from tests.fakes import fake_model, fake_reranker
from tests.sqlite_fixture import SQLiteFixture


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "transcripts.sqlite3"
        self.store = SQLiteFixture(str(self.db_path))
        self.channel = Channel(
            "UCtest", "Kênh thử", "https://youtube.com/channel/UCtest"
        )
        self.store.save_channel(self.channel)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, video_id, segments, channel_id="UCtest", source="youtube"):
        self.store.save_video(
            Video(video_id, channel_id, video_id), source, "vi,en", segments
        )

    def test_search_windows_keep_complete_timed_transcript(self):
        segments = [Segment(str(i), i * 10, 5) for i in range(10)]
        self.save("v", segments)
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            windows = corpus.windows("UCtest")
            self.assertEqual([p["start"] for p in windows], [0, 30, 60, 90])
            self.assertEqual(windows[0]["text"], "0 1 2 3 4 5")
            self.assertEqual(windows[1]["text"], "3 4 5 6 7 8")

    def test_two_versions_are_independent_and_search_selects_one(self):
        self.save("v", [Segment("youtube text", 10, 3)], source="youtube")
        self.save("v", [Segment("whisper text", 20, 4)], source="whisper")
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            self.assertEqual(
                [(w["transcript_source"], w["text"]) for w in corpus.windows()],
                [("youtube", "youtube text")],
            )
            self.assertEqual(
                [
                    (w["transcript_source"], w["text"])
                    for w in corpus.windows(source="generate")
                ],
                [("generate", "whisper text")],
            )
            self.save("v", [Segment("youtube refreshed", 30, 3)], source="youtube")
            self.assertEqual(
                corpus.windows(source="youtube")[0]["text"], "youtube refreshed"
            )
            self.assertEqual(
                corpus.windows(source="generate")[0]["text"], "whisper text"
            )

    def test_source_priority_is_per_video_and_never_combines_versions(self):
        self.save("one", [Segment("youtube one", 1, 2)], source="youtube")
        self.save("one", [Segment("whisper one", 1, 2)], source="whisper")
        self.save("two", [Segment("whisper two", 1, 2)], source="whisper")
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            selected = {
                w["video_id"]: (w["transcript_source"], w["text"])
                for w in corpus.windows()
            }
            self.assertEqual(
                selected,
                {"one": ("youtube", "youtube one"), "two": ("generate", "whisper two")},
            )
            self.assertEqual(
                {w["video_id"] for w in corpus.windows(source="youtube")}, {"one"}
            )
            self.assertEqual(
                {w["video_id"] for w in corpus.windows(source="generate")},
                {"one", "two"},
            )

    def test_unused_database_tables_are_ignored_and_not_touched(self):
        self.store.db.execute("CREATE TABLE passages(id INTEGER, text TEXT)")
        self.store.db.execute("INSERT INTO passages VALUES (1, 'unused')")
        self.store.db.execute(
            "CREATE TABLE embeddings(passage_id INTEGER, vector TEXT)"
        )
        self.store.db.execute("INSERT INTO embeddings VALUES (1, '[0.2]')")
        self.store.db.commit()
        self.save("v", [Segment("new transcript", 1, 2)])
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            self.assertEqual(corpus.windows()[0]["text"], "new transcript")
        self.assertEqual(
            self.store.db.execute("SELECT text FROM passages").fetchone()[0], "unused"
        )
        self.assertEqual(
            self.store.db.execute("SELECT vector FROM embeddings").fetchone()[0],
            "[0.2]",
        )

    def test_search_ignores_unversioned_parts_without_changing_them(self):
        self.store.db.executescript("""
            CREATE TABLE transcript_segments(video_id TEXT, position INTEGER,
                                              start REAL, duration REAL, text TEXT);
            INSERT INTO videos VALUES('v','UCtest','Video','');
            INSERT INTO transcript_segments VALUES('v',0,10,2,'unknown source');
        """)
        statements = []
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            corpus.db.set_trace_callback(statements.append)
            self.assertEqual(corpus.windows(), [])
        self.assertFalse(any("transcript_segments" in sql for sql in statements))
        self.save("v", [Segment("whisper text", 20, 2)], source="whisper")
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            self.assertEqual(corpus.windows()[0]["transcript_source"], "generate")
            self.assertEqual(corpus.windows(source="youtube"), [])
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).digest(), before)
        self.assertEqual(
            self.store.db.execute("SELECT text FROM transcript_segments").fetchone()[0],
            "unknown source",
        )

    def test_channel_resolution_and_scope(self):
        self.store.save_channel(
            Channel("other", "Kênh khác", "https://example.org/other")
        )
        self.save("v1", [Segment("one", 1, 2)])
        self.save("v2", [Segment("two", 1, 2)], "other")
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            self.assertEqual(corpus.channel_id("Kênh khác"), "other")
            self.assertEqual([w["video_id"] for w in corpus.windows("other")], ["v2"])
            self.assertEqual(len(corpus.windows()), 2)

    def test_channel_lists_only_videos_tab_and_skips_live_entries(self):
        source = YouTube("vi")
        with patch.object(
            source,
            "_extract",
            return_value={
                "entries": [
                    {"id": "live", "live_status": "is_live"},
                    {"id": "upcoming", "live_status": "is_upcoming"},
                    {"id": "live_flag", "is_live": True},
                    {"id": "done", "live_status": "was_live"},
                    {"id": "regular", "live_status": "not_live"},
                ]
            },
        ) as extract:
            self.assertEqual(
                [v.id for v in source.videos(self.channel)], ["done", "regular"]
            )
        extract.assert_called_once_with(
            self.channel.url + "/videos", noplaylist=False, extract_flat=True
        )

    def test_missing_tab_message_is_quiet_but_other_errors_are_visible(self):
        with self.assertLogs("transcript_collector.youtube", level="WARNING") as output:
            logger = _ExtractLogger()
            logger.error("ERROR: This channel does not have a streams tab")
            logger.error("ERROR: network unavailable")
        messages = "\n".join(output.output)
        self.assertNotIn("streams tab", messages)
        self.assertIn("temporary network failure", messages)

    def test_one_shot_search_reuses_cached_vectors_and_ranks_best_moment(self):
        self.save("v", [Segment("first", 0, 5), Segment("second", 120, 5)])
        self.save("other", [Segment("other", 0, 5)])
        corpus = Corpus(str(self.db_path))
        cache = EmbeddingStore(str(self.root / "vectors.sqlite3"))
        model = fake_model()

        def encode(texts, **kwargs):
            return np.array(
                [
                    [1.0, 0.0] if ("second" in t or t == "query") else [0.0, 1.0]
                    for t in texts
                ],
                dtype=np.float32,
            )

        model.encode.side_effect = encode
        module = types.ModuleType("sentence_transformers")
        setattr(module, "SentenceTransformer", Mock(return_value=model))
        setattr(module, "CrossEncoder", Mock(return_value=fake_reranker()))
        with contextlib.closing(corpus), contextlib.closing(cache):
            with patch.dict("sys.modules", {"sentence_transformers": module}):
                first = search(
                    VideoFilter(corpus).select(),
                    cache,
                    "query",
                    top=2,
                    model_name="test",
                )
                second = search(
                    VideoFilter(corpus).select(),
                    cache,
                    "query",
                    top=2,
                    model_name="test",
                )
            self.assertEqual([r["video_id"] for r in first], ["v", "other"])
            self.assertEqual(first[0]["start"], 120)
            self.assertTrue(first[0]["url"].endswith("&t=120s"))
            self.assertEqual(first, second)
            self.assertEqual(model.encode.call_count, 3)

    def test_keyword_score_supports_vietnamese_without_diacritics(self):
        self.save("v", [Segment("giải thích hố đen", 0, 5)])
        self.save("other", [Segment("nấu cơm", 0, 5)])
        with contextlib.closing(Corpus(str(self.db_path))) as corpus:
            windows = corpus.windows()
            scores = lexical_scores("ho den", windows)
            by_id = {
                window["video_id"]: score for window, score in zip(windows, scores)
            }
            self.assertGreater(by_id["v"], by_id["other"])

    def test_changed_transcript_does_not_reuse_old_vector(self):
        self.save("v", [Segment("old", 1, 2)])
        corpus = Corpus(str(self.db_path))
        cache = EmbeddingStore(str(self.root / "vectors.sqlite3"))
        old_hash = hashlib.sha256(b"old").hexdigest()
        cache.save_vectors([(old_hash, [1.0, 0.0])], "test", "old-config")
        self.save("v", [Segment("new", 1, 2)])
        model = fake_model()
        model.encode.side_effect = None
        model.encode.return_value = np.array([[1.0, 0.0]], dtype=np.float32)
        module = types.ModuleType("sentence_transformers")
        setattr(module, "SentenceTransformer", Mock(return_value=model))
        setattr(module, "CrossEncoder", Mock(return_value=fake_reranker()))
        with contextlib.closing(corpus), contextlib.closing(cache):
            with patch.dict("sys.modules", {"sentence_transformers": module}):
                self.assertEqual(
                    search(
                        VideoFilter(corpus).select(), cache, "query", model_name="test"
                    )[0]["text"],
                    "new",
                )
            self.assertEqual(model.encode.call_count, 2)

    def test_partial_embedding_progress_is_cached(self):
        passages: list[Window] = [
            {
                "video_id": str(i),
                "channel_id": "UCtest",
                "channel_title": "Kênh thử",
                "title": "",
                "transcript_source": "youtube",
                "source_mode": "manual",
                "language": "vi",
                "transcript_config": "",
                "start": 0.0,
                "end": 60.0,
                "text": f"window {i}",
            }
            for i in range(257)
        ]
        cache = EmbeddingStore(str(self.root / "partial.sqlite3"))
        first_batch = np.array([[1.0, 0.0]] * 256, dtype=np.float32)
        model = fake_model()
        model.encode.side_effect = [first_batch, RuntimeError("interrupted")]
        module = types.ModuleType("sentence_transformers")
        setattr(module, "SentenceTransformer", Mock(return_value=model))
        with contextlib.closing(cache):
            with (
                patch.dict("sys.modules", {"sentence_transformers": module}),
                self.assertRaisesRegex(RuntimeError, "interrupted"),
            ):
                SearchEngine(cache, model_name="test").search_windows("query", passages)
            self.assertEqual(
                cache.db.execute("SELECT count(*) FROM embedding_vectors").fetchone()[
                    0
                ],
                256,
            )

    def test_search_cli_writes_only_vector_database(self):
        self.save("v", [Segment("hố đen", 1, 2)])
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        cache_path = self.root / "vectors.sqlite3"
        model = fake_model()
        model.encode.side_effect = None
        model.encode.return_value = np.array([[1.0, 0.0]], dtype=np.float32)
        module = types.ModuleType("sentence_transformers")
        setattr(module, "SentenceTransformer", Mock(return_value=model))
        setattr(module, "CrossEncoder", Mock(return_value=fake_reranker()))
        output = io.StringIO()
        with (
            patch.dict("sys.modules", {"sentence_transformers": module}),
            contextlib.redirect_stdout(output),
        ):
            search_main(
                [
                    "hố đen",
                    "--db",
                    str(self.db_path),
                    "--search-db",
                    str(cache_path),
                    "--json",
                ]
            )
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).digest(), before)
        self.assertEqual(json.loads(output.getvalue())["results"][0]["video_id"], "v")
        with contextlib.closing(sqlite3.connect(cache_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0], 1
            )

    def test_same_source_and_cache_file_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            search_main(
                ["query", "--db", str(self.db_path), "--search-db", str(self.db_path)]
            )

    def test_hardlinked_source_and_cache_file_rejected(self):
        alias = self.root / "alias.sqlite3"
        os.link(self.db_path, alias)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            search_main(["query", "--db", str(self.db_path), "--search-db", str(alias)])


if __name__ == "__main__":
    unittest.main()

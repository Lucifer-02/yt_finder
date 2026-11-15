"""Multiple ranked moments per video, overlap suppression and CLI presentation."""

import contextlib
import io
import json
import math
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import numpy as np
from video_search.cache import EmbeddingStore
from video_search.documents import TranscriptSegment, VideoTranscript
from video_search.search import SearchEngine, SearchMoment, SearchResult
from yt_cli.interactive import InteractiveUI, show_result
from yt_cli.search import main as search_main

from tests import test_interactive
from tests.fakes import fake_model, fake_reranker
from tests.sqlite_fixture import SQLiteFixture
from tests.test_interactive import result
from tests.test_search import window


class MultipleMomentTests(unittest.TestCase):
    def setUp(self):
        self.cache = EmbeddingStore(":memory:")
        self.addCleanup(self.cache.close)
        self.model = fake_model()
        self.scores = {
            "best": 0.99,
            "overlap": 0.98,
            "other": 0.95,
            "excluded": 0.9,
            "later": 0.85,
            "otherlater": 0.8,
            "far": 0.7,
            "excess": 0.6,
        }
        self.model.encode.side_effect = lambda texts, **kwargs: np.array(
            [
                [1.0, 0.0]
                if text == "query"
                else [self.scores[text], math.sqrt(1 - self.scores[text] ** 2)]
                for text in texts
            ],
            dtype=np.float32,
        )
        module = types.ModuleType("sentence_transformers")
        setattr(module, "SentenceTransformer", Mock(return_value=self.model))
        patcher = patch.dict("sys.modules", {"sentence_transformers": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.docs = [
            window("best", "a", 0),
            window("overlap", "a", 30),
            window("later", "a", 120),
            window("far", "a", 600),
            window("excess", "a", 900),
            window("other", "b", 0),
            window("otherlater", "b", 120),
            window("excluded", "c", 0),
        ]

    def test_top_limits_videos_and_later_candidates_fill_each_video_in_both_modes(self):
        for mode in ("semantic", "keyword"):
            engine = SearchEngine(self.cache, mode=mode)
            lexical = np.array([self.scores[doc["text"]] for doc in self.docs])
            with (
                self.subTest(mode=mode),
                patch.object(self.cache, "lexical_scores", return_value=lexical),
            ):
                found = engine.search_windows(
                    "query", self.docs, top=2, moments_per_video=3
                )
                self.assertEqual([r["video_id"] for r in found], ["a", "b"])
                self.assertEqual(
                    [m["start"] for m in found[0]["moments"]], [0, 120, 600]
                )
                self.assertEqual([m["start"] for m in found[1]["moments"]], [0, 120])
                for video in found:
                    best = video["moments"][0]
                    for key in ("start", "end", "text", "url", "score", "similarity"):
                        self.assertEqual(video[key], best[key])
                    self.assertTrue(
                        all(
                            m["url"].endswith(f"&t={int(m['start'])}s")
                            for m in video["moments"]
                        )
                    )
                single = engine.search_windows(
                    "query", self.docs, top=1, moments_per_video=1
                )
                self.assertEqual(len(single), 1)
                self.assertEqual(len(single[0]["moments"]), 1)

    def test_semantic_cutoff_applies_to_each_moment_and_eval_defaults_to_one(self):
        engine = SearchEngine(self.cache)
        found = engine.search_windows(
            "query", self.docs, top=2, moments_per_video=3, min_similarity=0.82
        )
        self.assertEqual([m["start"] for m in found[0]["moments"]], [0, 120])
        self.assertEqual(len(found[1]["moments"]), 1)
        frozen = engine.search_windows("query", self.docs, top=2)
        self.assertEqual([len(v["moments"]) for v in frozen], [1, 1])

    def test_overlap_chain_keeps_separate_endpoints_and_uses_actual_end_times(self):
        engine = SearchEngine(self.cache)
        docs = [
            window("best", "a", 0),
            window("overlap", "a", 30),
            window("later", "a", 60),
        ]
        found = engine.search_windows("query", docs, moments_per_video=3)
        self.assertEqual([m["start"] for m in found[0]["moments"]], [0, 60])
        docs[0]["end"] = 150
        docs.append(window("far", "a", 240))
        found = engine.search_windows("query", docs, moments_per_video=3)
        self.assertEqual([m["start"] for m in found[0]["moments"]], [0, 240])

    def test_public_video_api_defaults_to_three_and_empty_scope_has_no_stale_results(
        self,
    ):
        video = VideoTranscript(
            video_id="a",
            channel_id="c",
            channel_title="channel",
            title="title",
            description="",
            language="vi",
            transcript_source="youtube",
            source_mode="manual",
            transcript_config="",
            segments=tuple(
                TranscriptSegment("needle", start, 5) for start in (0, 120, 600, 900)
            ),
        )
        engine = SearchEngine(self.cache, mode="keyword")
        found = engine.search("needle", [video], top=1)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["moments"]), 3)
        self.assertTrue(all(m["similarity"] is None for m in found[0]["moments"]))
        self.assertEqual(engine.search("needle", []), [])
        self.assertEqual(
            len(engine.search("needle", [video], moments_per_video=2)[0]["moments"]), 2
        )
        self.assertEqual(engine.search("missing", [video]), [])

    def test_invalid_moment_limit_fails_before_preparation(self):
        engine = SearchEngine(self.cache)
        with patch.object(engine, "prepare") as prepare:
            for limit in (0, -1):
                with self.assertRaises(ValueError):
                    engine.search("query", [], moments_per_video=limit)
            prepare.assert_not_called()

    def test_rerank_respects_requested_moment_limit_above_three(self):
        docs = [
            window(text, video, i * 120)
            for video in ("a", "b")
            for i, text in enumerate(("best", "later", "far", "excess", "other"))
        ]
        with patch(
            "sentence_transformers.CrossEncoder",
            return_value=fake_reranker(),
            create=True,
        ):
            engine = SearchEngine(self.cache, reranker_name="test")
            for limit in (1, 3, 5):
                with self.subTest(limit=limit):
                    found = engine.search_windows(
                        "query", docs, top=1, moments_per_video=limit
                    )
                    self.assertEqual(len(found), 1)
                    self.assertEqual(len(found[0]["moments"]), limit)
                    self.assertTrue(
                        all("rerank_score" in m for m in found[0]["moments"])
                    )


class MultipleMomentCLITests(unittest.TestCase):
    def test_cli_groups_distant_moments_in_json_and_text_and_preserves_top(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with contextlib.closing(SQLiteFixture(root / "input.sqlite3")) as fixture:
                fixture.db.executescript("""
                    INSERT INTO channels VALUES('c','channel','url');
                    INSERT INTO videos VALUES('a','c','title a','');
                    INSERT INTO videos VALUES('b','c','title b','');
                    INSERT INTO transcripts VALUES('a','vi','youtube','manual','','now');
                    INSERT INTO transcripts VALUES('b','vi','youtube','manual','','now');
                """)
                for video in ("a", "b"):
                    fixture.db.executemany(
                        "INSERT INTO transcript_parts VALUES(?,?,?,?,?,?,?,?)",
                        [
                            (video, "vi", "youtube", "manual", i, start, 5, "needle")
                            for i, start in enumerate((0, 120, 600, 900))
                        ],
                    )
                fixture.db.commit()
            args = [
                "needle",
                "--mode",
                "keyword",
                "--db",
                str(root / "input.sqlite3"),
                "--search-db",
                str(root / "cache.sqlite3"),
                "--log-file",
                str(root / "cli.jsonl"),
                "--top",
                "1",
                "--moments-per-video",
                "2",
            ]
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                search_main([*args, "--json"])
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["moments_per_video"], 2)
            self.assertEqual(len(payload["results"]), 1)
            self.assertEqual(len(payload["results"][0]["moments"]), 2)
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                search_main(args)
            self.assertEqual(output.getvalue().count("Đoạn "), 2)
            self.assertEqual(
                output.getvalue().count("https://www.youtube.com/watch?v="), 2
            )
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                search_main([*args, "--moments-per-video", "0"])
            self.assertEqual(error.exception.code, 2)

    def test_show_can_display_one_moment_or_all_and_interactive_changes_limit(self):
        item: SearchResult = result()
        first: SearchMoment = {
            "start": item["start"],
            "end": item["end"],
            "text": item["text"],
            "score": item["score"],
            "similarity": item["similarity"],
            "url": item["url"],
        }
        second: SearchMoment = {
            **first,
            "start": 900.0,
            "end": 960.0,
            "text": "SECOND MOMENT FULL",
            "url": "https://www.youtube.com/watch?v=video&t=900s",
        }
        item["moments"] = [first, second]
        ui = InteractiveUI()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            show_result(ui, ["/show", "1", "2"], [item], "query")
        self.assertIn("SECOND MOMENT FULL", output.getvalue())
        self.assertNotIn("PHẦN CUỐI.", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            show_result(ui, ["/show", "1"], [item], "query")
        self.assertIn("PHẦN CUỐI.", output.getvalue())
        self.assertIn("SECOND MOMENT FULL", output.getvalue())
        engine = Mock()
        engine.search.return_value = [item]
        test_interactive.InteractiveTests().run_session(
            engine, ["first", "/moments 1", "second", "/quit"]
        )
        self.assertEqual(
            engine.search.call_args_list,
            [
                call("first", 5, None, moments_per_video=3),
                call("second", 5, None, moments_per_video=1),
            ],
        )

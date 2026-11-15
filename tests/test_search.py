import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from video_search.cache import EmbeddingStore, tokens
from video_search.corpus import Corpus, Window
from video_search.filtering import VideoFilter
from video_search.models import DEFAULT_RERANKER, RetrievalModel, split_texts
from video_search.search import SearchEngine, exact_scores
from yt_cli.search import main as search_main
from yt_workflows.evaluation import (
    FixedCorpus,
    metrics,
    select_windows,
    validate_evidence,
    validate_output_paths,
)

from tests.fakes import FakeTokenizer, fake_model, fake_reranker
from tests.sqlite_fixture import SQLiteFixture


def window(text, video="v", start=0, title="") -> Window:
    return {
        "video_id": video,
        "channel_id": "c",
        "channel_title": "channel",
        "title": title,
        "transcript_source": "youtube",
        "source_mode": "manual",
        "language": "vi",
        "transcript_config": "",
        "start": float(start),
        "end": float(start + 60),
        "text": text,
    }


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = EmbeddingStore(str(self.root / "search.sqlite3"))
        self.addCleanup(self.cache.close)
        self.source = self.root / "transcripts.sqlite3"
        with contextlib.closing(SQLiteFixture(self.source)) as fixture:
            fixture.db.executescript("""
                INSERT INTO channels VALUES('c','channel','url');
                INSERT INTO videos VALUES('v','c','title','');
                INSERT INTO transcripts VALUES('v','vi','youtube','manual','','timestamp');
                INSERT INTO transcript_parts VALUES('v','vi','youtube','manual',0,0,60,'initial');
            """)
        self.corpus = Corpus(str(self.source))
        self.addCleanup(self.corpus.close)
        self.model = fake_model()
        self.reranker = fake_reranker()
        self.module = types.ModuleType("sentence_transformers")
        self.loader = Mock(return_value=self.model)
        self.reranker_loader = Mock(return_value=self.reranker)
        setattr(self.module, "SentenceTransformer", self.loader)
        setattr(self.module, "CrossEncoder", self.reranker_loader)

    def videos(self):
        return VideoFilter(self.corpus).select()

    def cli_args(self, *arguments):
        return [
            *arguments,
            "--db",
            str(self.source),
            "--search-db",
            str(self.root / "cli-cache.sqlite3"),
            "--log-file",
            str(self.root / "cli.jsonl"),
        ]

    def update(self, text):
        with contextlib.closing(sqlite3.connect(self.source)) as c:
            c.execute("UPDATE transcript_parts SET text=?", (text,))
            c.commit()

    def test_keyword_mode_never_loads_models_and_requires_all_terms(self):
        self.update("NMN giúp kháng insulin")
        before = hashlib.sha256(self.source.read_bytes()).digest()
        with patch(
            "video_search.search.RetrievalModel",
            side_effect=AssertionError("model loaded"),
        ):
            engine = SearchEngine(self.cache, mode="keyword")
            found = engine.search("nmn insulin", self.videos())
            self.assertEqual(found[0]["video_id"], "v")
            self.assertIsNone(found[0]["similarity"])
            self.assertGreater(found[0]["score"], 0)
            self.assertEqual(engine.search("nmn missing", self.videos()), [])
            self.assertEqual(engine.search("!!!", self.videos()), [])
            self.assertEqual(
                self.cache.db.execute(
                    "SELECT count(*) FROM embedding_vectors"
                ).fetchone()[0],
                0,
            )
            with self.assertRaises(ValueError):
                engine.search("nmn", self.videos(), min_similarity=0.5)
            self.update("nội dung mới")
            self.assertEqual(engine.search("nmn", self.videos()), [])
            self.assertEqual(
                engine.search("noi dung", self.videos())[0]["text"], "nội dung mới"
            )
        # Only the explicit simulated collector update changed the input.
        self.assertNotEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)

    def test_keyword_phrases_and_user_text_are_safe(self):
        docs = [
            window("kháng insulin", "a"),
            window("insulin kháng", "b"),
            window("kháng thuốc insulin", "c"),
        ]
        scores = self.cache.lexical_scores('"khang insulin"', docs, keyword_query=True)
        self.assertGreater(scores[0], 0)
        self.assertEqual(scores[1:].tolist(), [0.0, 0.0])
        self.assertTrue(
            all(
                self.cache.lexical_scores("khang insulin", docs, keyword_query=True) > 0
            )
        )
        self.assertEqual(
            self.cache.lexical_scores("NOT OR * :", docs, keyword_query=True).tolist(),
            [0.0] * 3,
        )
        with self.assertRaisesRegex(ValueError, "quote"):
            self.cache.lexical_scores('"khang', docs, keyword_query=True)

    def test_duplicate_videos_and_windows_preserve_results_across_cache_reuse(self):
        with patch.dict("sys.modules", {"sentence_transformers": self.module}):
            for mode in ("keyword", "hybrid", "semantic", "exact"):
                with self.subTest(mode=mode):
                    engine = SearchEngine(self.cache, mode=mode)
                    videos = self.videos()
                    expected = engine.search("initial", videos)
                    self.assertTrue(expected)
                    self.assertEqual(engine.search("initial", videos * 2), expected)
                    docs = [window("initial"), window("initial", "other")]
                    expected = engine.search_windows("initial", docs)
                    duplicate: Window = {**docs[0]}
                    duplicated = [docs[0], docs[1], duplicate]
                    self.assertEqual(
                        engine.search_windows("initial", duplicated), expected
                    )
                    reordered = engine.search_windows("initial", docs[::-1])
                    self.assertEqual(
                        [item["video_id"] for item in reordered], ["other", "v"]
                    )
                    self.assertEqual(
                        engine.search_windows("initial", [docs[1], docs[0], docs[1]]),
                        reordered,
                    )

    def test_semantic_mode_never_queries_bm25(self):
        with (
            patch.dict("sys.modules", {"sentence_transformers": self.module}),
            patch.object(
                self.cache, "lexical_scores", side_effect=AssertionError("BM25 used")
            ),
        ):
            engine = SearchEngine(self.cache, mode="semantic")
            found = engine.search("semantic question", self.videos())
            self.assertEqual(found[0]["score"], found[0]["similarity"])

    def test_keyword_cli_outputs_mode_and_no_model(self):
        self.update("kháng insulin")
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch(
                "video_search.search.RetrievalModel",
                side_effect=AssertionError("model loaded"),
            ),
        ):
            search_main(self.cli_args('"khang insulin"', "--mode", "keyword", "--json"))
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "keyword")
        self.assertIsNone(payload["model"])
        self.assertEqual(payload["results"][0]["video_id"], "v")

    def test_long_tail_is_embedded_and_can_win_without_lexical_overlap(self):
        long_text = " ".join(f"word{i}" for i in range(400)) + " needle"
        self.update(long_text)
        with contextlib.closing(sqlite3.connect(self.source)) as c:
            c.executescript("""INSERT INTO videos VALUES('other','c','','');
                INSERT INTO transcripts VALUES('other','vi','youtube','manual','','timestamp');
                INSERT INTO transcript_parts VALUES('other','vi','youtube','manual',0,0,60,'unrelated');""")

        def encode(texts, **kwargs):
            return np.array(
                [
                    [1.0, 0.0]
                    if "needle" in t or t == "semantic question"
                    else [0.0, 1.0]
                    for t in texts
                ]
            )

        self.model.encode.side_effect = encode
        before = hashlib.sha256(self.source.read_bytes()).digest()
        with patch.dict("sys.modules", {"sentence_transformers": self.module}):
            engine = SearchEngine(self.cache)
            results = engine.search("semantic question", self.videos(), top=2)
        self.assertEqual(results[0]["video_id"], "v")
        encoded_texts = [
            t for call in self.model.encode.call_args_list for t in call.args[0]
        ]
        self.assertTrue(any(t.endswith("needle") for t in encoded_texts))
        self.assertTrue(
            all(len(self.model.tokenizer.encode(t)) <= 128 for t in encoded_texts)
        )
        self.assertEqual(results[0]["text"], long_text)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)

    def test_engine_reuses_model_but_detects_an_external_commit(self):
        with patch.dict("sys.modules", {"sentence_transformers": self.module}):
            engine = SearchEngine(self.cache)
            engine.search("first question", self.videos())
            engine.search("second question", self.videos())
            self.assertEqual(self.loader.call_count, 1)
            self.assertEqual(self.model.encode.call_count, 3)
            self.update("changed transcript")
            result = engine.search("third question", self.videos())
            self.assertEqual(result[0]["text"], "changed transcript")
            self.assertEqual(self.loader.call_count, 1)
            self.assertEqual(self.model.encode.call_count, 5)

    def test_binary_cache_is_scoped_by_model_and_encoding_config(self):
        values = [(str(i), [float(i), 1.0]) for i in range(601)]
        self.cache.save_vectors(values, "model", "config-a")
        loaded = self.cache.vectors([k for k, _ in values], "model", "config-a")
        self.assertEqual(len(loaded), 601)
        np.testing.assert_allclose(loaded["600"], [600.0, 1.0])
        self.assertEqual(self.cache.vectors(["600"], "other", "config-a"), {})
        self.assertEqual(self.cache.vectors(["600"], "model", "config-b"), {})
        count = self.cache.db.execute(
            "SELECT count(*) FROM embedding_vectors WHERE model='model' AND config='config-a'"
        ).fetchone()[0]
        self.assertEqual(count, 601)

    def test_old_json_embeddings_are_preserved_and_not_used_with_new_config(self):
        self.cache.db.execute(
            "CREATE TABLE embeddings (model TEXT, text_hash TEXT, vector TEXT)"
        )
        self.cache.db.execute(
            "INSERT INTO embeddings VALUES ('model', 'h1', '[1.0, 0.0]')"
        )
        self.cache.db.commit()
        self.assertEqual(self.cache.vectors(["h1"], "model", "new-config"), {})
        self.assertEqual(
            self.cache.db.execute("SELECT vector FROM embeddings").fetchone()[0],
            "[1.0, 0.0]",
        )

    def test_sqlite_persists_vectors_and_fts_across_reopen(self):
        cache_path = str(self.root / "reopen.sqlite3")
        with contextlib.closing(EmbeddingStore(cache_path)) as store:
            store.save_vectors([("h1", [1.0, 0.0]), ("h2", [0.0, 1.0])], "m", "c")
            docs = [window("hố đen", "a"), window("nấu ăn", "b")]
            self.assertGreater(store.lexical_scores("ho den", docs)[0], 0)
        with contextlib.closing(EmbeddingStore(cache_path)) as reopened:
            np.testing.assert_allclose(
                reopened.vectors(["h1"], "m", "c")["h1"], [1.0, 0.0]
            )
            docs = [window("hố đen", "a"), window("nấu ăn", "b")]
            self.assertGreater(reopened.lexical_scores("ho den", docs)[0], 0)

    def test_fts_refreshes_for_text_title_order_and_scope_changes(self):
        docs = [window("cooking", "a"), window("hố đen", "b")]
        scores = self.cache.lexical_scores("ho den", docs)
        self.assertGreater(scores[1], scores[0])
        changes = self.cache.db.total_changes
        self.cache.lexical_scores("ho den", docs)
        self.assertEqual(self.cache.db.total_changes, changes)
        reverse = self.cache.lexical_scores("ho den", docs[::-1])
        self.assertGreater(reverse[0], reverse[1])
        docs[0]["title"] = "hố đen"
        self.assertGreater(self.cache.lexical_scores("ho den", docs[:1])[0], 0)
        docs[0]["title"] = "new title"
        self.assertEqual(self.cache.lexical_scores("ho den", docs[:1])[0], 0)
        self.assertGreater(self.cache.lexical_scores("ho den", docs)[1], 0)
        self.assertEqual(
            self.cache.lexical_scores('" OR NOT * : --', docs).tolist(), [0.0, 0.0]
        )

    def test_e5_prefixes_and_token_budget_are_applied(self):
        with patch.dict("sys.modules", {"sentence_transformers": self.module}):
            adapter = RetrievalModel("e5-small")
            chunks = adapter.split([" ".join(f"t{i}" for i in range(300))])[0]
            adapter.encode([chunks[0]], 4)
            self.assertEqual(self.model.encode.call_args.kwargs["prompt"], "passage: ")
            adapter.encode(["question"], 4, query=True)
            self.assertEqual(self.model.encode.call_args.kwargs["prompt"], "query: ")
        self.assertTrue(chunks[-1].endswith("t299"))
        self.assertTrue(
            all(
                len(self.model.tokenizer.encode("passage: " + s)) <= 128 for s in chunks
            )
        )

    def test_incremental_index_keeps_existing_rows_when_a_video_is_inserted(self):
        docs = [window("hố đen", "a"), window("cooking", "b")]
        self.cache.lexical_scores("ho den", docs)
        original_rows = dict(
            self.cache.db.execute("SELECT key, id FROM lexical_rows").fetchall()
        )
        with patch("video_search.cache.tokens", wraps=tokens) as tokenize:
            result = self.cache.lexical_scores(
                "ho den", [window("new video", "new"), *docs]
            )
        self.assertEqual(tokenize.call_count, 2)  # query and only the added window
        self.assertGreater(result[1], result[0])
        updated_rows = dict(
            self.cache.db.execute("SELECT key, id FROM lexical_rows").fetchall()
        )
        for key, row_id in original_rows.items():
            self.assertEqual(updated_rows[key], row_id)
        self.assertEqual(len(updated_rows), 3)

    def test_reranker_reads_the_tail_and_respects_pair_budget(self):
        self.update(" ".join(f"word{i}" for i in range(700)) + " important")
        reranker = Mock()
        reranker.tokenizer = FakeTokenizer()
        reranker.predict.side_effect = lambda pairs, **kwargs: np.array(
            [2.0 if p[1].endswith("important") else -1.0 for p in pairs]
        )
        factory = Mock(return_value=reranker)
        setattr(self.module, "CrossEncoder", factory)
        with patch.dict("sys.modules", {"sentence_transformers": self.module}):
            engine = SearchEngine(
                self.cache, reranker_name="test-reranker", mode="hybrid"
            )
            results = engine.search("question", self.videos())
        self.assertEqual(results[0]["rerank_score"], 2.0)
        pairs = reranker.predict.call_args.args[0]
        self.assertTrue(any(p[1].endswith("important") for p in pairs))
        self.assertTrue(
            all(len(p[0].split()) + len(p[1].split()) + 3 <= 512 for p in pairs)
        )

    def test_disabled_reranker_is_never_loaded_and_cutoff_can_return_empty(self):
        setattr(
            self.module,
            "CrossEncoder",
            Mock(side_effect=AssertionError("must not load")),
        )
        self.model.encode.side_effect = lambda texts, **kwargs: np.array(
            [[0.0, 1.0] if t == "question" else [1.0, 0.0] for t in texts]
        )
        with patch.dict("sys.modules", {"sentence_transformers": self.module}):
            engine = SearchEngine(self.cache)
            self.assertEqual(
                engine.search("question", self.videos(), min_similarity=0.5), []
            )
            self.assertEqual(len(engine.search("question", self.videos())), 1)

    def test_split_pair_reserves_query_tokens_and_never_discards_tail(self):
        parts = split_texts(
            FakeTokenizer(),
            [" ".join(f"t{i}" for i in range(100))],
            32,
            reserved_tokens=8,
        )[0]
        self.assertTrue(parts[-1].endswith("t99"))
        self.assertTrue(all(len(p.split()) + 2 + 8 <= 32 for p in parts))

    def test_evaluation_metrics_and_sampling_keep_known_moments(self):
        gold = {"video_id": "v", "source": "youtube", "start": 10.0, "end": 30.0}
        rows = [
            {"id": "positive", "relevant": [gold]},
            {"id": "negative", "relevant": []},
        ]
        score = metrics(rows, [[window("other", "a"), window("right")], []])
        self.assertEqual(score["known_video_hit@5"], 1.0)
        self.assertEqual(score["mrr@5"], 0.5)
        self.assertEqual(score["known_moment_hit@5"], 1.0)
        self.assertEqual(score["negative_false_return_rate"], 0.0)
        docs = [window(str(i), str(i), i * 100) for i in range(100)] + [window("right")]
        selected = select_windows(docs, rows, 10)
        self.assertEqual(len(selected), 10)
        self.assertIn(docs[-1], selected)
        self.assertEqual(selected, select_windows(docs, rows, 10))

    def test_evaluation_output_cannot_alias_input_through_hardlink_or_symlink(self):
        for name, link in [("hard", False), ("sym", True)]:
            target = self.root / name
            if link:
                target.symlink_to(self.source)
            else:
                target.hardlink_to(self.source)
            with self.assertRaises(ValueError):
                validate_output_paths([self.source], [target])

    def test_interactive_json_emits_one_object_per_line_and_reuses_model(self):
        output = io.StringIO()
        with (
            patch.dict("sys.modules", {"sentence_transformers": self.module}),
            patch("builtins.input", side_effect=["first", "second", ""]),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            search_main(self.cli_args("--interactive", "--json"))
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["query"] for r in rows], ["first", "second"])
        self.assertEqual(self.loader.call_count, 1)

    def test_evaluation_rejects_stale_evidence_and_keeps_exact_sample_limit(self):
        gold = {
            "video_id": "v",
            "source": "youtube",
            "start": 10.0,
            "end": 30.0,
            "evidence": "original evidence",
        }
        rows = [{"id": "one", "relevant": [gold]}]
        docs = [window("original evidence"), window("distractor", "other")]
        self.assertEqual(select_windows(docs, rows, 1), docs[:1])
        validate_evidence(rows, docs)
        with self.assertRaises(ValueError):
            validate_evidence(rows, [window("refreshed text")])

    def test_removed_source_is_rejected_by_api_and_cli(self):
        with self.assertRaisesRegex(ValueError, "Unknown transcript source"):
            self.corpus.windows(source="legacy")
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            search_main(["question", "--transcript-source", "legacy"])
        self.assertEqual(error.exception.code, 2)

    def test_evaluation_snapshot_rejects_removed_source(self):
        retired = window("retired transcript")
        retired["transcript_source"] = "legacy"
        with self.assertRaisesRegex(ValueError, "YouTube and generated"):
            FixedCorpus(str(self.source), [retired])

    def test_title_is_prepended_for_embedding_and_cache_version_bumped(self):
        with contextlib.closing(sqlite3.connect(self.source)) as c:
            c.execute("UPDATE videos SET title='Tiêu đề Docker' WHERE id='v'")
            c.execute("UPDATE transcript_parts SET text='chạy lệnh này để build'")
            c.commit()

        def encode(texts, **kwargs):
            return np.array(
                [
                    [1.0, 0.0] if ("Docker" in t or t == "docker build") else [0.0, 1.0]
                    for t in texts
                ],
                dtype=np.float32,
            )

        self.model.encode.side_effect = encode
        with patch.dict("sys.modules", {"sentence_transformers": self.module}):
            engine = SearchEngine(
                self.cache, reranker_name=DEFAULT_RERANKER, mode="hybrid"
            )
            results = engine.search("docker build", self.videos())
            assert engine.model is not None
            self.assertEqual(json.loads(engine.model.config)["version"], 3)
        self.assertEqual(results[0]["text"], "chạy lệnh này để build")
        encoded_docs = self.model.encode.call_args_list[0].args[0]
        self.assertEqual(encoded_docs, ["Tiêu đề Docker\nchạy lệnh này để build"])
        rerank_pairs = self.reranker.predict.call_args.args[0]
        self.assertEqual(
            rerank_pairs, [["docker build", "Tiêu đề Docker\nchạy lệnh này để build"]]
        )

    def test_cli_defaults_to_hybrid_with_reranker_and_validates_rerank_flags(self):
        self.update("nội dung mặc định")
        for extra, expected_mode, expect_reranker in (
            ([], "hybrid", DEFAULT_RERANKER),
            (["--no-rerank"], "hybrid", None),
            (["--mode", "semantic"], "semantic", None),
        ):
            with self.subTest(mode=expected_mode, reranker=expect_reranker):
                self.reranker_loader.reset_mock()
                output = io.StringIO()
                with (
                    patch.dict("sys.modules", {"sentence_transformers": self.module}),
                    contextlib.redirect_stdout(output),
                ):
                    search_main(self.cli_args("nội dung", *extra, "--json"))
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["mode"], expected_mode)
                self.assertEqual(payload["reranker"], expect_reranker)
                if expect_reranker:
                    self.reranker_loader.assert_called_once()
                else:
                    self.reranker_loader.assert_not_called()
        for invalid_mode in ("semantic", "keyword"):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                search_main(
                    self.cli_args("nội dung", "--mode", invalid_mode, "--rerank")
                )

    def test_exact_mode_preserves_diacritics_and_punctuation_without_models(self):
        with contextlib.closing(sqlite3.connect(self.source)) as c:
            c.execute("UPDATE videos SET title='Học C++ cơ bản' WHERE id='v'")
            c.execute("UPDATE transcript_parts SET text='Dùng C++ và kháng insulin'")
            c.executescript(
                """INSERT INTO videos VALUES('other','c','Khác','');
                INSERT INTO transcripts VALUES('other','vi','youtube','manual','','now');
                INSERT INTO transcript_parts VALUES('other','vi','youtube','manual',0,0,60,'Dung C va khang insulin');"""
            )
            c.commit()
        with (
            patch(
                "video_search.search.RetrievalModel",
                side_effect=AssertionError("model loaded"),
            ),
            patch.object(
                self.cache, "lexical_scores", side_effect=AssertionError("BM25 used")
            ),
        ):
            engine = SearchEngine(self.cache, mode="exact")
            found_cpp = engine.search("c++", self.videos())
            self.assertEqual([item["video_id"] for item in found_cpp], ["v"])
            self.assertIsNone(found_cpp[0]["similarity"])
            self.assertEqual(
                engine.search("kháng insulin", self.videos())[0]["video_id"], "v"
            )
            self.assertEqual(len(engine.search("kháng insulin", self.videos())), 1)
            with self.assertRaises(ValueError):
                engine.search("c++", self.videos(), min_similarity=0.5)

    def test_fuzzy_mode_is_rejected_by_engine_and_cli(self):
        with self.assertRaisesRegex(ValueError, "Unknown search mode"):
            SearchEngine(self.cache, mode="fuzzy")
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            search_main(["question", "--mode", "fuzzy"])
        self.assertEqual(error.exception.code, 2)

    def test_exact_scores_ranks_transcript_hits_above_title_only(self):
        docs = [
            window("bài mở đầu", "title_only", title="Hướng dẫn Python"),
            window("học python và python nâng cao", "body_hits", title="Lập trình"),
        ]
        scores = exact_scores("Python", docs)
        self.assertGreater(scores[1], scores[0])
        self.assertGreater(scores[0], 0)

    def test_search_requires_all_current_tables_without_changing_input(self):
        tables = ("channels", "videos", "transcripts", "transcript_parts")
        for missing in tables:
            with self.subTest(missing=missing):
                path = self.root / ("incomplete-" + missing + ".sqlite3")
                with contextlib.closing(SQLiteFixture(path)) as fixture:
                    fixture.db.execute("DROP TABLE " + missing)
                    fixture.db.commit()
                before = hashlib.sha256(path.read_bytes()).digest()
                with self.assertRaisesRegex(ValueError, missing):
                    Corpus(str(path))
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)


if __name__ == "__main__":
    unittest.main()

"""Public metadata predicates over independent, read-only schema-v2 fixtures."""

import contextlib
import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from tests.sqlite_fixture import SQLiteFixture
from video_search.cache import EmbeddingStore
from video_search.corpus import Corpus
from video_search.filtering import VideoFilter, VideoFilters
from video_search.search import SearchEngine


class MetadataPredicateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yt-predicate-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "transcripts.sqlite3"
        with contextlib.closing(SQLiteFixture(self.path)) as fixture, fixture.db:
            fixture.db.executemany(
                "INSERT INTO channels VALUES(?,?,?)",
                [
                    ("c1", "Khoa học", "https://youtube.com/@science"),
                    ("c2", "Kênh khác", "https://youtube.com/@other"),
                ],
            )
            fixture.db.executemany(
                "INSERT INTO videos VALUES(?,?,?,?)",
                [
                    ("a", "c1", "Alpha insulin", "O'Reilly"),
                    ("b", "c2", "Beta C++", "metabolism 100%"),
                    ("c", "c1", "Inventory only", ""),
                    ("d", "c1", "Delta fallback", None),
                    ("e", "c2", "Echo punctuation", "quotes ' OR 1=1 --"),
                    ("bad-source", "c2", "Invalid source", ""),
                    ("bad-youtube", "c2", "Invalid YouTube mode", ""),
                    ("bad-legacy", "c2", "Legacy engine", ""),
                    ("bad-engine", "c2", "Malformed engine", ""),
                ],
            )
            transcripts = [
                (
                    "a",
                    "vi",
                    "youtube",
                    "manual",
                    "",
                    "2026-01-02",
                    [(10, 5, "insulin Việt"), (130, 10, "insulin Việt sau")],
                ),
                (
                    "a",
                    "vi",
                    "youtube",
                    "auto_generated",
                    "",
                    "2026-02-01",
                    [(10, 5, "a automatic")],
                ),
                (
                    "a",
                    "en-US",
                    "youtube",
                    "manual",
                    "",
                    "2025-12-31",
                    [(2, 4, "insulin English"), (152, 5, "insulin English later")],
                ),
                (
                    "a",
                    "vi",
                    "generate",
                    "whisper",
                    "medium",
                    "2026-03-01",
                    [(20, 6, "a whisper")],
                ),
                (
                    "a",
                    "en",
                    "generate",
                    "custom-engine",
                    "custom",
                    "2026-04-01",
                    [(30, 7, "a custom engine")],
                ),
                (
                    "b",
                    "en",
                    "youtube",
                    "manual",
                    "",
                    "2026-05-01",
                    [(70, 15, "C++ English")],
                ),
                ("d", "vi", "youtube", "manual", "", "2026-06-01", [(0, 5, "  \n ")]),
                (
                    "d",
                    "vi",
                    "youtube",
                    "auto_generated",
                    "",
                    "2026-06-02",
                    [(20, 8, "d fallback")],
                ),
                (
                    "e",
                    "vi",
                    "generate",
                    "other-engine",
                    "custom",
                    "2026-07-01",
                    [(40, 9, "e custom engine")],
                ),
                (
                    "bad-source",
                    "vi",
                    "legacy",
                    "manual",
                    "",
                    "2026-01-01",
                    [(0, 1, "invalid")],
                ),
                (
                    "bad-youtube",
                    "vi",
                    "youtube",
                    "invented",
                    "",
                    "2026-01-01",
                    [(0, 1, "invalid")],
                ),
                (
                    "bad-legacy",
                    "vi",
                    "generate",
                    "legacy",
                    "",
                    "2026-01-01",
                    [(0, 1, "invalid")],
                ),
                (
                    "bad-engine",
                    "vi",
                    "generate",
                    "bad mode",
                    "",
                    "2026-01-01",
                    [(0, 1, "invalid")],
                ),
            ]
            for video, language, source, mode, config, updated, segments in transcripts:
                key = (video, language, source, mode)
                fixture.db.execute(
                    "INSERT INTO transcripts VALUES(?,?,?,?,?,?)",
                    (*key, config, updated),
                )
                fixture.db.executemany(
                    "INSERT INTO transcript_parts VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (*key, i, start, duration, text)
                        for i, (start, duration, text) in enumerate(segments)
                    ],
                )
        self.digest = hashlib.sha256(self.path.read_bytes()).digest()
        self.corpus = Corpus(str(self.path))
        self.addCleanup(self.corpus.close)
        self.selector = VideoFilter(self.corpus)

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), self.digest)

    def select(self, where, params=()):
        return self.selector.select_where(where, params)

    def ids(self, where, params=()):
        return [video.video_id for video in self.select(where, params)]

    def test_predicate_is_self_contained_and_excludes_inventory_and_invalid_sources(
        self,
    ):
        selected = self.select("1 = 1")
        self.assertEqual([video.video_id for video in selected], ["a", "b", "d", "e"])
        self.assertEqual(selected[0].language, "en-US")
        self.assertEqual(selected[0].source_mode, "manual")
        self.assertEqual(selected[2].source_mode, "auto_generated")
        self.assertEqual(selected[3].source_mode, "other-engine")
        self.assertEqual(self.ids("1 = 0"), [])

    def test_candidate_predicate_runs_before_transcript_priority_and_keeps_all_segments(
        self,
    ):
        selected = self.select(
            "video_id = ? AND language = ? AND source_mode = ?", ("a", "vi", "manual")
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].language, "vi")
        self.assertEqual([segment.start for segment in selected[0].segments], [10, 130])
        selected = self.select("video_id = 'a' AND source_mode = 'auto_generated'")
        self.assertEqual(selected[0].segments[0].text, "a automatic")
        self.assertEqual(self.ids("video_id = 'd' AND source_mode = 'manual'"), [])

    def test_generated_engines_are_not_implicitly_limited_to_whisper(self):
        selected = self.select("source = ?", ("generate",))
        self.assertEqual([video.video_id for video in selected], ["a", "e"])
        self.assertEqual(selected[0].source_mode, "custom-engine")
        self.assertEqual(selected[0].language, "en")
        selected = self.select("video_id='a' AND source='generate' AND language='vi'")
        self.assertEqual(selected[0].source_mode, "whisper")
        self.assertEqual(selected[0].transcript_config, "medium")

    def test_and_or_precedence_parentheses_and_not_select_candidate_rows(self):
        ungrouped = self.select("video_id='a' OR video_id='b' AND language='en'")
        self.assertEqual([video.video_id for video in ungrouped], ["a", "b"])
        self.assertEqual(ungrouped[0].language, "en-US")
        grouped = self.select("(video_id='a' OR video_id='b') AND language='vi'")
        self.assertEqual([video.video_id for video in grouped], ["a"])
        self.assertEqual(grouped[0].language, "vi")
        self.assertEqual(self.ids("NOT (channel_id='c1' OR language='en')"), ["e"])
        self.assertEqual(
            self.ids(
                "(channel_id='c1' AND language='vi') OR (channel_id='c2' AND language='en')"
            ),
            ["a", "b", "d"],
        )

    def test_metadata_fields_and_display_channel_alias_are_available(self):
        for field, value, expected in (
            ("video_id", "b", ["b"]),
            ("title", "Beta C++", ["b"]),
            ("description", "O'Reilly", ["a"]),
            ("channel", "Khoa học", ["a", "d"]),
            ("channel_title", "Khoa học", ["a", "d"]),
            ("channel_id", "c2", ["b", "e"]),
            ("channel_url", "https://youtube.com/@other", ["b", "e"]),
            ("language", "en-US", ["a"]),
            ("source", "generate", ["a", "e"]),
            ("source_mode", "auto_generated", ["a", "d"]),
            ("updated_at", "2026-07-01", ["e"]),
        ):
            with self.subTest(field=field):
                self.assertEqual(self.ids(f"{field} = ?", (value,)), expected)
        self.assertEqual(self.ids("channel = 'c1'"), [])

    def test_in_not_in_like_and_not_like_respect_sql_metadata_semantics(self):
        for where, params, expected in (
            ("video_id IN (?, ?)", ("e", "a"), ["a", "e"]),
            ("video_id NOT IN ('a','b')", (), ["d", "e"]),
            ("title LIKE ?", ("Alpha%",), ["a"]),
            ("title LIKE 'Beta _++'", (), ["b"]),
            ("language NOT LIKE 'vi%'", (), ["a", "b"]),
            ("description LIKE '%100%'", (), ["b"]),
        ):
            with self.subTest(where=where):
                self.assertEqual(self.ids(where, params), expected)

    def test_between_and_comparison_operators_include_boundaries(self):
        self.assertEqual(
            self.ids("updated_at BETWEEN ? AND ?", ("2026-05-01", "2026-07-01")),
            ["b", "d", "e"],
        )
        self.assertEqual(self.ids("video_id NOT BETWEEN 'b' AND 'd'"), ["a", "e"])
        for operator, expected in (
            ("=", ["b"]),
            ("!=", ["a", "d", "e"]),
            ("<>", ["a", "d", "e"]),
            ("<", ["a"]),
            ("<=", ["a", "b"]),
            (">", ["d", "e"]),
            (">=", ["b", "d", "e"]),
        ):
            with self.subTest(operator=operator):
                self.assertEqual(self.ids(f"video_id {operator} ?", ("b",)), expected)
        self.assertEqual(
            self.ids("2.5 BETWEEN 2 AND 3 AND -1 < 0"), ["a", "b", "d", "e"]
        )

    def test_null_checks_retain_sql_three_valued_logic(self):
        self.assertEqual(self.ids("description IS NULL"), ["d"])
        self.assertEqual(self.ids("description IS NOT NULL"), ["a", "b", "e"])
        self.assertEqual(self.ids("description = NULL"), [])
        self.assertEqual(self.ids("description != NULL"), [])
        self.assertEqual(self.ids("description = ?", (None,)), [])
        self.assertEqual(self.ids("NOT (description = NULL)"), [])

    def test_sql_literals_and_bound_values_keep_quotes_and_sql_tokens_as_data(self):
        self.assertEqual(self.ids("description = 'O''Reilly'"), ["a"])
        self.assertEqual(self.ids("description = ?", ("quotes ' OR 1=1 --",)), ["e"])
        self.assertEqual(self.ids("channel_title = ?", ("Khoa học' OR 1=1 --",)), [])
        self.assertEqual(self.ids("description = 'AND OR -- ; SELECT ?'"), [])
        self.assertEqual(self.ids("? = ?", (True, 1)), ["a", "b", "d", "e"])

    def test_malformed_and_out_of_scope_sql_raise_value_error_and_leave_reader_reusable(
        self,
    ):
        invalid = (
            None,
            "",
            " ",
            "WHERE video_id='a'",
            "SELECT * FROM videos",
            "missing = 'a'",
            "text LIKE '%insulin%'",
            "config = 'medium'",
            "v.title = 'Alpha insulin'",
            "length(title) > 0",
            "video_id IN (SELECT id FROM videos)",
            "video_id='a'; DELETE FROM videos",
            "video_id='a' -- comment",
            "/* comment */ video_id='a'",
            "title = 'unterminated",
            "video_id =",
            "video_id='a' AND",
            "(video_id='a'",
            "title || description = 'value'",
            "title GLOB '*'",
        )
        for where in invalid:
            with self.subTest(where=where), self.assertRaises(ValueError):
                self.select(where)
            self.assertFalse(self.corpus.db.in_transaction)
            self.assertEqual(self.ids("video_id = 'b'"), ["b"])

    def test_invalid_parameter_counts_containers_and_types_are_rejected(self):
        for where, params in (
            ("video_id = ?", ()),
            ("video_id = 'a'", ("a",)),
            ("video_id = ?", ("a", "b")),
            ("video_id = ?", ["a"]),
            ("video_id = ?", "a"),
            ("video_id = ?", {"id": "a"}),
            ("video_id = ?", None),
            ("video_id = ?", (b"a",)),
            ("video_id = ?", (object(),)),
        ):
            with (
                self.subTest(where=where, params=params),
                self.assertRaises(ValueError),
            ):
                self.select(where, params)
            self.assertFalse(self.corpus.db.in_transaction)
        self.assertEqual(self.ids("video_id = ?", ("a",)), ["a"])

    def test_integer_range_failures_are_value_errors_and_leave_reader_reusable(self):
        for value in (2**63, -(2**63) - 1, 999999999999999999999999999):
            for where, params in (
                ("video_id = ?", (value,)),
                (f"video_id = {value}", ()),
            ):
                with (
                    self.subTest(where=where, params=params),
                    self.assertRaises(ValueError),
                ):
                    self.select(where, params)
                self.assertFalse(self.corpus.db.in_transaction)
                self.assertEqual(self.ids("video_id = 'b'"), ["b"])
        for value in (-(2**63), 2**63 - 1):
            with self.subTest(value=value):
                self.assertEqual(
                    self.ids(f"? = {value}", (value,)), ["a", "b", "d", "e"]
                )

    def test_parameter_and_api_switching_never_returns_stale_cached_selection(self):
        where = "video_id = ?"
        self.assertEqual(self.ids(where, ("a",)), ["a"])
        self.assertEqual(self.ids(where, ("b",)), ["b"])
        self.assertEqual(self.ids(where, ("a",)), ["a"])
        legacy = self.selector.select(VideoFilters(video_ids=("a",)))
        self.assertEqual(legacy[0].language, "vi")
        self.assertEqual(self.select(where, ("a",))[0].language, "en-US")
        self.assertEqual(
            self.selector.select(VideoFilters(video_ids=("a",)))[0].language, "vi"
        )
        self.assertEqual(self.ids("video_id = ? AND language = ?", ("a", "fr")), [])
        self.assertEqual(self.ids(where, ("a",)), ["a"])

    def test_cache_distinguishes_integer_and_real_sql_values_for_text_columns(self):
        with contextlib.closing(sqlite3.connect(self.path)) as writer, writer:
            writer.executescript("""
                INSERT INTO videos VALUES('1','c1','Numeric ID','');
                INSERT INTO transcripts VALUES('1','vi','youtube','manual','','now');
                INSERT INTO transcript_parts VALUES('1','vi','youtube','manual',0,0,1,'numeric id');
            """)
        self.digest = hashlib.sha256(self.path.read_bytes()).digest()
        # Python considers 1 == 1.0, but SQLite TEXT affinity compares these
        # bindings as '1' and '1.0', so they cannot share a selection cache key.
        for value, expected in ((1, ["1"]), (1.0, []), (True, ["1"]), ("1.0", [])):
            with self.subTest(value=value, kind=type(value)):
                self.assertEqual(self.ids("video_id = ?", (value,)), expected)
        self.assertEqual(self.ids("video_id = 1"), ["1"])
        self.assertEqual(self.ids("video_id = 1.0"), [])
        self.assertEqual(self.ids("video_id = 1"), ["1"])

    def test_snapshot_is_immutable_and_searchable_after_corpus_close_without_model_load(
        self,
    ):
        selected = self.select("video_id='a' AND language='vi' AND source='youtube'")
        selected.clear()
        selected = self.select("video_id='a' AND language='vi' AND source='youtube'")
        self.assertEqual(len(selected), 1)
        with self.assertRaises(FrozenInstanceError):
            setattr(selected[0], "language", "changed")
        with self.assertRaises(FrozenInstanceError):
            setattr(selected[0].segments[0], "text", "changed")
        self.corpus.close()
        with (
            contextlib.closing(EmbeddingStore(":memory:")) as cache,
            patch(
                "video_search.search.RetrievalModel",
                side_effect=AssertionError("model loaded"),
            ),
        ):
            found = SearchEngine(cache, mode="keyword").search("insulin", selected)
        self.assertEqual([video["video_id"] for video in found], ["a"])
        self.assertEqual({moment["start"] for moment in found[0]["moments"]}, {10, 130})

    def test_external_commit_refreshes_predicate_selection_but_preserves_old_snapshot(
        self,
    ):
        old = self.select("video_id = ?", ("a",))
        with contextlib.closing(sqlite3.connect(self.path)) as writer, writer:
            writer.execute("UPDATE videos SET title='Changed title' WHERE id='a'")
            writer.execute(
                "UPDATE transcript_parts SET text='Changed transcript' WHERE video_id='a' AND language='en-US'"
            )
        self.digest = hashlib.sha256(self.path.read_bytes()).digest()
        new = self.select("video_id = ?", ("a",))
        self.assertEqual(new[0].title, "Changed title")
        self.assertEqual(new[0].segments[0].text, "Changed transcript")
        self.assertEqual(old[0].title, "Alpha insulin")
        self.assertEqual(old[0].segments[0].text, "insulin English")
        self.assertEqual(self.ids("title = 'Alpha insulin'"), [])
        self.assertEqual(self.ids("title = 'Changed title'"), ["a"])


if __name__ == "__main__":
    unittest.main()

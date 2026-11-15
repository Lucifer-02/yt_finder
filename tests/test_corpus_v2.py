"""Selection, provenance and read-only behavior for collector schema v2."""

import contextlib
import hashlib
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.store import TranscriptStore
from video_search.cache import EmbeddingStore
from video_search.corpus import Corpus
from video_search.filtering import VideoFilter, VideoFilters
from video_search.search import SearchEngine
from yt_workflows.evaluation import FixedCorpus, known_match, validate_evidence

from tests.fakes import fake_model


class CorpusV2Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "transcripts.sqlite3"
        self.store = TranscriptStore(str(self.path))
        self.addCleanup(self.store.close)
        self.store.save_channel(Channel("c", "channel", "url"))
        self.corpus = Corpus(str(self.path))
        self.addCleanup(self.corpus.close)

    def save(self, text, source="youtube", mode="manual", language="vi", video="v"):
        self.store.save_video(
            Video(video, "c", "title"),
            source,
            "medium" if source == "generate" else "",
            [Segment(text, 0, 5)],
            language=language,
            source_mode=mode,
        )

    def test_each_video_selects_one_language_and_mode_and_preserves_provenance(self):
        self.save("English", language="en")
        self.save("Whisper", source="generate", mode="whisper")
        for mode in ("unknown", "translated", "auto_generated", "manual"):
            self.save(mode, mode=mode)
            selected = self.corpus.windows()
            self.assertEqual(len(selected), 1)
            self.assertEqual(
                (selected[0]["text"], selected[0]["source_mode"]), (mode, mode)
            )
            self.assertEqual(selected[0]["language"], "vi")
        selected = self.corpus.windows(source="generate")[0]
        self.assertEqual(
            (selected["text"], selected["source_mode"], selected["transcript_config"]),
            ("Whisper", "whisper", "medium"),
        )
        self.assertEqual(
            self.corpus.windows(source_mode="translated")[0]["text"], "translated"
        )
        self.assertEqual(self.corpus.windows(language="en")[0]["text"], "English")
        self.assertEqual(self.corpus.windows(language="fr"), [])

    def test_regional_languages_and_engine_selection_do_not_cross_boundaries(self):
        self.save("US", language="en-US")
        self.save("GB", language="en-GB")
        self.save("other", source="generate", mode="other-engine", language="vi")
        self.assertEqual(self.corpus.windows(), [])
        self.assertEqual(
            self.corpus.windows(source="generate", source_mode="other-engine")[0][
                "text"
            ],
            "other",
        )
        self.assertEqual(
            self.corpus.windows(source_mode="other-engine")[0]["source_mode"],
            "other-engine",
        )
        self.assertEqual(self.corpus.windows(language="en-US")[0]["text"], "US")
        self.save("exact", language="en")
        self.assertEqual(self.corpus.windows(language="en")[0]["text"], "exact")

    def test_empty_preferred_mode_falls_back_and_inventory_alone_is_not_searchable(
        self,
    ):
        self.save("auto", mode="auto_generated")
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO transcripts VALUES('v','vi','youtube','manual','','time')"
            )
        self.store.save_inventory([Video("listed", "c", "listed")])
        self.assertEqual([w["text"] for w in self.corpus.windows()], ["auto"])
        self.assertEqual(self.corpus.windows(source_mode="manual"), [])

    def test_collector_commits_are_visible_and_reader_cannot_write(self):
        self.save("before")
        version = self.corpus.revision()
        self.corpus.db.execute("BEGIN")
        self.assertEqual(
            self.corpus.db.execute("SELECT text FROM transcript_parts").fetchone()[0],
            "before",
        )
        self.save("after")
        self.assertEqual(
            self.corpus.db.execute("SELECT text FROM transcript_parts").fetchone()[0],
            "before",
        )
        self.corpus.db.rollback()
        self.assertNotEqual(self.corpus.revision(), version)
        self.assertEqual(self.corpus.windows()[0]["text"], "after")
        with self.assertRaises(sqlite3.OperationalError):
            self.corpus.db.execute("DELETE FROM transcripts")

    def test_filter_change_reuses_vectors_but_updates_result_metadata(self):
        self.save("same", mode="manual")
        self.save("same", mode="translated")
        cache = EmbeddingStore(":memory:")
        self.addCleanup(cache.close)
        model = fake_model()
        module = types.ModuleType("sentence_transformers")
        setattr(module, "SentenceTransformer", Mock(return_value=model))
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            engine = SearchEngine(cache)
            selector = VideoFilter(self.corpus)
            videos = selector.select(VideoFilters(source_mode="manual"))
            self.assertEqual(engine.search("query", videos)[0]["source_mode"], "manual")
            videos = selector.select(VideoFilters(source_mode="translated"))
            self.assertEqual(
                engine.search("query", videos)[0]["source_mode"], "translated"
            )
        # One document encode and two queries; no second document encode.
        self.assertEqual(model.encode.call_count, 3)
        self.assertEqual(
            cache.db.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0], 1
        )

    def test_schema_validation_rejects_old_version_and_missing_columns_without_writes(
        self,
    ):
        path = self.path.with_name("bad.sqlite3")
        with contextlib.closing(sqlite3.connect(path)) as db:
            self.store.db.backup(db)
            db.execute("PRAGMA user_version=1")
        before = hashlib.sha256(path.read_bytes()).digest()
        with self.assertRaisesRegex(ValueError, "version 2"):
            Corpus(str(path))
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=2")
            db.execute("ALTER TABLE transcripts DROP COLUMN config")
        with self.assertRaisesRegex(ValueError, "transcripts.*config"):
            Corpus(str(path))

    def test_evaluation_keeps_language_and_method_evidence_separate(self):
        self.save("first half", mode="manual")
        self.save("second half", mode="translated")
        manual = self.corpus.windows(source_mode="manual")[0]
        translated = self.corpus.windows(source_mode="translated")[0]
        gold = {
            "video_id": "v",
            "source": "youtube",
            "source_mode": "manual",
            "language": "vi",
            "start": 0,
            "end": 5,
        }
        self.assertTrue(known_match(manual, gold))
        self.assertFalse(known_match(translated, gold))
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            validate_evidence(
                [
                    {
                        "id": "q",
                        "relevant": [{**gold, "evidence": "first half second half"}],
                    }
                ],
                [manual, translated],
            )
        with contextlib.closing(FixedCorpus(str(self.path), [manual])) as fixed:
            self.assertEqual(fixed.windows(), [manual])

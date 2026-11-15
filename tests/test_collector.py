import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.pipeline import index_channel
from transcript_collector.store import TranscriptStore
from transcript_collector.youtube import (
    CaptionsUnavailable,
    SourceError,
)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "transcripts.sqlite3"
        self.downloaded = []
        self.store = TranscriptStore(str(self.db_path))
        self.addCleanup(self.store.close)
        self.channel = Channel(
            "UCtest", "Kênh thử", "https://youtube.com/channel/UCtest"
        )
        self.store.save_channel(self.channel)

    def save(self, video_id, segments, channel_id="UCtest", source="youtube"):
        self.store.save_video(
            Video(video_id, channel_id, video_id),
            ("youtube" if source == "youtube" else "generate"),
            "vi,en",
            segments,
            language="vi",
            source_mode=("manual" if source == "youtube" else source),
        )

    def collect(self, source, language="vi", **options):
        with contextlib.redirect_stderr(io.StringIO()):
            return index_channel(source, self.store, self.channel, language, **options)

    def test_source_modes_and_cache(self):
        source = Mock(transcript_mode="manual", transcript_language="vi")
        source.download_audio.side_effect = self.download
        source.videos.return_value = [Video("v", "UCtest", "Video")]
        source.transcript.return_value = [Segment("caption", 1, 2)]
        transcriber = Mock(config="medium", transcript_language="vi")
        transcriber.name = "whisper"
        transcriber.transcript.return_value = [Segment("generated", 3, 2)]
        youtube = self.collect(source, mode="direct")
        whisper = self.collect(source, transcriber=transcriber, mode="generate")
        cached = self.collect(source, transcriber=transcriber, mode="generate")
        self.assertEqual(
            (youtube["indexed"], whisper["indexed"], cached["cached"]), (1, 1, 1)
        )
        source.transcript.assert_called_once_with("v")
        transcriber.transcript.assert_called_once()
        self.assertEqual(
            self.transcript("v", "whisper")["segments"][0]["text"],
            "generated",
        )
        self.assertEqual(
            self.transcript("v")["segments"][0]["text"],
            "caption",
        )

    def test_invalid_auto_mode_rejected(self):
        with self.assertRaisesRegex(ValueError, "Choose direct or generate"):
            self.collect(Mock(), mode="auto")

    def test_youtube_only_never_invokes_whisper(self):
        source = Mock(transcript_mode="manual", transcript_language="vi")
        source.videos.return_value = [Video("v", "UCtest", "v")]
        source.transcript.side_effect = CaptionsUnavailable("disabled")
        transcriber = Mock(config="medium", transcript_language="vi")
        counts = self.collect(source, transcriber=transcriber, mode="direct")
        self.assertEqual(counts["unavailable"], 1)
        transcriber.transcript.assert_not_called()
        source.download_audio.assert_not_called()

    def test_downloads_save_all_transcripts_and_reuse_completed_work(self):
        source = Mock(transcript_mode="manual", transcript_language="vi")
        ids = [f"fresh{i}" for i in range(4)]
        source.videos.return_value = [Video(v, "UCtest", v) for v in ids]
        source.transcript.side_effect = lambda v: [Segment(v, 0, 1)]
        first = self.collect(source, mode="direct")
        second = self.collect(source, mode="direct")
        self.assertEqual(
            (first["indexed"], first["audio_downloaded"], first["audio_errors"]),
            (4, 0, 0),
        )
        self.assertEqual(second["cached"], 4)
        self.assertEqual(source.transcript.call_count, 4)
        source.download_audio.assert_not_called()
        self.assertEqual(
            [(v, self.transcript(v)["segments"][0]["text"]) for v in ids],
            [(v, v) for v in ids],
        )

    def test_collection_retries_only_missing_transcripts(self):
        source = Mock(transcript_mode="manual", transcript_language="vi")
        source.videos.return_value = [Video(v, "UCtest", v) for v in ["one", "two"]]

        def fetch(video_id):
            if video_id == "two":
                raise SourceError("temporary failure")
            return [Segment(video_id, 0, 1)]

        source.transcript.side_effect = fetch
        first = self.collect(source, mode="direct")
        source.transcript.side_effect = lambda v: [Segment(v, 0, 1)]
        second = self.collect(source, mode="direct")
        self.assertEqual((first["indexed"], first["errors"]), (1, 1))
        self.assertEqual((second["indexed"], second["cached"]), (1, 1))
        self.assertEqual(
            [call.args[0] for call in source.transcript.call_args_list].count("one"), 1
        )
        self.assertEqual(
            [call.args[0] for call in source.transcript.call_args_list].count("two"), 2
        )

    def test_failed_or_empty_refresh_retains_existing_transcript(self):
        self.save("v", [Segment("old", 1, 2)])
        for failure in (SourceError("blocked"), [Segment("  ", 1, 2)]):
            with self.subTest(failure=failure):
                source = Mock(transcript_mode="manual", transcript_language="vi")
                source.videos.return_value = [Video("v", "UCtest", "v")]
                source.transcript.side_effect = [failure]
                counts = self.collect(source, refresh=True, mode="direct")
                self.assertEqual(counts["retained"], 1)
                self.assertEqual(self.transcript("v")["segments"][0]["text"], "old")

    def transcript(self, video_id, source="youtube", language="vi"):
        engine = source if source != "youtube" else "manual"
        source = "youtube" if source == "youtube" else "generate"
        row = self.store.db.execute(
            "SELECT * FROM transcripts WHERE video_id=? AND language=? AND source=? AND source_mode=?",
            (video_id, language, source, engine),
        ).fetchone()
        self.assertIsNotNone(row)
        return {
            **dict(row),
            "segments": [
                dict(r)
                for r in self.store.db.execute(
                    "SELECT text,start,duration FROM transcript_parts WHERE video_id=? AND language=? AND source=? AND source_mode=? ORDER BY position",
                    (video_id, language, source, engine),
                )
            ],
        }

    def test_database_keeps_languages_and_sources_independent(self):
        video = Video("v", "UCtest", "Tiêu đề", "Mô tả")
        for language, source, text in (
            ("vi", "youtube", "phụ đề"),
            ("en-US", "youtube", "captions"),
            ("vi", "whisper", "speech"),
        ):
            self.store.save_video(
                video,
                ("youtube" if source == "youtube" else "generate"),
                "config",
                [Segment(text, 1, 2)],
                language=language,
                source_mode=("manual" if source == "youtube" else source),
            )
        self.store.save_video(
            video,
            "youtube",
            "new",
            [Segment("mới", 5, 3)],
            language="vi",
            source_mode="manual",
        )
        self.assertEqual(
            self.transcript("v")["segments"],
            [{"text": "mới", "start": 5, "duration": 3}],
        )
        self.assertEqual(
            self.transcript("v", language="en-US")["segments"][0]["text"], "captions"
        )
        self.assertEqual(
            self.transcript("v", source="whisper")["segments"][0]["text"], "speech"
        )
        self.assertTrue(self.store.cached("v", "youtube", "ignored", language="vi"))
        self.assertFalse(self.store.cached("v", "youtube", "ignored", language="fr"))
        reopened = TranscriptStore(str(self.db_path))
        try:
            self.assertTrue(
                reopened.cached("v", "generate", "config", source_mode="whisper")
            )
        finally:
            reopened.close()

    def test_inventory_preserves_transcripts_and_full_description(self):
        video = Video("v", "UCtest", "title", "full description")
        self.store.save_video(
            video,
            "youtube",
            "vi",
            [Segment("old", 1, 2)],
            language="vi",
            source_mode="manual",
        )
        before = self.transcript("v")
        self.store.save_inventory([Video("v", "UCtest", "new title")])
        self.assertEqual(self.transcript("v"), before)
        metadata = self.store.db.execute(
            "SELECT title,description FROM videos WHERE id='v'"
        ).fetchone()
        self.assertEqual(tuple(metadata), ("new title", "full description"))

    def test_failed_transaction_restores_segments_and_metadata(self):
        self.save("v", [Segment("old", 1, 2)])
        self.save("v", [Segment("speech", 3, 2)], source="whisper")
        before = self.transcript("v")
        self.store.db.execute(
            "CREATE TRIGGER fail_write BEFORE INSERT ON transcript_parts WHEN NEW.text='fail' BEGIN SELECT RAISE(ABORT, 'failure'); END"
        )
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_video(
                Video("v", "UCtest", "changed"),
                "youtube",
                "new-config",
                [Segment("fail", 4, 2)],
                language="vi",
                source_mode="manual",
            )
        self.assertEqual(self.transcript("v"), before)
        self.assertEqual(
            self.transcript("v", source="whisper")["segments"][0]["text"], "speech"
        )
        self.assertEqual(
            self.store.db.execute("SELECT title FROM videos WHERE id='v'").fetchone()[
                0
            ],
            "v",
        )

    def test_invalid_values_do_not_replace_saved_transcript(self):
        self.save("v", [Segment("old", 1, 2)])
        video = Video("v", "UCtest", "v")
        for source, language, segments in (
            ("../bad", "vi", [Segment("text", 1, 2)]),
            ("youtube", "../escape", [Segment("text", 1, 2)]),
            ("youtube", "vi", [Segment("text", float("nan"), 2)]),
            ("youtube", "vi", [Segment("text", 1, -1)]),
            ("youtube", "vi", []),
        ):
            with (
                self.subTest(source=source, language=language, segments=segments),
                self.assertRaises(ValueError),
            ):
                self.store.save_video(
                    video,
                    ("youtube" if source == "youtube" else "generate"),
                    "config",
                    segments,
                    language=language,
                    source_mode=("manual" if source == "youtube" else source),
                )
        self.assertEqual(self.transcript("v")["segments"][0]["text"], "old")
        with self.assertRaises(ValueError):
            self.store.save_inventory([Video("../escape", "UCtest", "bad")])

    def test_actual_languages_are_stored_and_audio_is_temporary(self):
        source = Mock(transcript_mode="manual", transcript_language="en-US")
        source.download_audio.side_effect = self.download
        source.videos.return_value = [Video("new", "UCtest", "new")]
        source.transcript.return_value = [Segment("captions", 0, 2)]
        engine = Mock(config="medium", transcript_language="en")
        engine.name = "whisper"
        engine.transcript.side_effect = lambda path: [
            Segment("speech" if path.is_file() else "missing", 0, 2)
        ]
        for mode in ("direct", "generate", "generate"):
            counts = self.collect(source, language="en", mode=mode, transcriber=engine)
        self.assertEqual(counts["cached"], 1)
        self.assertEqual(self.transcript("new", language="en-US")["language"], "en-US")
        self.assertEqual(
            self.transcript("new", source="whisper", language="en")["segments"][0][
                "text"
            ],
            "speech",
        )
        source.download_audio.assert_called_once()
        source.transcript.assert_called_once_with("new")
        self.assertTrue(all(not path.parent.exists() for path in self.downloaded))

    def test_collector_cli_uses_database_and_has_no_audio_storage_option(self):
        from yt_cli.collector import parser

        args = parser().parse_args(
            ["--db", str(self.db_path), "collect", "@channel", "--mode", "direct"]
        )
        self.assertEqual(args.db, str(self.db_path))
        for option in ("--data-dir", "--audio-dir"):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser().parse_args(
                    ["collect", "@channel", "--mode", "generate", option, "directory"]
                )

    def test_whisper_exposes_detected_language(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from transcript_collector.engines.whisper import WhisperEngine

        transcriber = WhisperEngine()
        transcriber.model = Mock()
        transcriber.model.transcribe.return_value = (
            iter([SimpleNamespace(text=" speech ", start=2, end=4)]),
            SimpleNamespace(language="en"),
        )
        with patch.dict("sys.modules", {"faster_whisper": Mock()}):
            self.assertEqual(
                transcriber.transcript("audio.webm"), [Segment("speech", 2, 2)]
            )
        self.assertEqual(transcriber.transcript_language, "en")

    def test_generate_accepts_an_engine_without_whisper_specific_fields(self):
        from types import SimpleNamespace

        source = Mock(transcript_mode="manual", transcript_language="vi")
        source.download_audio.side_effect = self.download
        source.videos.return_value = [Video("v", "UCtest", "video")]
        engine = SimpleNamespace(
            name="test-engine",
            config="model-v1",
            transcript_language="vi",
            transcript=Mock(return_value=[Segment("generated", 0, 2)]),
        )
        self.collect(source, language=None, mode="generate", transcriber=engine)
        source.transcript.assert_not_called()
        self.assertEqual(
            self.transcript("v", source="test-engine")["config"], "model-v1"
        )
        self.assertTrue(
            self.store.cached("v", "generate", "model-v1", source_mode="test-engine")
        )
        self.assertTrue(self.store.has_transcript("v"))

    def test_generate_download_failure_does_not_call_engine(self):
        source = Mock(transcript_mode="manual")
        source.videos.return_value = [Video("missing", "UCtest", "missing")]
        source.download_audio.side_effect = SourceError("failed")
        engine = Mock(config="medium", transcript_language="vi")
        engine.name = "whisper"
        counts = self.collect(
            source, language=None, mode="generate", transcriber=engine
        )
        self.assertEqual((counts["audio_errors"], counts["errors"]), (1, 1))
        engine.transcript.assert_not_called()
        source.transcript.assert_not_called()
        self.assertFalse(self.store.has_transcript("missing"))

    def test_cli_mode_engine_selection_and_direct_rejects_engine_options(self):
        from unittest.mock import patch

        from transcript_collector.engines import create_engine
        from yt_cli.collector import main, parser

        args = parser().parse_args(
            [
                "collect",
                "@channel",
                "--mode",
                "generate",
                "--engine",
                "whisper",
                "--engine-model",
                "small",
            ]
        )
        self.assertEqual(
            (args.mode, args.engine, args.engine_model),
            ("generate", "whisper", "small"),
        )
        with (
            patch("yt_cli.collector_runtime.create_engine") as factory,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(["collect", "@channel", "--mode", "direct", "--engine", "whisper"])
        factory.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Unknown transcription engine"):
            create_engine("unknown", "model", "cpu")

    def download(self, video_id, directory):
        path = Path(directory) / "audio.webm"
        path.write_bytes(b"audio")
        self.downloaded.append(path)
        return path

    def test_incompatible_database_is_rejected_without_changing_it(self):
        old = self.root / "old.sqlite3"
        with contextlib.closing(sqlite3.connect(old)) as connection, connection:
            connection.execute("CREATE TABLE videos(id TEXT)")
            connection.execute("INSERT INTO videos VALUES('keep')")
        before = old.read_bytes()
        with self.assertRaisesRegex(ValueError, "Incompatible collector database"):
            TranscriptStore(str(old))
        self.assertEqual(old.read_bytes(), before)

    def test_database_only_contains_result_tables_and_enforces_foreign_keys(self):
        tables = {
            row[0]
            for row in self.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(
            tables, {"channels", "videos", "transcripts", "transcript_parts"}
        )
        with self.assertRaises(sqlite3.IntegrityError), self.store.db:
            self.store.db.execute(
                "INSERT INTO transcript_parts VALUES('absent','vi','youtube','manual',0,1,2,'orphan')"
            )

    def test_direct_cache_uses_requested_language_not_old_config_or_first_actual_track(
        self,
    ):
        source = Mock(transcript_mode="manual")
        source.videos.return_value = [
            Video("one", "UCtest", "one"),
            Video("two", "UCtest", "two"),
        ]
        self.store.save_video(
            Video("two", "UCtest", "two"),
            "youtube",
            "vi,en",
            [Segment("cached English", 0, 1)],
            language="en-GB",
            source_mode="manual",
        )

        def fetch(video_id):
            self.assertEqual(video_id, "one")
            source.transcript_language = "en-US"
            return [Segment("new English", 0, 1)]

        source.transcript.side_effect = fetch
        counts = self.collect(source, language="en", mode="direct")
        self.assertEqual((counts["indexed"], counts["cached"]), (1, 1))
        self.assertEqual(self.transcript("one", language="en-US")["config"], "")
        self.assertFalse(self.store.cached("two", "youtube", "vi,en", language="vi"))
        self.assertFalse(self.store.cached("two", "youtube", "", language="en-US"))
        for language in ("en", "EN", "en-gb", "EN-GB"):
            with self.subTest(language=language):
                self.assertTrue(
                    self.store.cached("two", "youtube", "", language=language)
                )
        self.assertFalse(self.store.cached("two", "youtube", "", language="en-us"))
        source.download_audio.assert_not_called()

    def test_direct_cli_requires_one_language_before_any_network_request(self):
        from unittest.mock import patch

        from yt_cli.collector import main

        for language_args in (
            [],
            ["--language", "vi,en"],
            ["--language", ""],
            ["--languages", "vi,en"],
        ):
            with (
                patch("yt_cli.collector_runtime.YouTube") as source,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(["collect", "@channel", "--mode", "direct", *language_args])
            source.assert_not_called()

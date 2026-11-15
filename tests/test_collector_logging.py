"""Collector logs phases without changing events, privacy or transaction behavior."""

import contextlib
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.events import EventDeliveryError
from transcript_collector.pipeline import index_video
from transcript_collector.store import TranscriptStore
from transcript_collector.youtube import YouTube


class CollectorLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TranscriptStore(str(Path(temporary.name) / "transcripts.sqlite3"))
        self.addCleanup(self.store.close)
        self.video = Video("AbCdEf12_-3", "UCtest", "private title")
        self.store.save_channel(Channel("UCtest", "channel", "https://example.test"))
        self.source = Mock(transcript_language="vi", transcript_mode="manual")
        self.source.transcript.return_value = [Segment("private transcript", 0, 1)]

    def run_video(self, **kwargs):
        return index_video(
            self.source, self.store, self.video, "vi", mode="direct", **kwargs
        )

    def test_logs_milestones_and_cache_without_transcript_or_duplicate_events(self):
        events = []
        with self.assertLogs("transcript_collector", level="DEBUG") as captured:
            self.assertEqual(self.run_video(on_event=events.append)["indexed"], 1)
            self.assertEqual(self.run_video(on_event=events.append)["cached"], 1)
        logs = "\n".join(captured.output)
        for marker in (
            "Inventory committed",
            "cache miss",
            "cache hit",
            "transcript ready",
            "source_mode=manual",
            "transcript committed",
            "elapsed=",
        ):
            self.assertIn(marker, logs)
        self.assertNotIn("private transcript", logs)
        self.assertNotIn("private title", logs)
        records = {
            record.__dict__["phase"]: record.__dict__
            for record in captured.records
            if hasattr(record, "phase")
        }
        for phase in (
            "video_start",
            "cache_miss",
            "cache_hit",
            "caption_fetch",
            "transcript_commit",
        ):
            self.assertIn(phase, records)
            self.assertEqual(records[phase]["video_id"], self.video.id)
        commit = records["transcript_commit"]
        self.assertGreaterEqual(commit["elapsed_seconds"], 0)
        self.assertEqual(commit["source_mode"], "manual")
        self.assertEqual(commit["language"], "vi")
        self.assertEqual(sum(e.kind == "video_finished" for e in events), 2)
        self.source.transcript.assert_called_once()

    def test_transaction_rollback_logged_and_previous_transcript_survives(self):
        self.run_video()
        self.store.db.execute("""CREATE TRIGGER fail_parts BEFORE INSERT ON transcript_parts
            BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END""")
        self.store.db.commit()
        with self.assertLogs("transcript_collector", level="INFO") as captured:
            result = self.run_video(refresh=True)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["retained"], 1)
        self.assertIn("transaction rolled back", "\n".join(captured.output))
        self.assertEqual(
            self.store.db.execute("SELECT text FROM transcript_parts").fetchone()[0],
            "private transcript",
        )
        self.assertFalse(self.store.db.in_transaction)

    def test_cancel_cleans_audio_and_logs_without_catching_keyboard_interrupt(self):
        directories = []

        def download(_video_id, directory):
            directories.append(Path(directory))
            audio = Path(directory) / "audio.webm"
            audio.write_bytes(b"audio")
            return audio

        self.source.download_audio.side_effect = download
        engine = Mock(name="engine", config="medium", transcript_language="vi")
        engine.name = "whisper"
        engine.transcript.side_effect = KeyboardInterrupt
        with (
            self.assertLogs("transcript_collector", level="INFO") as captured,
            self.assertRaises(KeyboardInterrupt),
        ):
            index_video(
                self.source,
                self.store,
                self.video,
                None,
                transcriber=engine,
                mode="generate",
            )
        self.assertTrue(directories)
        self.assertFalse(directories[0].exists())
        self.assertIn("temporary audio context exited", "\n".join(captured.output))
        self.assertFalse(self.store.has_transcript(self.video.id))

    def test_callback_failure_not_counted_as_source_error(self):
        def fail(_event):
            raise ValueError("private callback error")

        with (
            self.assertLogs("transcript_collector", level="INFO") as captured,
            self.assertRaises(EventDeliveryError),
        ):
            self.run_video(on_event=fail)
        self.assertNotIn("processing stopped", "\n".join(captured.output))

    def test_unconfigured_library_silent_and_environment_unchanged(self):
        environment = dict(os.environ)
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.run_video()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(root.handlers, handlers)
        self.assertEqual(root.level, level)

    def test_audio_progress_throttled_and_signed_urls_never_logged(self):
        downloader = Mock()
        downloader.__enter__ = Mock(return_value=downloader)
        downloader.__exit__ = Mock(return_value=False)
        downloader.extract_info.return_value = {"id": self.video.id}
        downloader.prepare_filename.return_value = "/tmp/audio.webm"
        source = YouTube("vi")

        def process(_info):
            hook = downloader_options["progress_hooks"][0]
            for completed in range(101):
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": completed,
                        "total_bytes": 100,
                        "url": "https://private.test?token=secret",
                    }
                )
            hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})

        downloader_options = {}

        def make_downloader(**options):
            downloader_options.update(options)
            return downloader

        downloader.process_info.side_effect = process
        with (
            patch.object(source, "_downloader", side_effect=make_downloader),
            self.assertLogs("transcript_collector", level="INFO") as captured,
        ):
            source.download_audio(self.video.id, "/tmp")
        self.assertLessEqual(len(captured.output), 15)
        self.assertIn("percent=100.0", "\n".join(captured.output))
        self.assertNotIn("secret", "\n".join(captured.output))

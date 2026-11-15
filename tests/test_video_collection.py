"""Single-video collection and its shared lifecycle with channel collection."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.pipeline import index_channel, index_video
from transcript_collector.store import TranscriptStore
from transcript_collector.youtube import (
    SourceBlocked,
    SourceError,
    VideoNotReady,
    YouTube,
    video_url,
)
from yt_cli.collector import main

VIDEO_ID = "AbCdEf12_-3"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CHANNEL_ID = "UC" + "a" * 22
CHANNEL = Channel(
    CHANNEL_ID, "Channel", f"https://www.youtube.com/channel/{CHANNEL_ID}"
)
VIDEO = Video(VIDEO_ID, CHANNEL_ID, "Video", "Full description")


def metadata():
    return {
        "id": VIDEO_ID,
        "channel_id": CHANNEL_ID,
        "channel": CHANNEL.title,
        "title": VIDEO.title,
        "description": VIDEO.description,
        "live_status": "not_live",
        "subtitles": {"vi": [{"ext": "json3", "url": "https://example.org/captions"}]},
    }


class VideoURLTests(unittest.TestCase):
    def test_supported_urls_select_only_one_video(self):
        for url in (
            URL,
            URL + "&list=playlist&index=5&t=90",
            f"https://youtu.be/{VIDEO_ID}?si=tracking&t=42",
            f"https://www.youtu.be/{VIDEO_ID}",
            f"https://m.youtube.com/watch?v={VIDEO_ID}",
            f"https://music.youtube.com/watch?v={VIDEO_ID}",
            f"https://youtube.com/shorts/{VIDEO_ID}?feature=share",
            f"https://www.youtube.com/live/{VIDEO_ID}",
            f"https://www.youtube.com/embed/{VIDEO_ID}",
        ):
            with self.subTest(url=url):
                self.assertEqual(video_url(url), URL)

    def test_channel_inputs_are_not_video_urls(self):
        for target in (
            "@channel",
            CHANNEL_ID,
            CHANNEL.url,
            "https://www.youtube.com/@channel/shorts",
        ):
            with self.subTest(target=target):
                self.assertIsNone(video_url(target))

    def test_invalid_inputs_never_trigger_network(self):
        for target in (
            "https://www.youtube.com/watch?list=playlist",
            URL + "&v=another_id1",
            "https://www.youtube.com/shorts/",
            "https://www.youtube.com/live/short",
            f"https://youtu.be/{VIDEO_ID}/extra",
            f"https://example.org/watch?v={VIDEO_ID}",
            f"https://www.youtube.com.evil.org/watch?v={VIDEO_ID}",
        ):
            with (
                self.subTest(target=target),
                patch.object(YouTube, "_extract") as extract,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                main(["collect", target, "--mode", "direct", "--language", "vi"])
            self.assertEqual(exc.exception.code, 1)
            extract.assert_not_called()

    def test_removed_max_videos_is_rejected_before_network_or_engine(self):
        for target in (URL, "@channel"):
            with (
                self.subTest(target=target),
                patch.object(YouTube, "_extract") as extract,
                patch("yt_cli.collector_runtime.create_engine") as engine,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                main(["collect", target, "--mode", "generate", "--max-videos", "1"])
            self.assertEqual(exc.exception.code, 2)
            extract.assert_not_called()
            engine.assert_not_called()

    def test_resolution_reads_owner_without_listing_channel(self):
        source = YouTube("vi")
        info = metadata()
        with patch.object(source, "_extract", return_value=info) as extract:
            self.assertEqual(source.resolve_video(URL), (CHANNEL, VIDEO))
        extract.assert_called_once_with(
            URL, extract_flat=False, writeautomaticsub=True, format="bestaudio/best"
        )
        self.assertEqual(source._video_info, (VIDEO_ID, info))

    def test_unusable_metadata_does_not_create_an_invented_channel(self):
        for info in (
            None,
            {},
            {**metadata(), "channel_id": None},
            {**metadata(), "id": "other_video"},
            {**metadata(), "_type": "playlist", "entries": []},
        ):
            with (
                self.subTest(info=info),
                patch.object(YouTube, "_extract", return_value=info),
                self.assertRaises(SourceError),
            ):
                YouTube("vi").resolve_video(URL)

    def test_active_and_upcoming_live_are_skipped_but_finished_live_is_allowed(self):
        for status in ("is_live", "is_upcoming"):
            with (
                self.subTest(status=status),
                patch.object(
                    YouTube,
                    "_extract",
                    return_value={**metadata(), "live_status": status},
                ),
                self.assertRaises(VideoNotReady),
            ):
                YouTube("vi").resolve_video(URL)
        with patch.object(
            YouTube, "_extract", return_value={**metadata(), "live_status": "was_live"}
        ):
            self.assertEqual(YouTube("vi").resolve_video(URL), (CHANNEL, VIDEO))


class SingleVideoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "results.sqlite3"
        self.output = io.StringIO()
        self.stderr = contextlib.redirect_stderr(self.output)
        self.stderr.__enter__()
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def test_cli_direct_collects_only_url_reuses_metadata_and_cache(self):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        # Each CLI invocation opens its own BytesIO response.
        ydl.urlopen.side_effect = lambda url: io.BytesIO(
            json.dumps(
                {
                    "events": [
                        {
                            "tStartMs": 1000,
                            "dDurationMs": 2000,
                            "segs": [{"utf8": "caption"}],
                        }
                    ]
                }
            ).encode()
        )
        args = [
            "--db",
            str(self.path),
            "collect",
            URL + "&list=playlist",
            "--mode",
            "direct",
            "--language",
            "vi",
        ]
        with (
            patch.object(
                YouTube, "_extract", side_effect=lambda *a, **kw: metadata()
            ) as extract,
            patch.object(YouTube, "_downloader", return_value=ydl),
            patch.object(YouTube, "videos") as listing,
            patch.object(YouTube, "download_audio") as audio,
            patch("yt_cli.collector_runtime.create_engine") as engine,
        ):
            main(args)
            main(args)
        self.assertEqual(extract.call_count, 2)
        ydl.extract_info.assert_not_called()
        ydl.urlopen.assert_called_once_with("https://example.org/captions")
        listing.assert_not_called()
        audio.assert_not_called()
        engine.assert_not_called()
        self.assertIn("[1/1] [cached] Video", self.output.getvalue())
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT * FROM channels").fetchone(),
                (CHANNEL_ID, CHANNEL.title, CHANNEL.url),
            )
            self.assertEqual(
                db.execute("SELECT * FROM videos").fetchone(),
                (VIDEO_ID, CHANNEL_ID, VIDEO.title, VIDEO.description),
            )
            self.assertEqual(
                db.execute(
                    "SELECT language,source,source_mode,config FROM transcripts"
                ).fetchall(),
                [("vi", "youtube", "manual", "")],
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_channel_and_single_video_share_cache_in_both_directions(self):
        source = Mock(transcript_mode="manual", transcript_language="vi")
        source.videos.return_value = [VIDEO]
        source.transcript.return_value = [Segment("caption", 0, 2)]
        with contextlib.closing(TranscriptStore(str(self.path))) as store:
            store.save_channel(CHANNEL)
            for first_is_single in (True, False):
                with self.subTest(first_is_single=first_is_single):
                    # Refresh the first path, then verify the other reuses it.
                    def single(refresh):
                        return index_video(
                            source, store, VIDEO, "vi", refresh, mode="direct"
                        )

                    def channel(refresh):
                        return index_channel(
                            source, store, CHANNEL, "vi", refresh, mode="direct"
                        )

                    first, second = (
                        (single, channel) if first_is_single else (channel, single)
                    )
                    self.assertEqual(first(True)["indexed"], 1)
                    self.assertEqual(second(False)["cached"], 1)
        self.assertEqual(source.transcript.call_count, 2)

    def test_generate_cli_reuses_metadata_and_cleans_audio_then_uses_cache(self):
        engine = Mock(config="test-model", transcript_language="en")
        engine.name = "whisper"
        audio_paths = []

        def transcribe(path):
            self.assertEqual(path.read_bytes(), b"audio")
            audio_paths.append(path)
            return [Segment("generated", 2, 3)]

        engine.transcript.side_effect = transcribe
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.prepare_filename.side_effect = lambda info: ydl.params["outtmpl"].replace(
            "%(ext)s", "webm"
        )
        ydl.process_info.side_effect = lambda info: Path(
            ydl.prepare_filename(info)
        ).write_bytes(b"audio")

        def downloader(**settings):
            ydl.params = settings
            return ydl

        args = ["--db", str(self.path), "collect", URL, "--mode", "generate"]
        with (
            patch.object(YouTube, "_extract", side_effect=lambda *a, **kw: metadata()),
            patch.object(YouTube, "_downloader", side_effect=downloader),
            patch.object(YouTube, "videos") as listing,
            patch.object(YouTube, "transcript") as captions,
            patch("yt_cli.collector_runtime.create_engine", return_value=engine),
        ):
            main(args)
            main(args)
        listing.assert_not_called()
        captions.assert_not_called()
        ydl.extract_info.assert_not_called()
        ydl.process_info.assert_called_once()
        engine.transcript.assert_called_once()
        self.assertFalse(audio_paths[0].parent.exists())
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT language,source,source_mode,config FROM transcripts"
                ).fetchone(),
                ("en", "generate", "whisper", "test-model"),
            )

    def test_inventory_committed_before_fetch_and_blocked_refresh_retains_transcript(
        self,
    ):
        source = Mock(transcript_mode="manual", transcript_language="vi")
        with contextlib.closing(TranscriptStore(str(self.path))) as store:
            store.save_channel(CHANNEL)

            def fetch(video_id):
                with contextlib.closing(sqlite3.connect(self.path)) as observer:
                    self.assertEqual(
                        observer.execute("SELECT id FROM videos").fetchall(),
                        [(VIDEO_ID,)],
                    )
                return [Segment("old", 0, 2)]

            source.transcript.side_effect = fetch
            self.assertEqual(
                index_video(source, store, VIDEO, "vi", mode="direct")["indexed"], 1
            )
            source.transcript.side_effect = SourceBlocked("429")
            counts = index_video(
                source, store, VIDEO, "vi", refresh=True, mode="direct"
            )
            self.assertEqual(
                (counts["blocked"], counts["retained"], counts["errors"]), (1, 1, 1)
            )
            self.assertEqual(
                store.db.execute("SELECT text FROM transcript_parts").fetchone()[0],
                "old",
            )

    def test_cli_skips_livestream_without_opening_database(self):
        with patch.object(
            YouTube, "_extract", return_value={**metadata(), "is_live": True}
        ):
            main(
                [
                    "--db",
                    str(self.path),
                    "collect",
                    URL,
                    "--mode",
                    "direct",
                    "--language",
                    "vi",
                ]
            )
        self.assertFalse(self.path.exists())
        self.assertIn("Skipping live or upcoming", self.output.getvalue())

    def test_block_during_channel_listing_remains_a_source_block(self):
        with (
            patch.object(YouTube, "_extract", side_effect=SourceBlocked("429")),
            self.assertRaises(SourceBlocked),
        ):
            YouTube("vi").videos(CHANNEL)

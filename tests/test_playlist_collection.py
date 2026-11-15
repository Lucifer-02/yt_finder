"""Playlist listing uses yt-dlp; each video keeps its actual owning channel."""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.pipeline import index_playlist
from transcript_collector.store import TranscriptStore
from transcript_collector.youtube import (
    SourceBlocked,
    SourceError,
    VideoNotReady,
    YouTube,
    playlist_url,
    video_url,
)
from yt_cli.collector import main

PLAYLIST = "https://www.youtube.com/playlist?list=PL_example-123"
IDS = ["AbCdEf12_-3", "ZyXwVu98_-7", "aBcDeF34_-5"]


class PlaylistURLTests(unittest.TestCase):
    def test_playlist_urls_are_forwarded_and_watch_urls_remain_video_urls(self):
        for host in (
            "www.youtube.com",
            "youtube.com",
            "m.youtube.com",
            "music.youtube.com",
        ):
            self.assertEqual(
                playlist_url(
                    f"https://{host}/playlist?list=PL_example-123&si=tracking"
                ),
                f"https://{host}/playlist?list=PL_example-123&si=tracking",
            )
        watch = f"https://www.youtube.com/watch?v={IDS[0]}&list=PL_example-123"
        self.assertIsNone(playlist_url(watch))
        self.assertEqual(video_url(watch), watch.split("&")[0])
        self.assertIsNone(
            playlist_url("https://example.org/playlist?list=PL_example-123")
        )

    def test_playlist_url_validation_is_delegated_to_ytdlp(self):
        for suffix in ("", "?list=", "?list=one&list=two", "?list=../bad"):
            url = "https://www.youtube.com/playlist" + suffix
            with (
                self.subTest(suffix=suffix),
                patch.object(
                    YouTube, "_extract", side_effect=SourceError("Invalid playlist")
                ) as extract,
                self.assertRaises(SourceError),
            ):
                YouTube("vi").playlist_videos(url)
            extract.assert_called_once_with(url, noplaylist=False, extract_flat=True)

    def test_listing_uses_flat_playlist_keeps_order_and_removes_duplicates_and_live(
        self,
    ):
        entries = [
            {"id": IDS[1]},
            None,
            {},
            {"id": "../bad"},
            {"id": IDS[0]},
            {"id": IDS[1]},
            {"id": IDS[2], "live_status": "is_upcoming"},
            {"id": IDS[2], "is_live": True},
            {"id": IDS[2], "live_status": "was_live"},
        ]
        source = YouTube("vi")
        with patch.object(
            source, "_extract", return_value={"entries": entries}
        ) as extract:
            self.assertEqual(source.playlist_videos(PLAYLIST), [IDS[1], IDS[0], IDS[2]])
        extract.assert_called_once_with(PLAYLIST, noplaylist=False, extract_flat=True)

    def test_empty_playlist_is_valid_and_extraction_errors_propagate(self):
        source = YouTube("vi")
        with patch.object(source, "_extract", return_value={"entries": []}):
            self.assertEqual(source.playlist_videos(PLAYLIST), [])
        for info in (None, {}, {"id": "not_a_playlist"}):
            with (
                patch.object(source, "_extract", return_value=info),
                self.assertRaises(SourceError),
            ):
                source.playlist_videos(PLAYLIST)
        with (
            patch.object(source, "_extract", side_effect=SourceBlocked("429")),
            self.assertRaises(SourceBlocked),
        ):
            source.playlist_videos(PLAYLIST)


class PlaylistCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "results.sqlite3"
        self.store = TranscriptStore(str(self.path))
        self.addCleanup(self.store.close)
        self.source = Mock(transcript_language="vi", transcript_mode="manual")
        self.source.playlist_videos.return_value = IDS
        self.source.resolve_video.side_effect = self.resolve
        self.source.transcript.return_value = [Segment("caption", 2, 3)]
        self.output = io.StringIO()
        stderr = contextlib.redirect_stderr(self.output)
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    def resolve(self, url):
        video_id = url.split("v=")[1]
        channel_id = "UC" + video_id
        return (
            Channel(
                channel_id,
                f"Channel {video_id}",
                f"https://www.youtube.com/channel/{channel_id}",
            ),
            Video(video_id, channel_id, f"Video {video_id}"),
        )

    def collect(self, **kwargs):
        return index_playlist(
            self.source, self.store, PLAYLIST, "vi", mode="direct", **kwargs
        )

    def test_multiple_owners_inventory_before_fetch_and_shared_cache(self):
        def transcript(video_id):
            with contextlib.closing(sqlite3.connect(self.path)) as observer:
                self.assertEqual(
                    observer.execute(
                        "SELECT channel_id FROM videos WHERE id=?", (video_id,)
                    ).fetchone()[0],
                    "UC" + video_id,
                )
            return [Segment("caption", 2, 3)]

        self.source.transcript.side_effect = transcript
        counts = self.collect()
        self.assertEqual((counts["listed"], counts["indexed"]), (3, 3))
        self.assertEqual(self.collect()["cached"], 3)
        self.assertEqual(self.source.transcript.call_count, 3)
        self.source.videos.assert_not_called()
        self.source.download_audio.assert_not_called()
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM channels").fetchone()[0], 3
        )
        self.assertEqual(
            self.store.db.execute("PRAGMA foreign_key_check").fetchall(), []
        )
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_missing_video_does_not_prevent_later_videos(self):
        def resolve(url):
            if IDS[1] in url:
                raise SourceError("deleted or private video")
            return self.resolve(url)

        self.source.resolve_video.side_effect = resolve
        counts = self.collect()
        self.assertEqual(
            (counts["indexed"], counts["errors"], counts["deferred"]), (2, 1, 0)
        )
        self.assertFalse(self.store.has_transcript(IDS[1]))
        self.assertTrue(self.store.has_transcript(IDS[2]))

    def test_block_during_metadata_stops_and_keeps_committed_transcripts(self):
        def resolve(url):
            if IDS[1] in url:
                raise SourceBlocked("429")
            return self.resolve(url)

        self.source.resolve_video.side_effect = resolve
        counts = self.collect()
        self.assertEqual(
            (counts["indexed"], counts["blocked"], counts["deferred"]), (1, 1, 1)
        )
        self.assertTrue(self.store.has_transcript(IDS[0]))
        self.assertEqual(self.source.resolve_video.call_count, 2)
        self.source.transcript.assert_called_once_with(IDS[0])

    def test_block_during_transcript_stops_using_single_video_error_handling(self):
        self.source.transcript.side_effect = SourceBlocked("429")
        counts = self.collect()
        self.assertEqual((counts["blocked"], counts["deferred"]), (1, 2))
        self.source.resolve_video.assert_called_once()
        self.assertEqual(
            self.store.db.execute("SELECT id FROM videos").fetchone()[0], IDS[0]
        )

    def test_live_detected_during_resolution_is_unavailable(self):
        self.source.resolve_video.side_effect = VideoNotReady("live video")
        counts = self.collect()
        self.assertEqual((counts["unavailable"], counts["errors"]), (3, 0))
        self.source.transcript.assert_not_called()

    def test_failed_refresh_preserves_old_transcripts(self):
        self.collect()
        self.source.resolve_video.side_effect = SourceError("temporarily inaccessible")
        counts = self.collect(refresh=True)
        self.assertEqual((counts["retained"], counts["errors"]), (3, 3))
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM transcript_parts").fetchone()[
                0
            ],
            3,
        )

    def test_generate_downloads_only_uncached_audio_and_cleans_it(self):
        engine = Mock(config="model", transcript_language="en")
        engine.name = "whisper"
        audio_paths = []

        def download(video_id, directory):
            path = Path(directory) / "audio.webm"
            path.write_bytes(b"audio")
            audio_paths.append(path)
            return path

        def transcribe(path):
            self.assertTrue(path.is_file())
            return [Segment("generated", 0, 2)]

        self.source.download_audio.side_effect = download
        engine.transcript.side_effect = transcribe
        for _ in range(2):
            counts = index_playlist(
                self.source,
                self.store,
                PLAYLIST,
                None,
                mode="generate",
                transcriber=engine,
            )
        self.assertEqual(counts["cached"], 3)
        self.assertEqual(self.source.download_audio.call_count, 3)
        self.assertTrue(all(not path.parent.exists() for path in audio_paths))
        self.source.transcript.assert_not_called()

    def test_cli_detects_playlist_and_uses_both_modes(self):
        engine = Mock(config="model", transcript_language="en")
        engine.name = "whisper"
        counts = dict(blocked=0, errors=0, audio_errors=0)
        for mode, options, transcriber in (
            ("direct", ["--language", "vi"], None),
            ("generate", [], engine),
        ):
            with (
                self.subTest(mode=mode),
                patch("yt_cli.collector_runtime.YouTube", return_value=self.source),
                patch("yt_cli.collector_runtime.create_engine", return_value=engine),
                patch(
                    "yt_cli.collector_runtime.index_playlist", return_value=counts
                ) as collect,
            ):
                main(
                    [
                        "--db",
                        str(self.path),
                        "collect",
                        PLAYLIST,
                        "--mode",
                        mode,
                        *options,
                    ]
                )
            self.assertEqual(collect.call_args.args[2], PLAYLIST)
            self.assertEqual(collect.call_args.args[5], transcriber)
            self.assertEqual(collect.call_args.kwargs["mode"], mode)
        self.source.resolve.assert_not_called()
        self.source.videos.assert_not_called()

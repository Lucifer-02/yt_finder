"""Collector behavior at the yt-dlp and filesystem boundaries; no real network/model."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from transcript_collector.contracts import Channel, Segment, Video
from transcript_collector.pipeline import index_channel
from transcript_collector.store import TranscriptStore
from transcript_collector.youtube import (
    CaptionsUnavailable,
    SourceBlocked,
    SourceError,
    YouTube,
    parse_json3,
)


def caption_data(text="caption", start=1250):
    return {
        "events": [{"tStartMs": start, "dDurationMs": 2000, "segs": [{"utf8": text}]}]
    }


class CaptionTests(unittest.TestCase):
    def test_selects_only_requested_language_manual_then_automatic(self):
        for requested, manual, automatic, expected in [
            ("vi", ["vi", "en"], ["vi", "en"], "manual-vi"),
            ("vi", ["en"], ["vi", "en"], "auto-vi"),
            ("vi", ["en-US"], ["vi", "en"], "auto-vi"),
            ("en", ["en-US"], ["en"], "manual-en-US"),
            ("en-US", ["en"], ["en-US"], "auto-en-US"),
            ("EN", ["en-US", "en"], [], "manual-en"),
            ("en-us", ["en", "en-US"], [], "manual-en-US"),
            ("en-us", ["en-GB"], ["en-US"], "auto-en-US"),
            ("vi", [], ["vi", "en"], "auto-vi"),
            ("vi", [], ["en"], None),
            ("en", [], ["en-translated"], "auto-en-translated"),
            ("EN", [], ["en-translated"], "auto-en-translated"),
            ("vi", [], [], None),
        ]:
            with self.subTest(requested=requested, expected=expected):
                source = YouTube(requested)
                ydl = MagicMock()
                ydl.__enter__.return_value = ydl

                def tracks(languages, kind):
                    return {
                        lang: [
                            {
                                "ext": "json3",
                                "url": f"https://example.org/{kind}-{lang}"
                                + ("?tlang=en" if lang.endswith("translated") else ""),
                            }
                        ]
                        for lang in languages
                    }

                ydl.extract_info.return_value = {
                    "subtitles": tracks(manual, "manual"),
                    "automatic_captions": tracks(automatic, "auto"),
                }
                ydl.urlopen.return_value = io.BytesIO(
                    json.dumps(caption_data()).encode()
                )
                with patch.object(source, "_downloader", return_value=ydl):
                    if expected:
                        self.assertEqual(
                            source.transcript("v"), [Segment("caption", 1.25, 2)]
                        )
                        ydl.urlopen.assert_called_once_with(
                            f"https://example.org/{expected}"
                            + ("?tlang=en" if expected.endswith("translated") else "")
                        )
                        self.assertEqual(
                            source.transcript_mode,
                            "translated"
                            if expected.endswith("translated")
                            else "manual"
                            if expected.startswith("manual")
                            else "auto_generated",
                        )
                        self.assertEqual(
                            source.transcript_language,
                            "en"
                            if expected.endswith("translated")
                            else expected.split("-", 1)[1],
                        )
                    else:
                        with self.assertRaises(CaptionsUnavailable):
                            source.transcript("v")
                        ydl.urlopen.assert_not_called()

    def test_channel_urls_decode_unicode_handles_before_validation(self):
        source = YouTube("vi")
        with patch.object(
            source, "_extract", return_value={"channel_id": "UCtest", "channel": "test"}
        ) as extract:
            for url in (
                "https://www.youtube.com/@kênhthử",
                "https://www.youtube.com/@k%C3%AAnhth%E1%BB%AD/videos",
            ):
                with self.subTest(url=url):
                    self.assertEqual(source.resolve(url).id, "UCtest")
                    extract.assert_called_with(
                        "https://www.youtube.com/@kênhthử/videos", playlistend=1
                    )
            extract.reset_mock()
            for path in ("@channel%2Fbad", "@channel%3Fbad", "@channel%FF"):
                with self.subTest(path=path), self.assertRaises(SourceError):
                    source.resolve("https://www.youtube.com/" + path)
            extract.assert_not_called()

    def test_audio_metadata_is_reused_for_the_same_video(self):
        source = YouTube("en")
        source._video_info = (
            "v",
            {
                "subtitles": {
                    "en": [{"ext": "json3", "url": "https://example.org/captions"}]
                }
            },
        )
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.urlopen.return_value = io.BytesIO(json.dumps(caption_data()).encode())
        with patch.object(source, "_downloader", return_value=ydl):
            source.transcript("v")
        ydl.extract_info.assert_not_called()

    def test_blocked_requests_remain_distinct_from_other_errors(self):
        for message, expected in [
            ("HTTP Error 429", SourceBlocked),
            ("Sign in to confirm you're not a bot", SourceBlocked),
            ("HTTP Error 403", SourceError),
            ("Failed to resolve", SourceError),
        ]:
            with self.subTest(message=message):
                source = YouTube("en")
                ydl = MagicMock()
                ydl.__enter__.return_value = ydl
                ydl.extract_info.side_effect = RuntimeError(message)
                with (
                    patch.object(source, "_downloader", return_value=ydl),
                    self.assertRaises(expected),
                ):
                    source.transcript("v")

    def test_json3_preserves_late_segments_and_rejects_invalid_times(self):
        data = caption_data("first")
        data["events"] += caption_data("last", 900000)["events"]
        self.assertEqual(
            parse_json3(data), [Segment("first", 1.25, 2), Segment("last", 900, 2)]
        )
        for start in (-1, float("nan")):
            with self.subTest(start=start), self.assertRaises(SourceError):
                parse_json3(caption_data(start=start))

    def test_channel_url_normalization_accepts_standard_youtube_tabs(self):
        source = YouTube("en")
        tabs = (
            "videos",
            "shorts",
            "streams",
            "featured",
            "about",
            "playlists",
            "podcasts",
            "community",
            "live",
            "membership",
            "store",
            "releases",
            "channels",
            "search",
        )
        with patch.object(
            source, "_extract", return_value={"channel_id": "UCtest", "channel": "test"}
        ) as extract:
            for tab in tabs:
                with self.subTest(tab=tab):
                    res = source.resolve(f"https://www.youtube.com/@HubermanLab/{tab}")
                    self.assertEqual(res.id, "UCtest")
                    extract.assert_called_with(
                        "https://www.youtube.com/@HubermanLab/videos", playlistend=1
                    )
            with self.assertRaises(SourceError):
                source.resolve("https://www.youtube.com/@channel%2Fbad")

    def test_json3_handles_null_timestamp_and_duration_safely(self):
        data1 = {
            "events": [
                {"tStartMs": 1000, "dDurationMs": None, "segs": [{"utf8": "hello"}]}
            ]
        }
        self.assertEqual(parse_json3(data1), [Segment("hello", 1.0, 0.0)])

        data2 = {
            "events": [
                {"tStartMs": None, "dDurationMs": 2000, "segs": [{"utf8": "skip"}]},
                {"tStartMs": 3000, "dDurationMs": 1500, "segs": [{"utf8": "keep"}]},
            ]
        }
        self.assertEqual(parse_json3(data2), [Segment("keep", 3.0, 1.5)])

        for bad_val in ("not_a_number", -1, float("nan")):
            with self.subTest(bad_val=bad_val), self.assertRaises(SourceError):
                parse_json3(
                    {
                        "events": [
                            {
                                "tStartMs": bad_val,
                                "dDurationMs": 1000,
                                "segs": [{"utf8": "err"}],
                            }
                        ]
                    }
                )

    def test_transcript_candidate_loop_falls_back_on_track_errors(self):
        source = YouTube("en")
        source._video_info = (
            "v",
            {
                "subtitles": {
                    "en": [
                        {"ext": "json3", "url": "https://example.org/bad_captions"},
                        {"ext": "json3", "url": "https://example.org/good_captions"},
                    ]
                }
            },
        )
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        bad_json = json.dumps(
            {
                "events": [
                    {
                        "tStartMs": "bad",
                        "dDurationMs": 1000,
                        "segs": [{"utf8": "bad"}],
                    }
                ]
            }
        ).encode()
        good_json = json.dumps(caption_data("recovered")).encode()
        ydl.urlopen.side_effect = [io.BytesIO(bad_json), io.BytesIO(good_json)]
        with patch.object(source, "_downloader", return_value=ydl):
            segments = source.transcript("v")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "recovered")


class CollectionFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TranscriptStore(str(self.root / "results.sqlite3"))
        self.addCleanup(self.store.close)
        self.channel = Channel("c", "channel", "https://example.org")
        self.source = Mock(transcript_mode="manual", transcript_language="en")
        self.source.videos.return_value = [Video(v, "c", v) for v in ("a", "b", "c")]
        self.source.download_audio.side_effect = self.download
        self.source.transcript.return_value = [Segment("text", 0, 1)]

    def download(self, video_id, directory):
        path = Path(directory) / "audio.webm"
        path.write_bytes(b"audio")
        return path

    def collect(self, mode="direct", transcriber=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return index_channel(
                self.source,
                self.store,
                self.channel,
                "en",
                mode=mode,
                transcriber=transcriber,
            )

    def test_direct_block_stops_collection_and_keeps_inventory(self):
        self.source.transcript.side_effect = SourceBlocked("blocked")
        counts = self.collect()
        self.assertEqual((counts["blocked"], counts["deferred"]), (1, 2))
        self.source.transcript.assert_called_once_with("a")
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM videos").fetchone()[0], 3
        )
        self.source.download_audio.assert_not_called()
        self.assertFalse(self.store.has_transcript("a"))

    def test_direct_interrupt_keeps_previous_transcripts(self):
        self.source.transcript.side_effect = [
            [Segment("saved", 0, 1)],
            KeyboardInterrupt,
        ]
        with self.assertRaises(KeyboardInterrupt):
            self.collect()
        self.assertTrue(self.store.cached("a", "youtube", "", language="en"))
        self.assertFalse(self.store.has_transcript("b"))
        self.source.download_audio.assert_not_called()
        self.assertEqual(self.source.transcript.call_count, 2)

    def test_interrupt_during_whisper_removes_intermediate_audio(self):
        transcriber = Mock(config="medium", transcript_language="en")
        transcriber.name = "whisper"
        transcriber.transcript.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.collect(mode="generate", transcriber=transcriber)
        transcriber.transcript.assert_called_once()
        self.assertFalse(Path(transcriber.transcript.call_args.args[0]).exists())
        self.source.transcript.assert_not_called()

    def test_generate_block_during_download_stops_before_engine_and_next_video(self):
        self.source.download_audio.side_effect = SourceBlocked("HTTP 429")
        engine = Mock(config="medium", transcript_language="en")
        engine.name = "whisper"
        counts = self.collect(mode="generate", transcriber=engine)
        self.assertEqual(
            (counts["blocked"], counts["deferred"], counts["audio_errors"]), (1, 2, 1)
        )
        self.source.download_audio.assert_called_once()
        engine.transcript.assert_not_called()
        self.source.transcript.assert_not_called()


class TranslationTests(unittest.TestCase):
    def test_originals_precede_translations_and_url_defines_destination(self):
        def track(url):
            return [{"ext": "json3", "url": url}]

        translation = "https://example.org/captions?lang=en&tlang=vi"
        for manual, automatic, expected, language in (
            (
                {"vi": track("https://example.org/manual")},
                {"vi": track(translation)},
                "https://example.org/manual",
                "vi",
            ),
            (
                {"en": track("https://example.org/english")},
                {
                    "vi": track(translation),
                    "vi-orig": track("https://example.org/original"),
                },
                "https://example.org/original",
                "vi",
            ),
            ({}, {"vi-en": track(translation)}, translation, "vi"),
            ({}, {"vi": track("https://example.org/wrong?tlang=fr")}, None, None),
            ({}, {"vi-en": track("https://example.org/wrong?tlang=en")}, None, None),
            ({}, {"vi": track("https://example.org/empty?tlang=")}, None, None),
        ):
            with self.subTest(expected=expected):
                source = YouTube("vi")
                ydl = MagicMock()
                ydl.__enter__.return_value = ydl
                ydl.extract_info.return_value = {
                    "subtitles": manual,
                    "automatic_captions": automatic,
                }
                ydl.urlopen.return_value = io.BytesIO(
                    json.dumps(caption_data()).encode()
                )
                with patch.object(source, "_downloader", return_value=ydl) as factory:
                    if expected:
                        self.assertEqual(
                            source.transcript("v"), [Segment("caption", 1.25, 2)]
                        )
                        self.assertEqual(source.transcript_language, language)
                        ydl.urlopen.assert_called_once_with(expected)
                    else:
                        with self.assertRaises(CaptionsUnavailable):
                            source.transcript("v")
                        ydl.urlopen.assert_not_called()
                self.assertTrue(factory.call_args.kwargs["writeautomaticsub"])

    def test_empty_original_falls_back_to_translation(self):
        source = YouTube("vi")
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = {
            "subtitles": {"vi": [{"ext": "json3", "url": "https://example.org/empty"}]},
            "automatic_captions": {
                "vi-en": [
                    {"ext": "json3", "url": "https://example.org/translation?tlang=vi"}
                ]
            },
        }
        ydl.urlopen.side_effect = [
            io.BytesIO(b'{"events": []}'),
            io.BytesIO(json.dumps(caption_data("translation")).encode()),
        ]
        with patch.object(source, "_downloader", return_value=ydl):
            self.assertEqual(source.transcript("v")[0].text, "translation")
        self.assertEqual(source.transcript_language, "vi")
        self.assertEqual(source.transcript_mode, "translated")

    def test_blocked_translation_is_not_treated_as_missing(self):
        source = YouTube("vi")
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = {
            "automatic_captions": {
                "vi-en": [
                    {"ext": "json3", "url": "https://example.org/translation?tlang=vi"}
                ]
            }
        }
        ydl.urlopen.side_effect = SourceBlocked("429")
        with (
            patch.object(source, "_downloader", return_value=ydl),
            self.assertRaises(SourceBlocked),
        ):
            source.transcript("v")

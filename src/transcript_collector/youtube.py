import json
import logging
import math
import re
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from .contracts import Channel, Segment, Video
from .errors import CaptionsUnavailable as CaptionsUnavailable
from .errors import SourceBlocked as SourceBlocked
from .errors import SourceError as SourceError
from .errors import VideoNotReady as VideoNotReady

logger = logging.getLogger(__name__)


def video_url(value: str) -> str | None:
    """Recognize a video URL and strip playlist, tracking and timestamp options.

    Return None for other inputs, which the channel resolver validates separately.
    A malformed video URL is an error, never a request to collect its channel.
    """
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.hostname
    path = parsed.path.rstrip("/")
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = path.removeprefix("/")
    elif host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        if path == "/watch":
            ids = parse_qs(parsed.query).get("v", [])
            video_id = ids[0] if len(ids) == 1 else ""
        elif re.match(r"^/(shorts|live|embed)(?:/|$)", path):
            video_id = path.split("/", 2)[-1] if path.count("/") == 2 else ""
        else:
            return None
    else:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise SourceError("Invalid YouTube video URL; expected one video ID.")
    return f"https://www.youtube.com/watch?v={video_id}"


def check_video_ready(info: Mapping[str, Any], video_id: str) -> None:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise VideoNotReady(
            f"Skipping live or upcoming video {video_id}; retry after it ends."
        )


def playlist_url(value: str) -> str | None:
    """Route explicit playlist URLs; yt-dlp validates and interprets the URL."""
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname
        not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
        or parsed.path.rstrip("/") != "/playlist"
    ):
        return None
    return value


def request_error(exc: Exception, context: str) -> SourceError:
    if isinstance(exc, SourceError):
        return exc
    message = str(exc).casefold()
    if any(word in message for word in ("429", "too many requests", "not a bot")):
        return SourceBlocked(f"{context}: YouTube blocked requests; retry later.")
    if any(word in message for word in ("403", "forbidden", "po token")):
        reason = "access denied; check client/EJS/PO Token"
    elif any(
        word in message
        for word in ("timeout", "timed out", "resolve", "connection", "network")
    ):
        reason = "temporary network failure"
    else:
        reason = "request failed"
    return SourceError(f"{context}: {reason}.")


class _ExtractLogger:
    """Suppress the expected missing-tab message; retain other errors."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        if any(
            word in message.casefold()
            for word in ("po token", "javascript", "challenge")
        ):
            logger.warning(
                "yt-dlp JavaScript challenge or PO Token warning; check extractor setup",
                extra={"phase": "source_warning"},
            )
        elif "retry" in message.casefold():
            logger.warning(
                "yt-dlp retrying a request or fragment", extra={"phase": "source_retry"}
            )

    def error(self, message: str) -> None:
        if not re.search(
            r"This channel does not have a (videos|shorts|streams) tab", message
        ):
            logger.error(
                "%s",
                request_error(RuntimeError(message), "yt-dlp"),
                extra={"phase": "collection"},
            )


class YouTube:
    def __init__(self, language: str | None = None, cookies: str | None = None):
        if language is not None and not re.fullmatch(
            r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language
        ):
            raise ValueError("Specify one caption language, for example vi or en.")
        self.transcript_mode = "unknown"
        self.transcript_language = "und"
        self.language = language
        self.cookies = cookies
        self._video_info = None

    def _downloader(self, **options):
        from deno import find_deno_bin
        from yt_dlp import YoutubeDL

        settings = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "sleep_interval_requests": 2,
            "concurrent_fragment_downloads": 2,
            "skip_unavailable_fragments": False,
            "js_runtimes": {"deno": {"path": find_deno_bin()}},
            "logger": _ExtractLogger(),
            **options,
        }
        if self.cookies:
            settings["cookiefile"] = self.cookies
        ydl = YoutubeDL(cast(Any, settings))
        original = ydl.urlopen

        def open_request(req):
            try:
                return original(req)
            except Exception as exc:
                error = request_error(exc, "YouTube request")
                if isinstance(error, SourceBlocked):
                    raise error from exc
                raise

        ydl.urlopen = open_request
        return ydl

    def _extract(self, url: str, **options) -> dict[str, Any] | None:
        settings = {
            "skip_download": True,
            "extract_flat": True,
            **options,
        }
        started = perf_counter()
        logger.debug(
            "YouTube metadata extraction started; flat=%s",
            settings["extract_flat"],
            extra={"phase": "metadata_resolve"},
        )
        try:
            with self._downloader(**settings) as ydl:
                result = cast(
                    dict[str, Any] | None, ydl.extract_info(url, download=False)
                )
                logger.debug(
                    "YouTube metadata extraction finished; elapsed=%.3fs",
                    perf_counter() - started,
                    extra={
                        "phase": "metadata_resolve",
                        "elapsed_seconds": perf_counter() - started,
                    },
                )
                return result
        except Exception as exc:
            if re.search(
                r"This channel does not have a (videos|shorts|streams) tab", str(exc)
            ):
                return {"entries": []}
            raise request_error(exc, "YouTube request") from exc

    def resolve(self, value: str) -> Channel:
        if re.fullmatch(r"UC[\w-]{22}", value):
            url = f"https://www.youtube.com/channel/{value}"
        elif value.startswith("@") and re.fullmatch(r"@[\w.-]+", value):
            url = f"https://www.youtube.com/{value}"
        elif value.startswith(("http://", "https://")):
            parsed = urlparse(value)
            if parsed.hostname not in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
            }:
                raise SourceError(
                    "Use a youtube.com channel URL, @handle or UC channel ID."
                )
            path = unquote(parsed.path).rstrip("/")
            path = re.sub(
                r"/(videos|shorts|streams|featured|about|playlists|podcasts|community|live|membership|store|releases|channels|search)$",
                "",
                path,
            )
            if not re.fullmatch(r"/(?:@[\w.-]+|(?:channel|c|user)/[\w.-]+)", path):
                raise SourceError(
                    "This is not a channel URL. Use its @handle or /channel/UC… URL."
                )
            url = f"https://www.youtube.com{path}"
        else:
            raise SourceError(
                "Use a youtube.com channel URL, @handle or UC channel ID."
            )
        info = self._extract(url + "/videos", playlistend=1) or {}
        identifier = info.get("channel_id")
        if not identifier:
            raise SourceError("Could not resolve this channel's ID.")
        logger.info(
            "Channel metadata resolved; channel_id=%s",
            identifier,
            extra={"phase": "metadata_resolve"},
        )
        return Channel(
            identifier,
            info.get("channel")
            or info.get("uploader")
            or info.get("title")
            or identifier,
            f"https://www.youtube.com/channel/{identifier}",
        )

    def _entries(self, url: str) -> list[dict[str, Any]]:
        """Use yt-dlp's native collection extraction for both channels and playlists."""
        info = self._extract(url, noplaylist=False, extract_flat=True)
        if not info or "entries" not in info:
            raise SourceError("Could not list videos for this URL.")
        entries = {}
        for e in info.get("entries") or []:
            if (
                e
                and isinstance(e.get("id"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]+", e["id"])
                and not e.get("is_live")
                and e.get("live_status") not in {"is_live", "is_upcoming"}
            ):
                entries.setdefault(e["id"], e)
        logger.info(
            "YouTube listing resolved; unique_available_videos=%d",
            len(entries),
            extra={"phase": "listing_end"},
        )
        return list(entries.values())

    def videos(self, channel: Channel) -> list[Video]:
        return [
            Video(
                e["id"],
                channel.id,
                e.get("title") or e["id"],
                e.get("description") or "",
            )
            for e in self._entries(channel.url + "/videos")
        ]

    def resolve_video(self, value: str) -> tuple[Channel, Video]:
        url = video_url(value)
        if url is None:
            raise SourceError("Use a YouTube video URL.")
        video_id = parse_qs(urlparse(url).query)["v"][0]
        started = perf_counter()
        logger.info(
            "Video %s metadata resolution started",
            video_id,
            extra={"phase": "metadata_resolve", "video_id": video_id},
        )
        info = self._extract(
            url,
            extract_flat=False,
            writeautomaticsub=True,
            format="bestaudio/best",
        )
        if (
            not info
            or info.get("_type", "video") != "video"
            or info.get("id") != video_id
        ):
            raise SourceError(f"Could not resolve video {video_id}.")
        check_video_ready(info, video_id)
        channel_id = info.get("channel_id")
        if not channel_id:
            raise SourceError(f"Could not resolve the channel for video {video_id}.")
        channel = Channel(
            channel_id,
            info.get("channel") or info.get("uploader") or channel_id,
            f"https://www.youtube.com/channel/{channel_id}",
        )
        video = Video(
            video_id,
            channel_id,
            info.get("title") or video_id,
            info.get("description") or "",
        )
        self._video_info = (video_id, info)
        logger.info(
            "Video %s metadata resolved; channel_id=%s elapsed=%.3fs",
            video_id,
            channel_id,
            perf_counter() - started,
            extra={
                "phase": "metadata_resolve",
                "video_id": video_id,
                "elapsed_seconds": perf_counter() - started,
            },
        )
        return channel, video

    def playlist_videos(self, value: str) -> list[str]:
        """List unique video IDs in playlist order without downloading media."""
        url = playlist_url(value)
        if url is None:
            raise SourceError("Use a YouTube playlist URL with a list parameter.")
        return [entry["id"] for entry in self._entries(url)]

    def _video_metadata(self, ydl, video_id: str):
        """Reuse resolved metadata for the requested video before extracting again."""
        cached = self._video_info
        logger.debug(
            "Video %s metadata cache %s",
            video_id,
            "hit" if cached and cached[0] == video_id else "miss",
            extra={"phase": "metadata_resolve", "video_id": video_id},
        )
        info = (
            cached[1]
            if cached and cached[0] == video_id
            else ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        )
        if not info:
            raise SourceError(f"Video unavailable: {video_id}.")
        return info

    def transcript(self, video_id: str) -> list[Segment]:
        """Prefer manual, original automatic captions, then translated captions."""
        if not self.language:
            raise ValueError("Specify one caption language.")
        requested_language = self.language.casefold()
        started = perf_counter()
        logger.info(
            "Video %s caption selection started; language=%s",
            video_id,
            self.language,
            extra={
                "phase": "caption_select",
                "video_id": video_id,
                "language": self.language,
            },
        )
        try:
            with self._downloader(
                skip_download=True, extract_flat=False, writeautomaticsub=True
            ) as ydl:
                info = self._video_metadata(ydl, video_id)
                check_video_ready(info, video_id)
                # yt-dlp can use synthetic translation keys such as vi-en.
                # The URL's tlang is the actual destination language.
                for translated in (False, True):
                    for field in ("subtitles", "automatic_captions"):
                        tracks = cast(
                            dict[str, list[dict[str, Any]]], info.get(field) or {}
                        )
                        for key in sorted(
                            tracks,
                            key=lambda key: (
                                key.removesuffix("-orig").casefold()
                                != requested_language,
                                key.casefold() != requested_language,
                                key,
                            ),
                        ):
                            original_language = key.removesuffix("-orig")
                            for track in tracks[key]:
                                if track.get("ext") != "json3":
                                    continue
                                query = parse_qs(
                                    urlparse(track.get("url", "")).query,
                                    keep_blank_values=True,
                                )
                                is_translation = "tlang" in query
                                if is_translation != translated:
                                    continue
                                target = (
                                    query["tlang"][-1]
                                    if translated
                                    else original_language
                                )
                                if (
                                    target.casefold() != requested_language
                                    and not target.casefold().startswith(
                                        requested_language + "-"
                                    )
                                ):
                                    continue
                                logger.debug(
                                    "Video %s fetching caption track; language=%s translated=%s",
                                    video_id,
                                    target,
                                    translated,
                                    extra={
                                        "phase": "caption_fetch",
                                        "video_id": video_id,
                                        "language": target,
                                    },
                                )
                                try:
                                    with ydl.urlopen(track["url"]) as response:
                                        data = json.loads(response.read().decode("utf-8"))
                                    segments = parse_json3(data)
                                except SourceBlocked:
                                    raise
                                except (
                                    CaptionsUnavailable,
                                    SourceError,
                                    OSError,
                                    ValueError,
                                    TypeError,
                                    json.JSONDecodeError,
                                ):
                                    continue
                                self.transcript_mode = (
                                    "translated"
                                    if translated
                                    else "manual"
                                    if field == "subtitles"
                                    else "auto_generated"
                                )
                                self.transcript_language = target
                                logger.info(
                                    "Video %s captions selected; language=%s source_mode=%s segments=%d elapsed=%.3fs",
                                    video_id,
                                    target,
                                    self.transcript_mode,
                                    len(segments),
                                    perf_counter() - started,
                                    extra={
                                        "phase": "caption_select",
                                        "video_id": video_id,
                                        "language": target,
                                        "source_mode": self.transcript_mode,
                                        "segments": len(segments),
                                        "elapsed_seconds": perf_counter() - started,
                                    },
                                )
                                return segments
                logger.info(
                    "Video %s captions unavailable; language=%s elapsed=%.3fs",
                    video_id,
                    self.language,
                    perf_counter() - started,
                    extra={
                        "phase": "caption_unavailable",
                        "video_id": video_id,
                        "language": self.language,
                        "elapsed_seconds": perf_counter() - started,
                    },
                )
                raise CaptionsUnavailable(
                    f"No captions or translations in {self.language} for {video_id}."
                )
        except Exception as exc:
            raise request_error(exc, f"yt-dlp captions for {video_id}") from exc

    def download_audio(self, video_id: str, directory: str) -> Path:
        started = perf_counter()
        last_time = started
        last_percent = -10.0
        progress_lock = Lock()
        logger.info(
            "Video %s audio download started",
            video_id,
            extra={"phase": "audio_download", "video_id": video_id},
        )

        def progress(data):
            nonlocal last_time, last_percent
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            percent = 100 * downloaded / total if total else 0
            now = perf_counter()
            with progress_lock:
                if data.get("status") == "finished" or (
                    data.get("status") == "downloading"
                    and (percent >= last_percent + 10 or now - last_time >= 5)
                ):
                    last_time, last_percent = now, percent
                    logger.info(
                        "Video %s audio download %s; bytes=%d total_bytes=%d percent=%.1f elapsed=%.3fs",
                        video_id,
                        data.get("status"),
                        downloaded,
                        total,
                        percent,
                        now - started,
                        extra={
                            "phase": "audio_download",
                            "video_id": video_id,
                            "elapsed_seconds": now - started,
                        },
                    )

        options = {
            "progress_hooks": [progress],
            "format": "bestaudio/best",
            "outtmpl": str(Path(directory) / "audio.%(ext)s"),
        }
        try:
            with self._downloader(**options) as ydl:
                info = self._video_metadata(ydl, video_id)
                self._video_info = (video_id, info)
                check_video_ready(info, video_id)
                ydl.process_info(info)
                result = Path(ydl.prepare_filename(info))
                logger.info(
                    "Video %s audio download finished; elapsed=%.3fs",
                    video_id,
                    perf_counter() - started,
                    extra={
                        "phase": "audio_download",
                        "video_id": video_id,
                        "elapsed_seconds": perf_counter() - started,
                    },
                )
                return result
        except SourceError:
            raise
        except Exception as exc:
            error = request_error(exc, f"Audio download for {video_id}")
            raise error from exc


def parse_json3(data: dict) -> list[Segment]:
    segments = []
    for event in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
        raw_start = event.get("tStartMs")
        if text and raw_start is not None:
            raw_duration = event.get("dDurationMs")
            try:
                start = float(raw_start) / 1000
                duration = (
                    float(raw_duration) / 1000 if raw_duration is not None else 0.0
                )
            except (ValueError, TypeError):
                raise SourceError("Invalid caption timing.")
            if (
                not math.isfinite(start)
                or not math.isfinite(duration)
                or start < 0
                or duration < 0
            ):
                raise SourceError("Invalid caption timing.")
            segments.append(Segment(text, start, duration))
    if not segments:
        raise CaptionsUnavailable("Transcript contains no text.")
    return segments

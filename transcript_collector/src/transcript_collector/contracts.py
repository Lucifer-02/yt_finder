from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    url: str


@dataclass(frozen=True)
class Video:
    id: str
    channel_id: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class Segment:
    text: str
    start: float
    duration: float


class VideoSource(Protocol):
    # Actual language of the most recent successful transcript() call.
    transcript_language: str
    transcript_mode: str

    def transcript(self, video_id: str) -> list[Segment]: ...
    def download_audio(self, video_id: str, directory: str) -> Path: ...


class ChannelSource(VideoSource, Protocol):
    def videos(self, channel: Channel) -> list[Video]: ...


class PlaylistSource(VideoSource, Protocol):
    def playlist_videos(self, value: str) -> list[str]: ...
    def resolve_video(self, value: str) -> tuple[Channel, Video]: ...


class TranscriptionEngine(Protocol):
    name: str

    @property
    def config(self) -> str: ...

    # Language detected by the most recent successful transcript() call.
    transcript_language: str

    def transcript(self, audio: str | Path) -> list[Segment]: ...


class TranscriptRepository(Protocol):
    def save_channel(self, channel: Channel) -> None: ...
    def save_inventory(self, videos: list[Video]) -> None: ...
    def cached(
        self,
        video_id: str,
        source: str,
        config: str,
        *,
        language: str | None = None,
        source_mode: str | None = None,
    ) -> bool: ...
    def has_transcript(self, video_id: str) -> bool: ...
    def save_video(
        self,
        video: Video,
        source: str,
        config: str,
        segments: list[Segment],
        *,
        language: str,
        source_mode: str,
    ) -> None: ...

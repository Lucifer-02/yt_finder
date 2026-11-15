"""Immutable selected videos and pure transcript windowing, with no database access."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    text: str
    start: float
    duration: float


@dataclass(frozen=True, slots=True)
class VideoTranscript:
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    description: str
    transcript_source: str
    source_mode: str
    language: str
    transcript_config: str
    segments: tuple[TranscriptSegment, ...]


class Window(TypedDict):
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    transcript_source: str
    source_mode: str
    language: str
    transcript_config: str
    start: float
    end: float
    text: str


def windows_for_video(
    video: VideoTranscript,
    segments: tuple[TranscriptSegment, ...],
    window: float = 60,
    stride: float = 30,
) -> list[Window]:
    result: list[Window] = []
    anchor = -stride
    for i, segment in enumerate(segments):
        if segment.start < anchor + stride:
            continue
        anchor = segment.start
        group = []
        for j in range(i, len(segments)):
            following = segments[j]
            if following.start >= anchor + window:
                break
            group.append(following)
        result.append(
            {
                "video_id": video.video_id,
                "channel_id": video.channel_id,
                "channel_title": video.channel_title,
                "title": video.title,
                "transcript_source": video.transcript_source,
                "source_mode": video.source_mode,
                "language": video.language,
                "transcript_config": video.transcript_config,
                "start": anchor,
                "end": max(s.start + s.duration for s in group),
                "text": " ".join(s.text.strip() for s in group),
            }
        )
    return result


def video_windows(videos: Sequence[VideoTranscript]) -> list[Window]:
    return [
        window
        for video in videos
        for window in windows_for_video(video, video.segments)
    ]

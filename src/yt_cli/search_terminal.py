"""Terminal-only progress rendering."""

import sys

from video_search.events import EmbeddingProgress


def print_progress(event: EmbeddingProgress) -> None:
    if event.total > 256 and (
        event.total <= 4096
        or event.completed % 2048 == 0
        or event.completed == event.total
    ):
        print(
            f"Embedded {event.completed}/{event.total} transcript chunks.",
            file=sys.stderr,
        )

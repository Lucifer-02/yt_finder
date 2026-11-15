"""Terminal rendering belongs to the CLI adapter, not the collection pipeline."""

import sys

from transcript_collector.events import CollectionEvent


def print_event(event: CollectionEvent) -> None:
    if event.kind == "video_started":
        print(
            f"[{event.position}/{event.total}] {'[cached] ' if event.cached else ''}{event.title}",
            file=sys.stderr,
        )
    elif event.kind == "summary":
        print(
            f"Scope: {event.scope}. "
            + ", ".join(
                f"{key}={value}" for key, value in (event.counts or {}).items()
            ),
            file=sys.stderr,
        )
    elif event.message:
        print(event.message, file=sys.stderr)

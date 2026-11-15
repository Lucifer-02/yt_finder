"""Structured progress for terminal, web or other application adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CollectionEvent:
    kind: Literal["message", "video_started", "video_finished", "summary"]
    message: str = ""
    scope: str = ""
    video_id: str = ""
    title: str = ""
    position: int = 0
    total: int = 0
    cached: bool = False
    counts: dict[str, int] | None = None


EventHandler = Callable[[CollectionEvent], None]


class EventDeliveryError(RuntimeError):
    """An application's event handler failed; this is not a source failure."""


def emit(handler: EventHandler | None, event: CollectionEvent) -> None:
    if handler is not None:
        try:
            handler(event)
        except Exception as exc:
            raise EventDeliveryError(f"Collection event handler failed: {exc}") from exc

"""Data-only worker boundaries; no dependency on the CLI or model libraries."""

from collections.abc import Callable
from typing import Any, Protocol

Payload = dict[str, Any]
EventHandler = Callable[[Payload], None]


class CollectorGateway(Protocol):
    """Adapter maps payload to CollectionRequest in a collector worker."""

    def collect(
        self, request: Payload, *, on_event: EventHandler | None = None
    ) -> Payload: ...


class SearchGateway(Protocol):
    """Adapter owns a worker-local SearchSession in the search interpreter."""

    def search(
        self,
        query: str,
        top: int = 5,
        min_similarity: float | None = None,
        *,
        moments_per_video: int = 3,
    ) -> list[Payload]: ...

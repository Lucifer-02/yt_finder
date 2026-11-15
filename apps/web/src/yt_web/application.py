"""Gateway application factory with explicit collector and search dependencies."""

from dataclasses import dataclass

from .ports import CollectorGateway, EventHandler, Payload, SearchGateway


@dataclass(frozen=True)
class WebApplication:
    collector: CollectorGateway
    searcher: SearchGateway

    def collect(
        self, request: Payload, *, on_event: EventHandler | None = None
    ) -> Payload:
        return self.collector.collect(request, on_event=on_event)

    def search(
        self,
        query: str,
        top: int = 5,
        min_similarity: float | None = None,
        *,
        moments_per_video: int = 3,
    ) -> list[Payload]:
        return self.searcher.search(
            query, top, min_similarity, moments_per_video=moments_per_video
        )


def create_app(*, collector: CollectorGateway, search: SearchGateway) -> WebApplication:
    """Inject worker adapters; constructing the app never opens DBs or models.

    Gateways retain ownership of worker/session lifetime. Request validation and
    transcript/search rules remain in the component services.
    """
    return WebApplication(collector=collector, searcher=search)

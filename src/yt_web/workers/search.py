"""Persistent search session whose SQLite connections stay on the main thread."""

import os

from .protocol import Protocol


def main() -> None:
    protocol = Protocol()
    session = None
    try:
        from video_search.config import SearchConfig

        from .search_session import SearchSession

        while request := protocol.read():
            try:
                arguments = request["arguments"]
                if request["command"] == "configure":
                    if session is not None:
                        session.close()
                        session = None
                    # Set process-local offline mode before importing/loading HF models.
                    offline = arguments.pop("offline", False)
                    if offline:
                        os.environ["HF_HUB_OFFLINE"] = "1"
                    session = SearchSession(
                        SearchConfig(**arguments),
                        on_progress=lambda event: protocol.send(
                            "event",
                            payload={
                                "kind": "embedding",
                                "completed": event.completed,
                                "total": event.total,
                            },
                        ),
                    )
                    protocol.send("result", payload={"ready": True})
                elif request["command"] == "search":
                    if session is None:
                        raise ValueError("Search worker is not configured.")
                    protocol.send("result", payload=session.search(**arguments))
                else:
                    raise ValueError("Unknown search command.")
            except Exception as exc:
                protocol.send("error", kind=type(exc).__name__, message=str(exc))
    except KeyboardInterrupt:
        protocol.send("cancelled")
    except Exception as exc:
        protocol.send("error", kind=type(exc).__name__, message=str(exc))
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    main()

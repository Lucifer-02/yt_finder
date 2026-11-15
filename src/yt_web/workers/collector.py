"""Single collect invocation in the collector interpreter; no search imports."""

from dataclasses import asdict

from .protocol import Protocol


def main() -> None:
    protocol = Protocol()
    try:
        from transcript_collector.config import CollectionRequest

        from .collector_runtime import collect

        request = protocol.read()
        if request is None:
            return
        if request["command"] != "collect":
            raise ValueError("Unknown collection command.")
        result = collect(
            CollectionRequest(**request["arguments"]),
            on_event=lambda event: protocol.send("event", payload=asdict(event)),
        )
        protocol.send("result", payload=asdict(result))
    except KeyboardInterrupt:
        protocol.send("cancelled")
    except Exception as exc:
        protocol.send("error", kind=type(exc).__name__, message=str(exc))


if __name__ == "__main__":
    main()

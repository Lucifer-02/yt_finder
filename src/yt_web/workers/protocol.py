"""Reserve stdout for protocol messages even when model libraries print."""

import json
import logging
import sys
import threading
from contextlib import suppress
from typing import Any

from ..logs import record_payload


class ProtocolLogHandler(logging.Handler):
    def __init__(self, protocol: "Protocol"):
        super().__init__(logging.DEBUG)
        self.protocol = protocol

    def emit(self, record: logging.LogRecord) -> None:
        # Broken diagnostic delivery must not be reported as a source failure.
        with suppress(OSError, ValueError):
            self.protocol.send(
                "log", payload={**record_payload(record), **self.protocol.context}
            )


class Protocol:
    def __init__(self):
        self.output = sys.stdout
        sys.stdout = sys.stderr
        self.context: dict[str, str] = {}
        self.lock = threading.RLock()
        self.handler = ProtocolLogHandler(self)
        for name in (
            "transcript_collector",
            "video_search",
            "yt_workflows",
            "yt_web.workers",
        ):
            logger = logging.getLogger(name)
            logger.setLevel(logging.DEBUG)
            logger.addHandler(self.handler)
            logger.propagate = False

    def send(self, message_type: str, **fields: Any) -> None:
        with self.lock:
            self.output.write(
                json.dumps({"type": message_type, **fields}, ensure_ascii=False) + "\n"
            )
            self.output.flush()

    def read(self) -> dict[str, Any] | None:
        line = sys.stdin.readline()
        if not line:
            return None
        request = json.loads(line)
        self.context = {
            key: str(value)
            for key, value in request.get("context", {}).items()
            if key in {"job_id", "request_id"}
        }
        return request

"""Internal JSON-lines model worker transport, independent of CLI and web."""

import json
import logging
import os
import sys
import threading
from dataclasses import asdict
from types import SimpleNamespace

from .config import DATA_DIR, EvaluationRequest

_output = sys.stdout
_send_lock = threading.Lock()


def send(event):
    with _send_lock:
        _output.write(json.dumps(event, ensure_ascii=False) + "\n")
        _output.flush()


class ProtocolLogs(logging.Handler):
    def emit(self, record):
        context = {
            name: getattr(record, name)
            for name in (
                "phase",
                "video_id",
                "model",
                "elapsed_seconds",
                "completed",
                "total",
            )
            if isinstance(getattr(record, name, None), (str, int, float))
        }
        context["worker_pid"] = record.process
        send(
            {
                "type": "log",
                "record": {
                    "logger": record.name,
                    "level": record.levelno,
                    "message": record.getMessage(),
                    "context": context,
                },
            }
        )


def main():
    # Model libraries may print; reserve the original stream for JSON transport.
    sys.stdout = sys.stderr
    model_name = "unknown"
    try:
        line = sys.stdin.readline()
        if not line.strip():
            raise ValueError("Missing worker input payload.")
        payload = json.loads(line)
        model_name = str(payload.get("model") or "unknown")
        request = EvaluationRequest(**payload["request"])
        os.environ.setdefault("HF_HOME", str(DATA_DIR / "models"))
        if request.offline:
            os.environ["HF_HUB_OFFLINE"] = "1"
        handler = ProtocolLogs()
        for namespace in ("yt_workflows", "video_search"):
            logger = logging.getLogger(namespace)
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
        from .evaluation import run_model, validate_output_paths

        args = SimpleNamespace(**asdict(request), worker=model_name)
        args.corpus_file = payload["snapshot"]
        args.output = payload["target"]
        validate_output_paths(
            [args.db, args.dataset, args.corpus_file], [args.search_db, args.output]
        )
        report = run_model(
            args, on_progress=lambda event: send({"type": "progress", "event": event})
        )
    except Exception as exc:
        logging.getLogger("yt_workflows.eval_worker").error(
            "Evaluation model failed (%s)",
            type(exc).__name__,
            extra={"phase": "evaluation_model_failed", "model": model_name},
        )
        send(
            {
                "type": "result",
                "result": {
                    "model": model_name,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                },
            }
        )
        return
    send({"type": "result", "result": report})


if __name__ == "__main__":
    main()

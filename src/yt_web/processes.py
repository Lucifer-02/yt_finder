"""Sequential JSON-lines RPC to isolated component processes, never the CLI."""

import json
import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from time import perf_counter
from typing import Any

from .config import WebConfig
from .logs import current_context, log_context, relay_worker_log

logger = logging.getLogger(__name__)


class WorkerError(RuntimeError):
    def __init__(self, message: str, kind: str = "WorkerError"):
        super().__init__(message)
        self.kind = kind


class WorkerCancelled(WorkerError):
    pass


class ProcessWorker:
    def __init__(
        self,
        config: WebConfig,
        component: str,
        module: str,
        *,
        extra_env: dict[str, str] | None = None,
    ):
        self.config, self.component, self.module = config, component, module
        self.extra_env = extra_env or {}
        self._lock = threading.RLock()
        self._interrupted = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._context: dict[str, str] = {}

    def _start(self) -> subprocess.Popen[str]:
        if self._interrupted.is_set():
            raise WorkerCancelled("Đã dừng tác vụ.", "Cancelled")
        if self._process is not None and self._process.poll() is None:
            return self._process
        self.close()
        python = self.config.interpreter()
        if not python.is_file():
            raise WorkerError(
                f"Chưa có môi trường dùng chung tại {self.config.root / '.venv'}. "
                f"Chạy uv sync --all-extras từ thư mục {self.config.root} trước."
            )
        environment = os.environ.copy()
        source = str(self.config.root / "src")
        environment["PYTHONPATH"] = source + (
            os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH")
            else ""
        )
        environment.setdefault("HF_HOME", str(self.config.root / "data/models"))
        environment.update(self.extra_env)
        self._process = subprocess.Popen(
            [str(python), "-u", "-m", self.module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            start_new_session=os.name == "posix",
        )
        logger.info(
            "Worker started",
            extra={
                "phase": "worker_started",
                "component": self.component,
                "worker_pid": self._process.pid,
            },
        )
        if self._interrupted.is_set():
            self.interrupt()
            raise WorkerCancelled("Đã dừng tác vụ.", "Cancelled")
        return self._process

    def call(
        self,
        command: str,
        arguments: dict[str, Any],
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        with self._lock:
            self._context = current_context()
            started = perf_counter()
            process = self._start()
            if process.stdin is None or process.stdout is None:
                raise WorkerError("Worker pipes are unavailable.")
            try:
                process.stdin.write(
                    json.dumps(
                        {
                            "command": command,
                            "arguments": arguments,
                            "context": {
                                key: value
                                for key, value in self._context.items()
                                if key in {"job_id", "request_id"}
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                process.stdin.flush()
                logger.debug(
                    "Worker command sent",
                    extra={
                        "phase": "worker_call",
                        "component": self.component,
                        "command": command,
                    },
                )
                while line := process.stdout.readline():
                    reply = json.loads(line)
                    if reply["type"] == "log":
                        relay_worker_log(reply["payload"])
                    elif reply["type"] == "event":
                        if on_event is not None:
                            on_event(reply["payload"])
                    elif reply["type"] == "result":
                        logger.info(
                            "Worker command completed",
                            extra={
                                "phase": "worker_result",
                                "component": self.component,
                                "command": command,
                                "elapsed_seconds": perf_counter() - started,
                            },
                        )
                        return reply["payload"]
                    elif reply["type"] == "cancelled":
                        raise WorkerCancelled("Đã dừng tác vụ.", "Cancelled")
                    elif reply["type"] == "error":
                        logger.error(
                            "Worker command failed (%s)",
                            reply["kind"],
                            extra={
                                "phase": "worker_failed",
                                "component": self.component,
                                "command": command,
                                "elapsed_seconds": perf_counter() - started,
                            },
                        )
                        raise WorkerError(reply["message"], reply["kind"])
                raise WorkerError(
                    f"Worker {self.component} đã dừng. Kiểm tra môi trường và log của ứng dụng."
                )
            except (BrokenPipeError, json.JSONDecodeError) as exc:
                self.interrupt()
                raise WorkerError(f"Mất kết nối với worker {self.component}.") from exc
            except WorkerError:
                raise
            except BaseException:
                self.interrupt()
                self.close()
                raise

    def interrupt(self) -> None:
        self._interrupted.set()
        process = self._process
        if process is not None and process.poll() is None:
            with log_context(**self._context):
                logger.info(
                    "Interrupting worker group",
                    extra={
                        "phase": "worker_interrupt",
                        "component": self.component,
                        "worker_pid": process.pid,
                    },
                )
            with suppress(ProcessLookupError):
                self._signal(process, signal.SIGINT)

    @staticmethod
    def _signal(process: subprocess.Popen[str], sig: int) -> None:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        else:
            process.send_signal(sig)

    def force_stop(self) -> None:
        """Last-resort shutdown without waiting for a pending RPC lock."""
        process = self._process
        if process is not None and (os.name == "posix" or process.poll() is None):
            with log_context(**self._context):
                logger.warning(
                    "Force-stopping worker group",
                    extra={
                        "phase": "worker_force_stop",
                        "component": self.component,
                        "worker_pid": process.pid,
                    },
                )
            with suppress(ProcessLookupError):
                self._signal(process, signal.SIGKILL)

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if process is not None:
                if process.stdin is not None:
                    with suppress(BrokenPipeError, OSError):
                        process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        self._signal(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            self._signal(process, signal.SIGKILL)
                        process.wait(timeout=3)
                # A helper may outlive its worker and retain a protocol pipe.
                # Reap the complete group before releasing temporary audio.
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        self._signal(process, signal.SIGKILL)
                if process.stdout is not None:
                    with log_context(**self._context):
                        for line in process.stdout:
                            try:
                                reply = json.loads(line)
                                if reply.get("type") == "log":
                                    relay_worker_log(reply["payload"])
                            except (ValueError, KeyError):
                                continue
                    process.stdout.close()
                with log_context(**self._context):
                    logger.info(
                        "Worker group closed",
                        extra={
                            "phase": "worker_closed",
                            "component": self.component,
                            "worker_pid": process.pid,
                            "exit_code": process.returncode,
                        },
                    )

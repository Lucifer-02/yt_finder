"""Durable scoped logs, worker transport and output isolation without external calls."""

import contextlib
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.sqlite_fixture import SQLiteFixture
from tests.test_web import FakeCollector, FakeSearch
from yt_web.config import WebConfig
from yt_web.http import create_http_app
from yt_web.logs import LogStore, log_context
from yt_web.processes import ProcessWorker, WorkerError

ROOT = Path(__file__).resolve().parents[1]


class WebLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = WebConfig(
            ROOT,
            self.root / "input.sqlite3",
            self.root / "cache.sqlite3",
            self.root / "state.sqlite3",
        )

    def test_scoped_records_filters_and_persistence_across_rotation(self):
        path = self.root / "processing.jsonl"
        first = LogStore(path, max_bytes=1000, backups=2)
        second = LogStore(self.root / "other.jsonl")
        logger = logging.getLogger("yt_web")
        before = (logger.level, logger.propagate, logger.handlers[:])
        first.attach()
        second.attach()
        try:
            with log_context(
                store_id=first.identifier, job_id="job-a", request_id="request-a"
            ):
                for number in range(30):
                    logging.getLogger("yt_web.test").info(
                        "phase %s",
                        number,
                        extra={"phase": "commit", "video_id": "video"},
                    )
                logging.getLogger("yt_web.test").debug(
                    "cached", extra={"phase": "cache"}
                )
            with log_context(store_id=second.identifier, job_id="job-b"):
                logging.getLogger("yt_web.test").warning("other app")
            rows = first.read(level="DEBUG")["items"]
            self.assertGreater(len(rows), 1)
            self.assertLess(len(rows), 31)
            self.assertTrue(all(row["job_id"] == "job-a" for row in rows))
            self.assertTrue(all(row["request_id"] == "request-a" for row in rows))
            self.assertEqual(len(second.read()["items"]), 1)
            self.assertFalse(first.read(job_id="job-b")["items"])
            self.assertTrue(first.read(limit=1)["truncated"])
            last = first.read(level="DEBUG")["next_cursor"]
            self.assertFalse(first.read(after=last, level="DEBUG")["items"])
            self.assertTrue(first.read(after="rotated-cursor")["reset"])
            expected_ids = [row["id"] for row in rows]
        finally:
            first.close()
            second.close()
        self.assertEqual((logger.level, logger.propagate, logger.handlers), before)
        reopened = LogStore(path)
        self.addCleanup(reopened.close)
        self.assertEqual(
            [row["id"] for row in reopened.read(level="DEBUG")["items"]], expected_ids
        )

    def test_redaction_exception_context_and_sink_failure_do_not_escape(self):
        store = LogStore(self.root / "diagnostics.jsonl")
        self.addCleanup(store.close)
        store.attach()
        with log_context(store_id=store.identifier, job_id="job"):
            logger = logging.getLogger("yt_web.test")
            logger.info(
                "token=secret-one https://private.test/file?signature=secret-two"
            )
            logger.warning("Authorization: Bearer secret-three")
            try:
                raise ValueError("private-query-body")
            except ValueError:
                logger.exception("Operation failed")
            before = store.read()["next_cursor"]
            stderr = io.StringIO()
            with (
                patch.object(store.writer, "emit", side_effect=OSError("disk full")),
                contextlib.redirect_stderr(stderr),
            ):
                logger.info("This diagnostic cannot be written")
                logger.info("Neither can this")
            self.assertEqual(stderr.getvalue().count("không ghi được"), 1)
        content = store.path.read_text()
        for secret in (
            "secret-one",
            "secret-two",
            "secret-three",
            "private-query-body",
        ):
            self.assertNotIn(secret, content)
        rows = store.read()["items"]
        self.assertIn("ValueError", rows[-1]["traceback"])
        self.assertEqual(store.read()["next_cursor"], before)

    def test_real_workers_relay_core_logs_without_corrupting_protocol(self):
        store = LogStore(self.root / "workers.jsonl")
        store.attach()
        self.addCleanup(store.close)
        with contextlib.closing(SQLiteFixture(self.config.db)):
            pass
        # Exercise core logging before a deterministic source failure. An invalid
        # channel is rejected before core logs, while orchestration logs belong
        # to the application adapter. Keep the real collector protocol and core;
        # replace only its external downloader boundary inside the child process.
        fixture = self.root / "collector_logging_fixture.py"
        fixture.write_text(
            "from unittest.mock import patch\n"
            "from yt_web.workers.collector import main\n"
            "with patch('transcript_collector.youtube.YouTube._downloader', "
            "side_effect=RuntimeError('offline fixture')):\n"
            "    main()\n",
            encoding="utf-8",
        )
        events = []
        with (
            log_context(
                store_id=store.identifier,
                job_id="job-worker",
                request_id="request-worker",
            ),
            patch.object(WebConfig, "interpreter", return_value=Path(sys.executable)),
        ):
            collector = ProcessWorker(
                self.config,
                "transcript_collector",
                "collector_logging_fixture",
                extra_env={
                    "PYTHONPATH": os.pathsep.join([str(self.root), str(ROOT / "src")])
                },
            )
            try:
                with self.assertRaises(WorkerError) as error:
                    collector.call(
                        "collect",
                        {
                            "target": "https://www.youtube.com/watch?v=abcdefghijk",
                            "mode": "direct",
                            "language": "vi",
                            "db": str(self.config.db),
                        },
                        on_event=events.append,
                    )
                self.assertEqual(error.exception.kind, "SourceError")
            finally:
                collector.close()
            self.assertTrue(events)
            self.assertTrue(all(event["kind"] != "log" for event in events))
            search = ProcessWorker(self.config, "video_search", "yt_web.workers.search")
            try:
                search.call(
                    "configure",
                    {
                        "db": str(self.config.db),
                        "search_db": str(self.config.search_db),
                        "offline": True,
                    },
                )
                self.assertEqual(
                    search.call("search", {"query": "private-query-body"}), []
                )
            finally:
                search.close()
        rows = store.read(level="DEBUG")["items"]
        self.assertTrue(
            any(row["logger"].startswith("transcript_collector") for row in rows)
        )
        self.assertTrue(
            any(
                row["logger"].startswith("yt_web.workers.collector_runtime")
                for row in rows
            )
        )
        self.assertTrue(any(row["logger"].startswith("video_search") for row in rows))
        self.assertTrue(
            all(
                row["job_id"] == "job-worker" and row["request_id"] == "request-worker"
                for row in rows
            )
        )
        self.assertNotIn("private-query-body", store.path.read_text())
        self.assertFalse(
            any("private-query-body" in json.dumps(event) for event in events)
        )

    def test_log_and_analysis_outputs_cannot_alias_application_databases(self):
        with contextlib.closing(SQLiteFixture(self.config.db)):
            pass
        original = self.config.db.read_bytes()
        hardlink = self.root / "log-alias.jsonl"
        hardlink.hardlink_to(self.config.db)
        bad = WebConfig(
            ROOT, self.config.db, self.config.search_db, self.config.state_db, hardlink
        )
        with self.assertRaises(ValueError):
            create_http_app(bad)
        self.assertEqual(self.config.db.read_bytes(), original)
        real_log = self.root / "real-log.jsonl"
        log_link = self.root / "log-link.jsonl"
        log_link.symlink_to(real_log)
        Path(str(real_log) + ".1").hardlink_to(self.config.db)
        with self.assertRaises(ValueError):
            WebConfig(
                ROOT,
                self.config.db,
                self.config.search_db,
                self.config.state_db,
                log_link,
            ).validate()

        app = create_http_app(
            self.config, search_gateway=FakeSearch(), collector_factory=FakeCollector
        )
        with TestClient(app) as client:
            for payload in (
                {"query": "test", "search_db": str(self.config.db)},
                {"query": "test", "search_db": str(self.config.state_db)},
            ):
                response = client.post(
                    "/api/search", json=payload, headers={"X-YT-Request": "1"}
                )
                self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(client.get("/api/collections").json()["items"], [])
        self.assertEqual(self.config.db.read_bytes(), original)

    def test_live_sqlite_sidecars_are_protected_from_logs_reports_and_caches(self):
        with contextlib.closing(SQLiteFixture(self.config.db)):
            pass
        with contextlib.closing(sqlite3.connect(self.config.db)) as source:
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("INSERT INTO channels VALUES('live','Live','url')")
            source.commit()
            wal = Path(str(self.config.db) + "-wal")
            before = wal.read_bytes()
            bad = WebConfig(
                ROOT, self.config.db, self.config.search_db, self.config.state_db, wal
            )
            with self.assertRaises(ValueError):
                create_http_app(bad)
            app = create_http_app(
                self.config,
                search_gateway=FakeSearch(),
                collector_factory=FakeCollector,
            )
            with TestClient(app) as client:
                for suffix in ("-wal", "-shm", "-journal"):
                    target = str(self.config.db) + suffix
                    self.assertEqual(
                        client.post(
                            "/api/search",
                            json={"query": "test", "search_db": target},
                            headers={"X-YT-Request": "1"},
                        ).status_code,
                        400,
                    )
            self.assertEqual(wal.read_bytes(), before)
            self.assertEqual(
                source.execute("SELECT title FROM channels").fetchone(), ("Live",)
            )
            self.assertEqual(source.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_download_is_a_complete_snapshot_during_appends_and_rotation(self):
        app = create_http_app(
            self.config, search_gateway=FakeSearch(), collector_factory=FakeCollector
        )
        stop = threading.Event()
        with TestClient(app) as client:
            store = app.state.logs
            store.writer.maxBytes = 1400

            def publish():
                for number in range(100):
                    store.publish(
                        {
                            "message": f"Background record {number}",
                            "phase": "download_test",
                        }
                    )
                    if stop.wait(0.002):
                        break

            thread = threading.Thread(target=publish)
            thread.start()
            try:
                for _ in range(15):
                    response = client.get("/api/logs/download")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        int(response.headers["content-length"]), len(response.content)
                    )
                    self.assertTrue(response.content.endswith(b"\n"))
                    self.assertTrue(
                        all(
                            json.loads(line)["id"]
                            for line in response.content.splitlines()
                        )
                    )
                    time.sleep(0.001)
            finally:
                stop.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_custom_log_handlers_replace_default_store(self):
        class CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__(logging.INFO)
                self.records: list[logging.LogRecord] = []
                self.closed = False

            def emit(self, record: logging.LogRecord) -> None:
                self.records.append(record)

            def close(self) -> None:
                self.closed = True
                super().close()

        captured = CaptureHandler()
        app = create_http_app(
            self.config,
            search_gateway=FakeSearch(),
            collector_factory=FakeCollector,
            log_handlers=[captured],
            replace_default_logs=True,
        )
        with TestClient(app) as client:
            self.assertIsNone(app.state.logs)
            response = client.post(
                "/api/search",
                json={"query": "private-query-body", "mode": "keyword"},
                headers={"X-YT-Request": "1"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/logs/download").status_code, 404)
            api_logs = client.get("/api/logs", params={"level": "INFO"}).json()
            self.assertTrue(api_logs["items"])
        self.assertFalse(captured.closed)
        messages = "\n".join(record.getMessage() for record in captured.records)
        self.assertIn("Web application started", messages)
        self.assertNotIn("private-query-body", messages)

    def test_sse_log_stream_endpoint_returns_initial_events_and_respects_filters(self):
        app = create_http_app(
            self.config, search_gateway=FakeSearch(), collector_factory=FakeCollector
        )
        with TestClient(app) as client:
            client.post(
                "/api/search",
                json={"query": "private-sse-query", "mode": "keyword"},
                headers={"X-YT-Request": "1"},
            )
            with client.stream(
                "GET", "/api/logs/stream", params={"level": "INFO"}
            ) as stream:
                self.assertEqual(stream.status_code, 200)
                self.assertTrue(
                    stream.headers["content-type"].startswith("text/event-stream")
                )
                events = []
                event_name = "message"
                data_lines = []
                for line in stream.iter_lines():
                    if line == "":
                        if data_lines:
                            events.append(
                                (event_name, json.loads("\n".join(data_lines)))
                            )
                            if len(events) >= 3:
                                app.state.close_streams()
                        event_name, data_lines = "message", []
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
            self.assertEqual(events[0][0], "meta")
            log_events = [payload for name, payload in events if name == "log"]
            self.assertTrue(log_events)
            self.assertNotIn("private-sse-query", json.dumps(log_events))

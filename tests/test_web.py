"""HTTP, durable jobs and worker RPC; no YouTube calls or model downloads."""

import contextlib
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tests.sqlite_fixture import SQLiteFixture
from yt_web.catalog import Library
from yt_web.config import WebConfig
from yt_web.gateways import CollectorProcessGateway, SearchProcessGateway
from yt_web.http import create_http_app
from yt_web.jobs import JobManager, JobStore
from yt_web.processes import ProcessWorker, WorkerCancelled, WorkerError

ROOT = Path(__file__).resolve().parents[1]


def counts(**values):
    result = dict.fromkeys(
        (
            "listed",
            "cached",
            "indexed",
            "unavailable",
            "errors",
            "retained",
            "audio_downloaded",
            "audio_errors",
            "blocked",
            "deferred",
        ),
        0,
    )
    result.update(values)
    return result


class FakeSearch:
    def __init__(self):
        self.model_lock = threading.RLock()
        self.calls = []

    def search_with_options(self, query, options):
        self.calls.append((query, options))
        return [
            {
                "video_id": "v",
                "title": "Video",
                "text": "Caption",
                "language": "vi",
                "transcript_source": "youtube",
                "source_mode": "manual",
            }
        ]

    def close(self):
        pass


class FakeCollector:
    def __init__(self, *, wait=False):
        self.cancelled = threading.Event()
        self.wait = wait

    def collect(self, request, on_event=None):
        if on_event is None:
            raise ValueError("Test collector requires an event handler.")
        on_event(
            {
                "kind": "video_started",
                "video_id": "v",
                "title": "Video",
                "position": 1,
                "total": 1,
                "cached": False,
            }
        )
        if self.wait:
            self.cancelled.wait(5)
        if self.cancelled.is_set():
            raise WorkerCancelled("cancelled")
        result = counts(listed=1, indexed=1)
        on_event(
            {
                "kind": "video_finished",
                "video_id": "v",
                "title": "Video",
                "position": 1,
                "total": 1,
                "counts": result,
            }
        )
        on_event({"kind": "summary", "counts": result})
        return {"scope": "single video", "counts": result}

    def cancel(self):
        self.cancelled.set()

    def force_stop(self):
        self.cancel()


class WebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = WebConfig(
            ROOT,
            self.root / "transcripts.sqlite3",
            self.root / "search.sqlite3",
            self.root / "web.sqlite3",
        )

    def populate(self):
        with contextlib.closing(SQLiteFixture(self.config.db)) as fixture:
            fixture.db.executescript("""
                INSERT INTO channels VALUES('c','Channel','url');
                INSERT INTO videos VALUES('v','c','Video','');
                INSERT INTO videos VALUES('other','c','Other','');
                INSERT INTO transcripts VALUES('v','vi','youtube','manual','','2026-11-13T00:00:00Z');
                INSERT INTO transcript_parts VALUES('v','vi','youtube','manual',0,0,2,'Caption');
            """)

    def client(self, *, wait=False):
        self.search = FakeSearch()
        self.collector = FakeCollector(wait=wait)
        return TestClient(
            create_http_app(
                self.config,
                search_gateway=self.search,
                collector_factory=lambda: self.collector,
            )
        )

    def wait_for(self, client, identifier, statuses):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/api/collections/{identifier}").json()
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail(f"Job did not reach {statuses}: {job}")

    def test_library_is_read_only_missing_database_is_not_created_and_pages_are_filtered(
        self,
    ):
        library = Library(self.config.db)
        self.assertFalse(library.summary()["has_database"])
        self.assertFalse(self.config.db.exists())
        self.populate()
        before = self.config.db.read_bytes()
        summary = library.summary()
        self.assertEqual(summary["stats"]["videos"], 2)
        self.assertEqual(summary["stats"]["segments"], 1)
        self.assertEqual(summary["languages"], ["vi"])
        self.assertEqual(
            library.videos(query="Video", channel="c")["items"][0]["id"], "v"
        )
        self.assertEqual(library.videos(query="' OR 1=1 --")["total"], 0)
        self.assertEqual(len(library.videos(limit=1)["items"]), 1)
        self.assertEqual(self.config.db.read_bytes(), before)

    def test_http_shell_search_and_library_preserve_input_database(self):
        self.populate()
        before = hashlib.sha256(self.config.db.read_bytes()).digest()
        with self.client() as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/static/app.js").status_code, 200)
            self.assertIn("/api/search", client.get("/openapi.json").json()["paths"])
            self.assertEqual(client.get("/docs").status_code, 404)
            self.assertEqual(client.get("/api/health").json()["status"], "ok")
            self.assertEqual(client.get("/api/library").json()["stats"]["videos"], 2)
            response = client.post(
                "/api/search", json={"query": "caption"}, headers={"X-YT-Request": "1"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["results"][0]["source_mode"], "manual")
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(hashlib.sha256(self.config.db.read_bytes()).digest(), before)

    def test_collect_http_progress_completion_and_history_survive_restart(self):
        headers = {"X-YT-Request": "1"}
        with self.client() as client:
            response = client.post(
                "/api/collections",
                json={
                    "target": "https://youtu.be/AbCdEf12_-3",
                    "mode": "direct",
                    "language": "vi",
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            identifier = response.json()["id"]
            job = self.wait_for(client, identifier, {"completed"})
            self.assertEqual(job["result"]["counts"]["indexed"], 1)
            events = client.get(f"/api/collections/{identifier}/events").json()
            self.assertEqual(len(events["items"]), 3)
            self.assertEqual(
                client.get(
                    f"/api/collections/{identifier}/events?after={events['next_cursor']}"
                ).json()["items"],
                [],
            )
            self.assertEqual(
                client.get("/api/collections").json()["items"][0]["id"], identifier
            )
        with self.client() as client:
            self.assertEqual(
                client.get(f"/api/collections/{identifier}").json()["status"],
                "completed",
            )

    def test_cancel_active_collection_is_cooperative(self):
        headers = {"X-YT-Request": "1"}
        with self.client(wait=True) as client:
            identifier = client.post(
                "/api/collections", json={"target": "URL"}, headers=headers
            ).json()["id"]
            self.wait_for(client, identifier, {"running"})
            response = client.post(
                f"/api/collections/{identifier}/cancel", headers=headers
            )
            self.assertEqual(response.status_code, 200)
            self.wait_for(client, identifier, {"cancelled"})

    def test_search_modes_and_rerank_options(self):
        headers = {"X-YT-Request": "1"}
        with self.client() as client:
            for mode in ("hybrid", "semantic", "keyword", "exact"):
                response = client.post(
                    "/api/search",
                    json={"query": "caption", "mode": mode},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["mode"], mode)
                self.assertEqual(response.json()["rerank"], mode == "hybrid")
                self.assertEqual(self.search.calls[-1][1]["mode"], mode)
            no_rerank = client.post(
                "/api/search",
                json={"query": "caption", "mode": "hybrid", "rerank": False},
                headers=headers,
            )
            self.assertEqual(no_rerank.status_code, 200, no_rerank.text)
            self.assertFalse(no_rerank.json()["rerank"])
            self.assertFalse(self.search.calls[-1][1]["rerank"])
            for options in (
                {"mode": "unknown"},
                {"mode": "fuzzy"},
                {"mode": "semantic", "rerank": True},
                {"mode": "keyword", "rerank": True},
                {"mode": "exact", "rerank": True},
                {"rerank_top": 40},
                {"mode": "keyword", "min_similarity": 0.2},
                {"mode": "exact", "min_similarity": 0.2},
            ):
                response = client.post(
                    "/api/search", json={"query": "caption", **options}, headers=headers
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_web_supports_cli_search_and_collection_options(self):
        headers = {"X-YT-Request": "1"}
        with self.client() as client:
            options = {
                "top": 80,
                "model": "e5-small",
                "language": "en",
                "channel": "c",
                "transcript_source": "generate",
                "source_mode": "whisper",
                "mode": "semantic",
                "min_similarity": 0.5,
                "device": "cpu",
                "batch_size": 2,
                "offline": True,
            }
            response = client.post(
                "/api/search", json={"query": "caption", **options}, headers=headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            query, actual = self.search.calls[0]
            self.assertEqual(query, "caption")
            for key, value in options.items():
                self.assertEqual(actual[key], value)
            self.assertEqual(actual["db"], str(self.config.db))
            request = {
                "target": "URL",
                "mode": "generate",
                "language": None,
                "refresh": True,
                "engine": "whisper",
                "engine_model": "small",
                "device": "cpu",
                "cookies": str(self.root / "cookies.txt"),
            }
            response = client.post("/api/collections", json=request, headers=headers)
            self.assertEqual(response.status_code, 202, response.text)
            job = self.wait_for(client, response.json()["id"], {"completed"})
            self.assertEqual(job["request"], request)
            self.assertEqual(
                client.post(
                    "/api/search",
                    json={"query": "caption", "model": "invalid"},
                    headers=headers,
                ).status_code,
                422,
            )
            self.assertEqual(
                client.post(
                    "/api/collections",
                    json={"target": "URL", "device": "invalid"},
                    headers=headers,
                ).status_code,
                422,
            )

    def test_where_http_options_omit_legacy_defaults_and_reject_explicit_scope_keys(
        self,
    ):
        self.populate()
        before = self.config.db.read_bytes()
        headers = {"X-YT-Request": "1"}
        where = "language='en' OR source_mode='custom-engine'"
        with self.client() as client:
            response = client.post(
                "/api/search",
                json={
                    "query": "caption",
                    "where": f"  {where}  ",
                    "mode": "exact",
                    "top": 2,
                    "moments_per_video": 1,
                    "offline": True,
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["where"], where)
            options = self.search.calls[-1][1]
            self.assertEqual(options["where"], where)
            self.assertEqual(options["mode"], "exact")
            self.assertEqual(options["top"], 2)
            self.assertEqual(options["moments_per_video"], 1)
            for key in (
                "language",
                "transcript_source",
                "channel",
                "source_mode",
                "video_ids",
            ):
                self.assertNotIn(key, options)
            for key, value in (
                ("language", "vi"),
                ("transcript_source", "auto"),
                ("channel", None),
                ("source_mode", None),
                ("video_ids", None),
            ):
                with self.subTest(key=key):
                    rejected = client.post(
                        "/api/search",
                        json={"query": "caption", "where": where, key: value},
                        headers=headers,
                    )
                    self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(len(self.search.calls), 1)
            for invalid in ("", "   ", 123, ["language='en'"]):
                with self.subTest(where=invalid):
                    rejected = client.post(
                        "/api/search",
                        json={"query": "caption", "where": invalid},
                        headers=headers,
                    )
                    self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(len(self.search.calls), 1)
            legacy = client.post(
                "/api/search",
                json={"query": "caption", "where": None, "language": "vi"},
                headers=headers,
            )
            self.assertEqual(legacy.status_code, 200, legacy.text)
            self.assertNotIn("where", self.search.calls[-1][1])
            self.assertEqual(self.search.calls[-1][1]["language"], "vi")
        self.assertEqual(self.config.db.read_bytes(), before)

    def test_invalid_requests_cross_origin_posts_and_hosts_are_rejected(self):
        with self.client() as client:
            self.assertEqual(
                client.post("/api/collections", json={"target": "URL"}).status_code, 403
            )
            self.assertEqual(
                client.post(
                    "/api/collections",
                    json={"target": "URL"},
                    headers={"X-YT-Request": "1", "Origin": "https://evil.example"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.get(
                    "/api/library", headers={"Host": "evil.example"}
                ).status_code,
                400,
            )
            for payload in (
                {"target": ""},
                {"target": "URL", "mode": "generate", "language": "vi"},
                {"target": "URL", "engine_model": "small"},
            ):
                self.assertEqual(
                    client.post(
                        "/api/collections", json=payload, headers={"X-YT-Request": "1"}
                    ).status_code,
                    422,
                )
            self.assertEqual(
                client.post(
                    "/api/search", json={"query": " "}, headers={"X-YT-Request": "1"}
                ).status_code,
                422,
            )
            self.assertEqual(client.get("/api/collections/missing").status_code, 404)
            self.assertEqual(client.get("/api/collections").json()["items"], [])

    def test_database_aliases_are_rejected_before_web_schema_writes(self):
        self.populate()
        before = self.config.db.read_bytes()
        hardlink = self.root / "hardlink.sqlite3"
        hardlink.hardlink_to(self.config.db)
        for destination in (self.config.db, hardlink):
            configuration = WebConfig(
                ROOT, self.config.db, self.config.search_db, destination
            )
            with self.assertRaisesRegex(ValueError, "different"):
                create_http_app(configuration)
        self.assertEqual(self.config.db.read_bytes(), before)

    def test_state_recovery_marks_running_jobs_interrupted_without_erasing_completed_work(
        self,
    ):
        store = JobStore(self.config.state_db)
        first = store.create({"target": "first", "mode": "direct"})
        self.assertEqual(store.next(), first["id"])
        second = store.create({"target": "second", "mode": "direct"})
        store.recover()
        self.assertEqual(store.get(first["id"])["status"], "interrupted")
        self.assertEqual(store.get(second["id"])["status"], "queued")

    def test_incompatible_state_columns_are_rejected_without_mutation(self):
        import sqlite3

        with contextlib.closing(sqlite3.connect(self.config.state_db)) as db:
            db.executescript(
                "CREATE TABLE jobs(id TEXT); CREATE TABLE events(id INTEGER); PRAGMA user_version=1;"
            )
        before = self.config.state_db.read_bytes()
        with self.assertRaisesRegex(ValueError, "không tương thích"):
            JobStore(self.config.state_db)
        self.assertEqual(self.config.state_db.read_bytes(), before)
        self.assertFalse(Path(str(self.config.state_db) + "-wal").exists())

    def test_queue_survives_factory_failure_and_runs_next_job(self):
        calls = []

        def factory():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("factory unavailable")
            return FakeCollector()

        manager = JobManager(self.config, FakeSearch(), collector_factory=factory)
        first = manager.submit({"target": "first", "mode": "direct"})
        second = manager.submit({"target": "second", "mode": "direct"})
        manager.start()
        try:
            deadline = time.monotonic() + 5
            while manager.store.get(second["id"])["status"] != "completed":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            self.assertEqual(manager.store.get(first["id"])["status"], "failed")
            assert manager._thread is not None
            self.assertTrue(manager._thread.is_alive())
        finally:
            manager.close()

    def test_second_queue_owner_cannot_interrupt_running_job(self):
        first = JobManager(
            self.config,
            FakeSearch(),
            collector_factory=lambda: FakeCollector(wait=True),
        )
        job = first.submit({"target": "first", "mode": "direct"})
        first.start()
        second = JobManager(self.config, FakeSearch())
        try:
            deadline = time.monotonic() + 5
            while first.store.get(job["id"])["status"] != "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            with self.assertRaisesRegex(ValueError, "khác đang sử dụng"):
                second.start()
            self.assertEqual(first.store.get(job["id"])["status"], "running")
        finally:
            first.close()
        second.start()
        second.close()

    def test_generate_cancel_does_not_wait_for_active_search(self):
        search = FakeSearch()
        collectors = []

        def factory():
            gateway = FakeCollector()
            collectors.append(gateway)
            return gateway

        manager = JobManager(self.config, search, collector_factory=factory)
        with search.model_lock:
            job = manager.submit({"target": "first", "mode": "generate"})
            manager.start()
            try:
                deadline = time.monotonic() + 5
                while manager.store.get(job["id"])["status"] != "running":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                manager.cancel(job["id"])
                deadline = time.monotonic() + 2
                while manager.store.get(job["id"])["status"] != "cancelled":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                self.assertEqual(manager.store.events(job["id"])["items"], [])
            finally:
                manager.close()

    def test_search_retries_with_fresh_worker_after_transport_failure(self):
        failed, replacement = Mock(), Mock()
        failed.call.side_effect = WorkerError("transport closed")
        replacement.call.side_effect = [None, []]
        with patch("yt_web.gateways.ProcessWorker", side_effect=[failed, replacement]):
            gateway = SearchProcessGateway(self.config)
            with self.assertRaisesRegex(WorkerError, "transport closed"):
                gateway.search("first")
            self.assertEqual(gateway.search("retry"), [])
        failed.close.assert_called_once()
        self.assertEqual(replacement.call.call_args_list[0].args[0], "configure")
        self.assertEqual(replacement.call.call_args_list[1].args[0], "search")

    def test_cancel_before_worker_spawn_never_starts_process(self):
        worker = ProcessWorker(
            self.config, "transcript_collector", "yt_web.workers.collector"
        )
        worker.interrupt()
        with patch("yt_web.processes.subprocess.Popen") as spawn:
            with self.assertRaises(WorkerCancelled):
                worker.call("collect", {})
            spawn.assert_not_called()

    def test_real_search_worker_protocol_handles_empty_corpus_and_keeps_session_after_bad_query(
        self,
    ):
        with contextlib.closing(SQLiteFixture(self.config.db)):
            pass
        with patch.object(WebConfig, "interpreter", return_value=Path(sys.executable)):
            worker = ProcessWorker(self.config, "video_search", "yt_web.workers.search")
            try:
                worker.call(
                    "configure",
                    {
                        "db": str(self.config.db),
                        "search_db": str(self.config.search_db),
                        "offline": True,
                    },
                )
                self.assertEqual(worker.call("search", {"query": "first"}), [])
                with self.assertRaises(WorkerError) as error:
                    worker.call("search", {"query": ""})
                self.assertEqual(error.exception.kind, "ValueError")
                self.assertEqual(worker.call("search", {"query": "retry"}), [])
            finally:
                worker.close()

    def test_real_collector_worker_rejects_invalid_input_without_network_or_database_creation(
        self,
    ):
        with patch.object(WebConfig, "interpreter", return_value=Path(sys.executable)):
            worker = ProcessWorker(
                self.config, "transcript_collector", "yt_web.workers.collector"
            )
            try:
                with self.assertRaises(WorkerError) as error:
                    worker.call(
                        "collect",
                        {
                            "target": "",
                            "mode": "direct",
                            "language": "vi",
                            "db": str(self.config.db),
                        },
                    )
                self.assertEqual(error.exception.kind, "ValueError")
            finally:
                worker.close()
        self.assertFalse(self.config.db.exists())

    @unittest.skipUnless(os.name == "posix", "Process group shutdown test")
    def test_forced_generate_shutdown_releases_rpc_and_removes_parent_temp_audio(self):
        script = self.root / "stuck_worker.py"
        script.write_text("""import json,os,signal,subprocess,sys,time
sys.stdin.readline()
subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])
signal.signal(signal.SIGINT,signal.SIG_IGN)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
with open(os.path.join(os.environ['TMPDIR'],'audio.wav'),'w') as audio:
    audio.write('temporary audio')
print(json.dumps({'type':'event','payload':{'directory':os.environ['TMPDIR']}}),flush=True)
while True:
    time.sleep(60)
""")
        ready, stopped = threading.Event(), threading.Event()
        directories = []

        def factory(config, component, module, *, extra_env=None):
            return ProcessWorker(
                config,
                component,
                "stuck_worker",
                extra_env={**(extra_env or {}), "PYTHONPATH": str(self.root)},
            )

        def event(payload):
            directories.append(Path(payload["directory"]))
            ready.set()

        gateway = CollectorProcessGateway(self.config)

        def collect():
            try:
                gateway.collect({"target": "fake", "mode": "generate"}, on_event=event)
            except WorkerError:
                stopped.set()

        with (
            patch.object(WebConfig, "interpreter", return_value=Path(sys.executable)),
            patch("yt_web.gateways.ProcessWorker", side_effect=factory),
        ):
            thread = threading.Thread(target=collect)
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertTrue((directories[0] / "audio.wav").is_file())
                with patch.object(
                    CollectorProcessGateway, "CANCEL_GRACE_SECONDS", 0.05
                ):
                    gateway.cancel()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(stopped.is_set())
                self.assertFalse(directories[0].exists())
            finally:
                gateway.force_stop()
                thread.join(5)

    @unittest.skipUnless(os.name == "posix", "SIGINT process test")
    def test_worker_interrupt_can_run_while_rpc_waits_without_deadlocking(self):
        script = self.root / "fake_worker.py"
        script.write_text("""import json,sys,time
sys.stdin.readline()
print(json.dumps({'type':'event','payload':{'ready':True}}),flush=True)
try:
    time.sleep(20)
except KeyboardInterrupt:
    print(json.dumps({'type':'cancelled'}),flush=True)
""")
        ready, cancelled = threading.Event(), threading.Event()
        with patch.object(WebConfig, "interpreter", return_value=Path(sys.executable)):
            worker = ProcessWorker(
                self.config,
                "transcript_collector",
                "fake_worker",
                extra_env={"PYTHONPATH": str(self.root)},
            )

            def call():
                try:
                    worker.call("collect", {}, on_event=lambda event: ready.set())
                except WorkerCancelled:
                    cancelled.set()

            thread = threading.Thread(target=call)
            thread.start()
            self.assertTrue(ready.wait(5))
            worker.interrupt()
            thread.join(5)
            worker.close()
            self.assertFalse(thread.is_alive())
            self.assertTrue(cancelled.is_set())

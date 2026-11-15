"""Live HTTP, installed entry points and real workers over an offline v2 corpus."""

import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from tests.sqlite_fixture import SQLiteFixture

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".venv" / "bin"


class LiveWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yt-web-e2e-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.db = self.directory / "transcripts.sqlite3"
        self.cache = self.directory / "search.sqlite3"
        self.state = self.directory / "state.sqlite3"
        self.log = self.directory / "web.jsonl"
        with contextlib.closing(SQLiteFixture(self.db)) as fixture, fixture.db:
            fixture.db.executescript("""
                INSERT INTO channels VALUES('c', 'Kênh kiểm tra', 'https://youtube.com/channel/c');
                INSERT INTO videos VALUES('AbCdEf12_-3', 'c', 'Vai trò insulin', '');
                INSERT INTO videos VALUES('ZyXwVu98_-1', 'c', 'Ngôn ngữ lập trình', '');
                INSERT INTO transcripts VALUES('AbCdEf12_-3', 'vi', 'youtube', 'manual', '', '2026-11-15');
                INSERT INTO transcripts VALUES('ZyXwVu98_-1', 'vi', 'youtube', 'manual', '', '2026-11-15');
                INSERT INTO transcript_parts VALUES('AbCdEf12_-3', 'vi', 'youtube', 'manual', 0, 30, 10, 'Insulin giúp điều hòa đường huyết.');
                INSERT INTO transcript_parts VALUES('ZyXwVu98_-1', 'vi', 'youtube', 'manual', 0, 90, 10, 'C++ là một ngôn ngữ lập trình.');
            """)
        self.original = self.db.read_bytes()
        self.addCleanup(self.assert_input_preserved)
        self.environment = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        self.client = httpx.Client(trust_env=False, timeout=10)
        self.addCleanup(self.client.close)
        self.process = None
        self.addCleanup(self.stop_server)
        self.start_server()

    def assert_input_preserved(self):
        self.assertEqual(self.db.read_bytes(), self.original)

    def start_server(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.output = (self.directory / "server.log").open("ab")
        self.addCleanup(self.output.close)
        self.process = subprocess.Popen(
            [
                str(BIN / "yt-web"),
                "--root",
                str(ROOT),
                "--port",
                str(port),
                "--db",
                str(self.db),
                "--search-db",
                str(self.cache),
                "--state-db",
                str(self.state),
                "--log-file",
                str(self.log),
                "--log-level",
                "ERROR",
            ],
            cwd=self.directory,
            env=self.environment,
            stdout=self.output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail((self.directory / "server.log").read_text())
            try:
                response = self.get("/api/health")
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        self.fail("Live web server did not become healthy within 15 seconds.")

    def stop_server(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            self.fail("Server did not shut down gracefully.")
        # Uvicorn re-raises SIGTERM after completing its lifespan shutdown.
        self.assertIn(self.process.returncode, (0, -signal.SIGTERM))
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        workers = {
            row["worker_pid"] for row in records if row.get("phase") == "worker_started"
        }
        closed = {
            row["worker_pid"] for row in records if row.get("phase") == "worker_closed"
        }
        self.assertLessEqual(workers, closed)
        for pid in workers:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def get(self, path):
        return self.client.get(self.url + path)

    def post(self, path, payload, *, headers=None):
        return self.client.post(
            self.url + path,
            json=payload,
            headers={"X-YT-Request": "1", **(headers or {})},
        )

    def search(self, query="insulin", **options):
        return self.post(
            "/api/search",
            {"query": query, "mode": "keyword", "offline": True, **options},
        )

    def add_where_versions(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("""
                UPDATE videos SET description='O''Reilly' WHERE id='ZyXwVu98_-1';
                INSERT INTO transcripts VALUES('AbCdEf12_-3','en','youtube','manual','','2026-11-15');
                INSERT INTO transcripts VALUES('ZyXwVu98_-1','fr','generate','custom-engine','custom','2026-11-15');
                INSERT INTO transcript_parts VALUES('AbCdEf12_-3','en','youtube','manual',0,20,10,'Insulin in English.');
                INSERT INTO transcript_parts VALUES('ZyXwVu98_-1','fr','generate','custom-engine',0,80,10,'Insulin custom engine.');
                INSERT INTO transcript_parts VALUES('ZyXwVu98_-1','fr','generate','custom-engine',1,220,10,'Insulin later custom segment.');
            """)
        self.original = self.db.read_bytes()

    def test_installed_server_serves_ui_assets_and_read_only_library(self):
        self.assertEqual(
            self.get("/api/health").json(),
            {"status": "ok", "components": {"collector": True, "search": True}},
        )
        page = self.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="search-form"', page.text)
        for asset in ("app.js", "app.css", "favicon.svg"):
            response = self.get("/static/" + asset)
            self.assertEqual(response.status_code, 200, asset)
            self.assertTrue(response.content, asset)
        summary = self.get("/api/library").json()
        self.assertTrue(summary["has_database"])
        self.assertEqual(summary["stats"]["videos"], 2)
        videos = self.get("/api/videos?q=insulin").json()
        self.assertEqual([row["id"] for row in videos["items"]], ["AbCdEf12_-3"])

    def test_real_search_worker_matches_cli_results_and_handles_filters(self):
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([row["video_id"] for row in body["results"]], ["AbCdEf12_-3"])
        moment = body["results"][0]["moments"][0]
        self.assertIn("Insulin", moment["text"])
        self.assertIn("watch?v=AbCdEf12_-3", moment["url"])
        cli = subprocess.run(
            [
                str(BIN / "yt"),
                "search",
                "insulin",
                "--mode",
                "keyword",
                "--offline",
                "--json",
                "--db",
                str(self.db),
                "--search-db",
                str(self.directory / "cli-search.sqlite3"),
                "--log-file",
                str(self.directory / "cli.jsonl"),
            ],
            cwd=self.directory,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["results"], body["results"])
        for options in (
            {"video_ids": []},
            {"video_ids": ["ZyXwVu98_-1"]},
            {"language": "en"},
        ):
            filtered = self.search(**options)
            self.assertEqual(filtered.status_code, 200, filtered.text)
            self.assertEqual(filtered.json()["results"], [])
        exact = self.search("C++", mode="exact")
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(
            [row["video_id"] for row in exact.json()["results"]], ["ZyXwVu98_-1"]
        )
        self.assertEqual(self.search("absentword").json()["results"], [])

    def test_bad_requests_and_worker_errors_allow_subsequent_search(self):
        self.assertEqual(
            self.client.post(
                self.url + "/api/search", json={"query": "insulin"}
            ).status_code,
            403,
        )
        self.assertEqual(self.search(query="   ").status_code, 422)
        response = self.post(
            "/api/search",
            {"query": "insulin", "mode": "keyword"},
            headers={"Origin": "https://untrusted.invalid"},
        )
        self.assertEqual(response.status_code, 403)
        missing = self.search(db=str(self.directory / "missing.sqlite3"))
        self.assertEqual(missing.status_code, 503, missing.text)
        self.assertFalse((self.directory / "missing.sqlite3").exists())
        alias = self.directory / "alias.sqlite3"
        alias.symlink_to(self.db)
        self.assertEqual(self.search(search_db=str(alias)).status_code, 400)
        recovered = self.search()
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(len(recovered.json()["results"]), 1)

    def test_where_search_matches_cli_across_languages_and_custom_engines(self):
        self.add_where_versions()
        where = "channel='Kênh kiểm tra' AND (language='en' OR (description='O''Reilly' AND source_mode IN ('custom-engine')))"
        response = self.search(where=where)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["where"], where)
        results = {item["video_id"]: item for item in payload["results"]}
        self.assertEqual(set(results), {"AbCdEf12_-3", "ZyXwVu98_-1"})
        self.assertEqual(results["AbCdEf12_-3"]["language"], "en")
        generated = results["ZyXwVu98_-1"]
        self.assertEqual(generated["language"], "fr")
        self.assertEqual(generated["source_mode"], "custom-engine")
        self.assertEqual(
            {moment["start"] for moment in generated["moments"]}, {80, 220}
        )
        cli = subprocess.run(
            [
                str(BIN / "yt"),
                "search",
                "insulin",
                "--where",
                where,
                "--mode",
                "keyword",
                "--offline",
                "--json",
                "--db",
                str(self.db),
                "--search-db",
                str(self.directory / "where-cli.sqlite3"),
                "--log-file",
                str(self.directory / "where-cli.jsonl"),
            ],
            cwd=self.directory,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["results"], payload["results"])
        exact = self.search("Insulin", mode="exact", where=where)
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(
            {item["video_id"] for item in exact.json()["results"]}, set(results)
        )
        # Switching back to the legacy scope must restore its default vi policy.
        legacy = self.search()
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(
            [item["video_id"] for item in legacy.json()["results"]], ["AbCdEf12_-3"]
        )
        self.assertEqual(legacy.json()["results"][0]["language"], "vi")

    def test_invalid_where_worker_requests_recover_and_empty_scope_does_not_expand(
        self,
    ):
        self.assertEqual(self.search(where="").status_code, 422)
        for where in (
            "language =",
            "missing_column = 1",
            "SELECT * FROM videos",
            "1=1; DELETE FROM videos",
        ):
            with self.subTest(where=where):
                response = self.search(where=where)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.db.read_bytes(), self.original)
                recovered = self.search(
                    where="video_id='AbCdEf12_-3' AND language='vi'"
                )
                self.assertEqual(recovered.status_code, 200, recovered.text)
                self.assertEqual(len(recovered.json()["results"]), 1)
        empty = self.search(where="video_id='unknown'")
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["results"], [])

    def test_failed_collection_history_logs_and_search_survive_server_restart(self):
        response = self.post(
            "/api/collections",
            {"target": "not-a-youtube-target", "mode": "direct", "language": "vi"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.get(f"/api/collections/{identifier}").json()
            if job["status"] == "failed":
                break
            self.assertIn(job["status"], {"queued", "running"}, job)
            time.sleep(0.05)
        else:
            self.fail(f"Collection did not fail as expected: {job}")
        self.assertTrue(job["error"])
        self.assertTrue(
            self.get(f"/api/collections/{identifier}/events").json()["items"]
        )
        self.assertEqual(self.search().status_code, 200)
        logs = self.get("/api/logs?level=DEBUG").json()["items"]
        self.assertTrue(any(row.get("worker_pid") for row in logs))
        self.stop_server()
        self.start_server()
        persisted = self.get(f"/api/collections/{identifier}").json()
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["error"], job["error"])
        self.assertEqual(self.search().status_code, 200)
        downloaded = self.get("/api/logs/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertTrue(downloaded.content)

"""Web parity integration with controlled gateways; never load models or YouTube."""

import logging
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from yt_web.config import WebConfig
from yt_web.http import create_http_app

from tests.test_web import FakeCollector, FakeSearch

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("yt_web.parity_fixture")


class LoggedCollector(FakeCollector):
    def __init__(self, *, wait=False, journal=None):
        super().__init__(wait=wait)
        self.calls = []
        self.journal = journal

    def collect(self, request, on_event=None):
        self.calls.append(request)
        if self.journal:
            self.journal("collect", request)
        logger.info(
            "Video fixture inventory committed",
            extra={"phase": "inventory", "video_id": "v"},
        )
        logger.debug("Fixture transcript cache miss", extra={"phase": "cache"})
        logger.info(
            "Literal <img src=x onerror=window.logXss=true> token=secret-test https://private.test/signed?key=secret"
        )
        result = super().collect(request, on_event)
        logger.info(
            "Video fixture transcript committed",
            extra={"phase": "commit", "video_id": "v"},
        )
        return result


class LoggedSearch(FakeSearch):
    def __init__(self, *, journal=None):
        super().__init__()
        self.journal = journal

    def search_with_options(self, query, options):
        if self.journal:
            self.journal("search", {"query": query, **options})
        logger.info("Search fixture started", extra={"phase": "search"})
        logger.debug(
            "Embedding fixture batch completed",
            extra={"phase": "embedding", "completed": 2, "total": 2},
        )
        time.sleep(0.15)
        result = super().search_with_options(query, options)
        logger.info(
            "Search fixture results ready",
            extra={"phase": "ranking", "count": len(result)},
        )
        return result


class WebParityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = WebConfig(
            ROOT,
            self.root / "transcripts.sqlite3",
            self.root / "search.sqlite3",
            self.root / "state.sqlite3",
        )
        self.search = LoggedSearch()
        self.collectors = []

    def client(self, *, wait=False):
        def collector():
            gateway = LoggedCollector(wait=wait)
            self.collectors.append(gateway)
            return gateway

        return TestClient(
            create_http_app(
                self.config,
                search_gateway=self.search,
                collector_factory=collector,
            )
        )

    def wait_for(self, client, route, identifier, statuses={"completed"}):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/api/{route}/{identifier}").json()
            if job.get("status") in statuses:
                return job
            time.sleep(0.01)
        self.fail(f"Job failed to reach {statuses}: {job}")

    def test_advanced_collect_and_search_fields_reach_gateway(self):
        headers = {"X-YT-Request": "1"}
        collection = {
            "target": "https://youtu.be/AbCdEf12_-3",
            "mode": "generate",
            "language": None,
            "engine": "whisper",
            "engine_model": "tiny",
            "device": "cpu",
            "cookies": str(self.root / "cookies.txt"),
            "db": str(self.root / "alternate.sqlite3"),
            "refresh": True,
        }
        search = {
            "query": "Vietnamese fixture",
            "top": 7,
            "language": "en-US",
            "channel": "UCtest",
            "model": "e5-small",
            "transcript_source": "generate",
            "source_mode": "whisper",
            "device": "cpu",
            "batch_size": 3,
            "mode": "semantic",
            "min_similarity": 0.2,
            "offline": True,
            "db": str(self.root / "alternate.sqlite3"),
            "search_db": str(self.root / "alternate-search.sqlite3"),
        }
        with self.client() as client:
            response = client.post("/api/collections", json=collection, headers=headers)
            self.assertEqual(response.status_code, 202, response.text)
            self.wait_for(client, "collections", response.json()["id"])
            for field, value in collection.items():
                self.assertEqual(self.collectors[-1].calls[-1][field], value)
            response = client.post("/api/search", json=search, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            query, options = self.search.calls[-1]
            self.assertEqual(query, search["query"])
            for field, value in search.items():
                if field != "query":
                    self.assertEqual(options[field], value)

    def test_analyses_endpoints_are_removed(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/analyses").status_code, 404)
            self.assertEqual(
                client.post(
                    "/api/analyses",
                    json={"kind": "eval"},
                    headers={"X-YT-Request": "1"},
                ).status_code,
                404,
            )

    def test_queue_cancel_and_diagnostic_context_are_independent(self):
        headers = {"X-YT-Request": "1"}
        identifiers = []
        with self.client(wait=True) as client:
            for target in ("@first", "@queued"):
                response = client.post(
                    "/api/collections",
                    json={"target": target, "mode": "direct", "language": "vi"},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 202, response.text)
                identifier = response.json()["id"]
                identifiers.append(identifier)
                if target == "@first":
                    self.wait_for(client, "collections", identifier, {"running"})
                else:
                    self.assertEqual(
                        client.get(f"/api/collections/{identifier}").json()["status"],
                        "queued",
                    )
            self.assertEqual(
                client.post(
                    f"/api/collections/{identifiers[1]}/cancel", headers=headers
                ).json()["status"],
                "cancelled",
            )
            self.assertEqual(
                client.get(f"/api/collections/{identifiers[0]}").json()["status"],
                "running",
            )
            client.post(f"/api/collections/{identifiers[0]}/cancel", headers=headers)
            self.wait_for(client, "collections", identifiers[0], {"cancelled"})
            logs = client.get("/api/logs?level=DEBUG").json()
            self.assertTrue(any(item["level"] == "DEBUG" for item in logs["items"]))
            scoped = client.get(
                f"/api/logs?level=DEBUG&job_id={identifiers[0]}"
            ).json()["items"]
            self.assertTrue(scoped)
            self.assertTrue(
                all(item.get("job_id") == identifiers[0] for item in scoped)
            )
            info = client.get("/api/logs?level=INFO").json()["items"]
            self.assertFalse(any(item["level"] == "DEBUG" for item in info))
            downloaded = client.get("/api/logs/download")
            self.assertEqual(downloaded.status_code, 200)
            self.assertIn("Literal <img", downloaded.text)
            self.assertNotIn("secret-test", downloaded.text)
            self.assertNotIn("private.test", downloaded.text)
        with self.client() as client:
            for identifier in identifiers:
                self.assertEqual(
                    client.get(f"/api/collections/{identifier}").json()["status"],
                    "cancelled",
                )
            self.assertTrue(
                client.get(f"/api/logs?level=DEBUG&job_id={identifiers[0]}").json()[
                    "items"
                ]
            )

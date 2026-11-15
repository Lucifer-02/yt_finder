"""Installed CLI processes against real SQLite, without YouTube or model downloads."""

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.sqlite_fixture import SQLiteFixture

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".venv" / "bin"


class CLISubprocessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yt-cli-e2e-")
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)
        self.source = self.cwd / "transcripts.sqlite3"
        self.cache = self.cwd / "search.sqlite3"
        self.log = self.cwd / "cli.jsonl"
        with contextlib.closing(SQLiteFixture(self.source)) as fixture:
            fixture.db.executescript("""
                INSERT INTO channels VALUES('health','Sức khỏe','https://www.youtube.com/@health');
                INSERT INTO channels VALUES('other','Kênh khác','https://www.youtube.com/@other');
                INSERT INTO videos VALUES('v-health','health','Chuyển hóa','');
                INSERT INTO videos VALUES('v-code','health','Lập trình','O''Reilly');
                INSERT INTO videos VALUES('v-other','other','Dinh dưỡng','');
                INSERT INTO videos VALUES('v-english','health','English','');
                INSERT INTO transcripts VALUES('v-health','vi','youtube','manual','','2026-01-01');
                INSERT INTO transcripts VALUES('v-code','vi','youtube','manual','','2026-01-01');
                INSERT INTO transcripts VALUES('v-other','vi','youtube','manual','','2026-01-01');
                INSERT INTO transcripts VALUES('v-english','en','youtube','manual','','2026-01-01');
                INSERT INTO transcripts VALUES('v-code','fr','generate','custom-engine','custom','2026-01-01');
                INSERT INTO transcript_parts VALUES('v-health','vi','youtube','manual',0,12,8,'Kháng insulin liên quan chuyển hóa.');
                INSERT INTO transcript_parts VALUES('v-health','vi','youtube','manual',1,132,10,'Tập luyện giảm kháng insulin.');
                INSERT INTO transcript_parts VALUES('v-code','vi','youtube','manual',0,70,15,'C++ và NAD+ là các chuỗi có dấu câu.');
                INSERT INTO transcript_parts VALUES('v-other','vi','youtube','manual',0,9,11,'Kháng insulin và dinh dưỡng.');
                INSERT INTO transcript_parts VALUES('v-english','en','youtube','manual',0,0,20,'Insulin resistance.');
                INSERT INTO transcript_parts VALUES('v-code','fr','generate','custom-engine',0,80,10,'Insulin transcript custom engine.');
                INSERT INTO transcript_parts VALUES('v-code','fr','generate','custom-engine',1,220,10,'Insulin second custom segment.');
            """)
        self.source_digest = hashlib.sha256(self.source.read_bytes()).digest()
        self.env = os.environ.copy()
        for key in ("PYTHONPATH", "YT_FINDER_ROOT"):
            self.env.pop(key, None)
        self.env.update(
            HF_HOME=str(self.cwd / "models"),
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            NO_COLOR="1",
            PYTHONIOENCODING="utf-8",
        )

    def run_cli(self, *arguments, entry="yt", stdin=None, expected=0):
        process = subprocess.run(
            [str(BIN / entry), *map(str, arguments)],
            cwd=self.cwd,
            env=self.env,
            input=stdin,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            expected,
            f"{entry} {arguments!r}\nstdout: {process.stdout}\nstderr: {process.stderr}",
        )
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).digest(), self.source_digest
        )
        self.assertFalse((self.cwd / "models").exists())
        self.assertNotIn("Traceback", process.stderr)
        return process

    def search(self, *arguments, entry="yt", **kwargs):
        return self.run_cli(
            *(["search"] if entry == "yt" else []),
            *arguments,
            "--db",
            "transcripts.sqlite3",
            "--search-db",
            "search.sqlite3",
            "--log-file",
            "cli.jsonl",
            "--offline",
            entry=entry,
            **kwargs,
        )

    def test_installed_entrypoints_show_help_from_another_directory(self):
        for entry, arguments, expected_text in (
            ("yt", ["--help"], "collect"),
            ("yt", ["collect", "--help"], "--language"),
            ("yt-collect", ["collect", "--help"], "--mode"),
            ("yt-search", ["--help"], "--moments-per-video"),
        ):
            with self.subTest(entry=entry, arguments=arguments):
                result = self.run_cli(*arguments, entry=entry)
                self.assertIn(expected_text, result.stdout)
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.log.exists())

    def test_keyword_json_filters_language_and_returns_multiple_timestamped_moments(
        self,
    ):
        payload = json.loads(
            self.search("khang insulin", "--mode", "keyword", "--json").stdout
        )
        self.assertEqual(
            {item["video_id"] for item in payload["results"]},
            {"v-health", "v-other"},
        )
        self.assertEqual(payload["mode"], "keyword")
        self.assertIsNone(payload["model"])
        self.assertIsNone(payload["reranker"])
        health = next(
            item for item in payload["results"] if item["video_id"] == "v-health"
        )
        self.assertEqual({moment["start"] for moment in health["moments"]}, {12, 132})
        self.assertEqual({moment["end"] for moment in health["moments"]}, {20, 142})
        self.assertEqual(health["language"], "vi")
        for moment in health["moments"]:
            self.assertEqual(
                moment["url"],
                f"https://www.youtube.com/watch?v=v-health&t={int(moment['start'])}s",
            )
            self.assertIsNone(moment["similarity"])
        self.assertTrue(self.cache.is_file())
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn("command_completed", {record.get("phase") for record in records})
        self.assertNotIn("khang insulin", self.log.read_text())
        self.assertNotIn("Kháng insulin liên quan chuyển hóa.", self.log.read_text())

    def test_search_alias_combines_video_channel_filters_and_moment_limits(self):
        process = self.search(
            "insulin",
            "--mode",
            "keyword",
            "--json",
            "--video-id",
            "v-health",
            "--video-id",
            "v-other",
            "--channel",
            "Sức khỏe",
            "--top",
            "1",
            "--moments-per-video",
            "1",
            entry="yt-search",
        )
        payload = json.loads(process.stdout)
        self.assertEqual(payload["channel"], "health")
        self.assertEqual(payload["video_ids"], ["v-health", "v-other"])
        self.assertEqual(
            [item["video_id"] for item in payload["results"]], ["v-health"]
        )
        self.assertEqual(len(payload["results"][0]["moments"]), 1)
        mismatch = json.loads(
            self.search(
                "insulin",
                "--mode",
                "keyword",
                "--json",
                "--video-id",
                "v-health",
                "--channel",
                "other",
            ).stdout
        )
        self.assertEqual(mismatch["results"], [])

    def test_exact_mode_preserves_punctuation_and_vietnamese_diacritics(self):
        payload = json.loads(self.search("C++", "--mode", "exact", "--json").stdout)
        self.assertEqual([item["video_id"] for item in payload["results"]], ["v-code"])
        self.assertEqual(payload["results"][0]["start"], 70)
        self.assertIn("C++", payload["results"][0]["text"])
        for query in ("khang insulin", "completely absent phrase"):
            with self.subTest(query=query):
                payload = json.loads(
                    self.search(query, "--mode", "exact", "--json").stdout
                )
                self.assertEqual(payload["results"], [])

    def test_interactive_json_is_one_object_per_query_and_ends_at_eof(self):
        process = self.search(
            "--interactive",
            "--json",
            "--mode",
            "keyword",
            entry="yt-search",
            stdin="insulin\ncompletelyabsent\n",
        )
        payloads = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(
            [item["query"] for item in payloads], ["insulin", "completelyabsent"]
        )
        self.assertEqual(len(payloads[0]["results"]), 2)
        self.assertEqual(payloads[1]["results"], [])
        self.assertIn("query>", process.stderr)

    def test_interactive_terminal_commands_show_results_and_update_settings(self):
        process = self.search(
            "--interactive",
            "--mode",
            "keyword",
            stdin="/top 1\n/moments 1\ninsulin\n/show 1\n/settings\n/quit\n",
        )
        self.assertIn("1 video", process.stdout)
        self.assertIn("Top: 1", process.stdout)
        self.assertIn("Tối đa 1 đoạn/video", process.stdout)
        self.assertIn("đầy đủ", process.stdout)
        self.assertIn("https://www.youtube.com/watch?v=", process.stdout)
        self.assertNotIn("\033", process.stdout)

    def test_where_selects_english_and_custom_engine_with_complete_moments(self):
        where = "channel IN ('Sức khỏe') AND ((language='en' AND video_id='v-english') OR (description='O''Reilly' AND source='generate'))"
        for mode in ("keyword", "exact"):
            with self.subTest(mode=mode):
                payload = json.loads(
                    self.search(
                        "Insulin", "--where", where, "--mode", mode, "--json"
                    ).stdout
                )
                self.assertEqual(payload["where"], where)
                for field in (
                    "channel",
                    "video_ids",
                    "language",
                    "transcript_source",
                    "source_mode",
                ):
                    self.assertIsNone(payload[field], field)
                results = {item["video_id"]: item for item in payload["results"]}
                self.assertEqual(set(results), {"v-code", "v-english"})
                self.assertEqual(results["v-english"]["language"], "en")
                self.assertEqual(results["v-code"]["language"], "fr")
                self.assertEqual(results["v-code"]["source_mode"], "custom-engine")
                self.assertEqual(results["v-code"]["transcript_config"], "custom")
                self.assertEqual(
                    {moment["start"] for moment in results["v-code"]["moments"]},
                    {80, 220},
                )

    def test_where_rejects_every_explicit_legacy_filter_including_defaults(self):
        for flag, value in (
            ("--channel", "health"),
            ("--video-id", "v-english"),
            ("--language", "vi"),
            ("--transcript-source", "auto"),
            ("--source-mode", "manual"),
        ):
            with self.subTest(flag=flag):
                process = self.search(
                    "insulin",
                    "--where",
                    "language='en'",
                    "--mode",
                    "keyword",
                    flag,
                    value,
                    expected=2,
                )
                self.assertIn("--where", process.stderr)
                self.assertEqual(process.stdout, "")
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.log.exists())

    def test_invalid_where_reports_failure_and_subsequent_legacy_search_works(self):
        for where in (
            "",
            "video_id =",
            "text LIKE '%insulin%'",
            "SELECT * FROM videos",
            "1=1; DELETE FROM videos",
        ):
            with self.subTest(where=where):
                process = self.search(
                    "insulin",
                    "--where",
                    where,
                    "--mode",
                    "keyword",
                    "--json",
                    expected=1,
                )
                self.assertEqual(process.stdout, "")
                self.assertIn("Error:", process.stderr)
                self.assertFalse(self.cache.exists())
        payload = json.loads(
            self.search("insulin", "--mode", "keyword", "--json").stdout
        )
        self.assertEqual(
            {item["video_id"] for item in payload["results"]}, {"v-health", "v-other"}
        )

    def test_interactive_where_settings_and_results_keep_the_sql_scope(self):
        where = "source_mode='custom-engine' AND language='fr'"
        process = self.search(
            "--where",
            where,
            "--mode",
            "keyword",
            "--interactive",
            stdin="/settings\ninsulin\n/show 1\n/quit\n",
        )
        self.assertIn(f"WHERE: {where}", process.stdout)
        self.assertIn("Insulin transcript custom engine.", process.stdout)
        self.assertIn("Insulin second custom segment.", process.stdout)
        self.assertNotIn("Ngôn ngữ: vi", process.stdout)
        self.assertNotIn("Nguồn: auto", process.stdout)

    def test_search_reports_missing_input_and_rejects_old_schema_without_writes(self):
        for name, create in (("missing.sqlite3", False), ("old.sqlite3", True)):
            path = self.cwd / name
            if create:
                with contextlib.closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA user_version=1")
            before = path.read_bytes() if create else None
            with self.subTest(name=name):
                process = self.run_cli(
                    "search",
                    "insulin",
                    "--mode",
                    "keyword",
                    "--json",
                    "--db",
                    name,
                    "--search-db",
                    "unused.sqlite3",
                    "--log-file",
                    "cli.jsonl",
                    expected=1,
                )
                self.assertEqual(process.stdout, "")
                self.assertIn("Error:", process.stderr)
                if create:
                    self.assertEqual(path.read_bytes(), before)
                else:
                    self.assertFalse(path.exists())
                self.assertFalse((self.cwd / "unused.sqlite3").exists())

    def test_invalid_collection_requests_fail_before_creating_a_database(self):
        for entry, prefix, arguments, message, code in (
            ("yt", ["collect"], ["--mode", "direct"], "requires one --language", 1),
            (
                "yt-collect",
                ["collect"],
                ["--mode", "generate", "--language", "vi"],
                "only valid in direct mode",
                1,
            ),
            ("yt", ["collect"], ["--language", "vi"], "--mode", 2),
        ):
            with self.subTest(entry=entry, arguments=arguments):
                process = self.run_cli(
                    *prefix,
                    "https://www.youtube.com/watch?v=not-a-live-request",
                    *arguments,
                    "--db",
                    "collection.sqlite3",
                    "--log-file",
                    "collect.jsonl",
                    entry=entry,
                    expected=code,
                )
                self.assertIn(message, process.stderr)
                self.assertFalse((self.cwd / "collection.sqlite3").exists())

    def test_search_refuses_cache_aliases_and_sqlite_sidecars(self):
        symbolic = self.cwd / "symbolic.sqlite3"
        symbolic.symlink_to(self.source)
        hard = self.cwd / "hard.sqlite3"
        os.link(self.source, hard)
        for cache in (self.source, symbolic, hard, Path(str(self.source) + "-wal")):
            with self.subTest(cache=cache.name):
                process = self.run_cli(
                    "search",
                    "insulin",
                    "--mode",
                    "keyword",
                    "--db",
                    self.source,
                    "--search-db",
                    cache,
                    "--log-file",
                    self.log,
                    expected=1,
                )
                self.assertIn("cache must be different", process.stderr)
        self.assertFalse(Path(str(self.source) + "-wal").exists())

    def test_search_and_collect_refuse_log_aliases_before_opening_inputs(self):
        symbolic = self.cwd / "log-symlink.jsonl"
        symbolic.symlink_to(self.source)
        hard = self.cwd / "log-hardlink.jsonl"
        os.link(self.source, hard)
        for entry, prefix in (
            ("yt", ["search", "insulin", "--mode", "keyword"]),
            (
                "yt-collect",
                ["collect", "unused", "--mode", "direct", "--language", "vi"],
            ),
        ):
            for log in (
                self.source,
                symbolic,
                hard,
                Path(str(self.source) + "-journal"),
            ):
                with self.subTest(entry=entry, log=log.name):
                    process = self.run_cli(
                        *prefix,
                        "--db",
                        self.source,
                        "--log-file",
                        log,
                        entry=entry,
                        expected=2,
                    )
                    self.assertIn("Log files must differ", process.stderr)
        self.assertFalse(Path(str(self.source) + "-journal").exists())


if __name__ == "__main__":
    unittest.main()

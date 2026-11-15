import contextlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_cli.collector import main as collect_main
from yt_cli.logging_config import command_logging, validate_log_path
from yt_cli.search import main as search_main


class CliLoggingTests(unittest.TestCase):
    def test_stderr_file_context_and_restore(self):
        logger = logging.getLogger("video_search")
        original = (logger.level, logger.propagate, logger.handlers[:])
        existing = logging.NullHandler()
        logger.addHandler(existing)
        logger.setLevel(logging.ERROR)
        logger.propagate = False
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "logs" / "cli.jsonl"
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    command_logging("search", str(path)),
                ):
                    logger.debug(
                        "Embedding batch",
                        extra={"phase": "embedding", "completed": 16},
                    )
                    logger.info("Ready", extra={"video_id": "abc"})
                    logging.getLogger("third_party").debug("secret external debug")
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("Ready", stderr.getvalue())
                self.assertNotIn("Embedding batch", stderr.getvalue())
                batch = next(row for row in rows if row["phase"] == "embedding")
                self.assertEqual(batch["completed"], 16)
                self.assertEqual(logger.level, logging.ERROR)
                self.assertFalse(logger.propagate)
                self.assertEqual(logger.handlers, [*original[2], existing])
                self.assertNotIn("secret external debug", path.read_text())
        finally:
            logger.removeHandler(existing)
            logger.setLevel(original[0])
            logger.propagate = original[1]

    def test_rotation_redaction_and_failure_cleanup(self):
        logger = logging.getLogger("yt_cli")
        original = (logger.level, logger.propagate, logger.handlers[:])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cli.jsonl"
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(RuntimeError),
                command_logging("search", str(path), max_bytes=400, backup_count=2),
            ):
                for i in range(12):
                    logger.debug(
                        "batch %s https://example.com/private?sig=secret token=secret",
                        i,
                    )
                try:
                    raise RuntimeError("private transcript payload")
                except RuntimeError:
                    logger.exception("Operation failed")
                    raise
            paths = [
                item
                for item in Path(directory).glob("cli.jsonl*")
                if item.suffix != ".lock"
            ]
            self.assertGreater(len(paths), 1)
            self.assertLessEqual(len(paths), 3)
            content = "".join(item.read_text() for item in paths)
            self.assertNotIn("sig=secret", content)
            self.assertNotIn("token=secret", content)
            self.assertNotIn("private transcript payload", content)
            rows = [
                json.loads(line)
                for item in paths
                for line in item.read_text().splitlines()
            ]
            self.assertTrue(any(row.get("phase") == "command_failed" for row in rows))
        self.assertEqual((logger.level, logger.propagate, logger.handlers), original)

    def test_log_alias_guards_inputs_backups_and_lock_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.sqlite3"
            source.write_bytes(b"preserved")
            alias = Path(directory) / "alias"
            alias.symlink_to(source)
            with self.assertRaises(ValueError):
                validate_log_path(alias, [source])
            log_file = Path(directory) / "cli.jsonl"
            os.link(source, str(log_file) + ".1")
            with self.assertRaises(ValueError):
                validate_log_path(log_file, [source])
            self.assertFalse(log_file.exists())
            self.assertEqual(source.read_bytes(), b"preserved")
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                search_main(["query", "--db", str(source), "--log-file", str(source)])
            self.assertEqual(source.read_bytes(), b"preserved")

    def test_cli_log_cannot_write_active_input_wal_or_its_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "input.sqlite3"
            with contextlib.closing(sqlite3.connect(db)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE content(value TEXT)")
                connection.execute("INSERT INTO content VALUES('preserved')")
                connection.commit()
                wal = Path(str(db) + "-wal")
                before = wal.read_bytes()
                symlink_db = Path(directory) / "input-alias.sqlite3"
                symlink_db.symlink_to(db)
                hardlink = Path(directory) / "wal-alias.log"
                os.link(wal, hardlink)
                for target in (wal, hardlink):
                    with self.subTest(target=target):
                        with (
                            contextlib.redirect_stderr(io.StringIO()),
                            patch("yt_cli.search.SearchSession") as service,
                            self.assertRaises(SystemExit),
                        ):
                            search_main(
                                [
                                    "query",
                                    "--db",
                                    str(symlink_db),
                                    "--log-file",
                                    str(target),
                                ]
                            )
                        service.assert_not_called()
                self.assertEqual(wal.read_bytes(), before)
                self.assertEqual(
                    connection.execute("SELECT * FROM content").fetchall(),
                    [("preserved",)],
                )
                self.assertEqual(
                    connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
                )

    def test_concurrent_processes_rotate_without_losing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cli.jsonl"
            script = """import logging, sys
from yt_cli.logging_config import command_logging
with command_logging('search', sys.argv[1], max_bytes=1000, backup_count=100):
    for number in range(20):
        logging.getLogger('yt_cli.writer').debug('writer=%s record=%s', sys.argv[2], number)
"""
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(path), str(writer)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for writer in range(4)
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout, "")
                self.assertNotIn("Logging error", stderr)
            rows = [
                json.loads(line)
                for item in Path(directory).glob("cli.jsonl*")
                if item.suffix != ".lock"
                for line in item.read_text().splitlines()
            ]
            messages = [
                row["message"] for row in rows if row["logger"] == "yt_cli.writer"
            ]
            self.assertEqual(len(messages), 80)
            self.assertEqual(len(set(messages)), 80)

    def test_each_invocation_correlates_core_app_and_forwarded_worker_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cli.jsonl"
            with contextlib.redirect_stderr(io.StringIO()):
                for command in ("search", "collect"):
                    with command_logging(command, str(path)):
                        logging.getLogger("video_search.models").debug("Core detail")
                        logging.getLogger("yt_cli.adapter").info("App detail")
                        logging.getLogger("video_search.search").info(
                            "Forwarded worker detail", extra={"worker_pid": 123456}
                        )
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            run_ids = set()
            for command in ("search", "collect"):
                records = [row for row in rows if row["command"] == command]
                ids = {row["run_id"] for row in records}
                self.assertEqual(len(ids), 1)
                run_ids.update(ids)
                self.assertTrue(all(row["pid"] == os.getpid() for row in records))
                self.assertTrue(
                    any(row["logger"] == "video_search.models" for row in records)
                )
                self.assertTrue(
                    any(row["logger"] == "yt_cli.adapter" for row in records)
                )
                worker = next(row for row in records if "worker_pid" in row)
                self.assertEqual(worker["worker_pid"], 123456)
            self.assertEqual(len(run_ids), 2)

    def test_search_json_stdout_is_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cli.jsonl"
            with patch("yt_cli.search.SearchSession") as service:
                session = service.return_value.__enter__.return_value
                session.search.return_value = []
                session.channel_id = None
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    search_main(["private query", "--json", "--log-file", str(path)])
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["results"], [])
            self.assertIn("search completed", stderr.getvalue())
            self.assertNotIn("private query", path.read_text())

    def test_custom_python_handler_receives_redacted_records_and_stays_open(self):
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
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            command_logging(
                "search",
                handlers=[captured],
                replace_defaults=True,
            ),
        ):
            logger = logging.getLogger("video_search.models")
            logger.debug("ignored by handler level")
            logger.info("token=secret-one https://example.test/watch?v=1")
            try:
                raise RuntimeError("private transcript body")
            except RuntimeError:
                logger.exception("Boom")

        self.assertFalse(captured.closed)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        messages = [record.getMessage() for record in captured.records]
        self.assertFalse(any("ignored by handler level" in msg for msg in messages))
        joined = "\n".join(messages)
        self.assertNotIn("secret-one", joined)
        self.assertNotIn("https://example.test", joined)
        for record in captured.records:
            if record.exc_info:
                self.assertNotIn("private transcript body", str(record.exc_info[1]))
            self.assertEqual(getattr(record, "command", None), "search")

    def test_cli_rejects_log_handler_flag(self):
        for fn, argv in (
            (search_main, ["query", "--log-handler", "null"]),
            (
                collect_main,
                ["collect", "URL", "--mode", "direct", "--log-handler", "null"],
            ),
        ):
            with (
                self.subTest(fn=fn.__module__),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                fn(argv)
            self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

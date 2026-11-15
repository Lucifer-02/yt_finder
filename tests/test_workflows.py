"""Application workflow parity and safety, without model or network calls."""

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from yt_workflows.config import EvaluationRequest
from yt_workflows.evaluation import (
    run_evaluation,
    run_isolated_model,
    validate_output_paths,
)

from tests.sqlite_fixture import SQLiteFixture


class WorkflowTests(unittest.TestCase):
    def test_model_stdout_cannot_corrupt_json_worker_protocol(self):
        script = """
import runpy, sys, types
module = types.ModuleType('yt_workflows.evaluation')
def run_model(args, on_progress=None):
    print('external model diagnostic')
    return {'model': args.worker, 'status': 'completed'}
module.run_model = run_model
module.validate_output_paths = lambda *args: None
sys.modules['yt_workflows.evaluation'] = module
runpy.run_module('yt_workflows.eval_worker', run_name='__main__')
"""
        payload = {
            "request": asdict(EvaluationRequest()),
            "model": "minilm",
            "snapshot": "snapshot.jsonl",
            "target": "report.json",
        }
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps(payload) + "\n",
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            messages,
            [{"type": "result", "result": {"model": "minilm", "status": "completed"}}],
        )
        self.assertIn("external model diagnostic", result.stderr)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "input.sqlite3"
        fixture = SQLiteFixture(self.db)
        fixture.db.execute("INSERT INTO channels VALUES('c','channel','url')")
        fixture.db.execute("INSERT INTO videos VALUES('v','c','title','')")
        fixture.db.execute(
            "INSERT INTO transcripts VALUES('v','vi','youtube','manual','','now')"
        )
        fixture.db.execute(
            "INSERT INTO transcript_parts VALUES('v','vi','youtube','manual',0,0,30,'nhà văn bác sĩ')"
        )
        fixture.db.commit()
        fixture.close()
        self.dataset = self.root / "queries.jsonl"
        self.dataset.write_text(
            json.dumps({"id": "q", "query": "nhà văn", "relevant": []}) + "\n"
        )
        self.request = EvaluationRequest(
            db=str(self.db),
            search_db=str(self.root / "cache.sqlite3"),
            dataset=str(self.dataset),
            output=str(self.root / "report.json"),
            models=("minilm", "e5-small"),
            max_windows=0,
        )

    def test_all_models_share_snapshot_and_reports_preserve_configuration(self):
        before = self.db.read_bytes()
        observed = []
        events = []

        def runner(request, model, snapshot, target, progress):
            observed.append(
                (model, hashlib.sha256(snapshot.read_bytes()).hexdigest(), request)
            )
            report = {"model": model, "metrics": {"positive_queries": 0}}
            target.write_text(json.dumps(report))
            return report

        report = run_evaluation(self.request, events.append, runner=runner)
        self.assertEqual([item[0] for item in observed], ["minilm", "e5-small"])
        self.assertEqual(len({item[1] for item in observed}), 1)
        self.assertTrue(all(item[2].max_windows == 0 for item in observed))
        self.assertFalse(report["sampled"])
        self.assertEqual(json.loads(Path(self.request.output).read_text()), report)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(events[-1]["phase"], "evaluation_complete")
        snapshot = Path(self.request.output).with_suffix(".corpus.jsonl")
        windows = [json.loads(line) for line in snapshot.read_text().splitlines()]
        self.assertTrue(
            {"language", "source_mode", "transcript_config"} <= windows[0].keys()
        )
        with self.assertRaisesRegex(ValueError, "filters"):
            run_evaluation(
                replace(
                    self.request,
                    corpus_file=str(snapshot),
                    language="en",
                    output=str(self.root / "other.json"),
                ),
                runner=runner,
            )

    def test_output_hardlink_alias_is_rejected_before_any_write(self):
        target = self.root / "report-minilm.json"
        os.link(self.dataset, target)
        original = self.dataset.read_bytes()
        with self.assertRaisesRegex(ValueError, "read-only"):
            run_evaluation(self.request, runner=Mock())
        self.assertEqual(self.dataset.read_bytes(), original)
        self.assertFalse(Path(self.request.search_db).exists())
        self.assertFalse(Path(self.request.output).exists())

    def test_active_wal_and_sidecar_aliases_are_protected_before_analysis_writes(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("UPDATE channels SET title='committed in WAL'")
            connection.commit()
            wal = Path(str(self.db) + "-wal")
            self.assertTrue(wal.exists())
            before = wal.read_bytes()
            alias = self.root / "input-alias.sqlite3"
            alias.symlink_to(self.db)
            hardlink = self.root / "wal-alias.json"
            os.link(wal, hardlink)
            for target in (wal, hardlink):
                with (
                    self.subTest(target=target),
                    self.assertRaisesRegex(ValueError, "sidecar"),
                ):
                    run_evaluation(
                        replace(self.request, db=str(alias), output=str(target)),
                        runner=Mock(),
                    )
            self.assertEqual(wal.read_bytes(), before)
            self.assertEqual(
                connection.execute("SELECT title FROM channels").fetchall(),
                [("committed in WAL",)],
            )
            self.assertFalse(Path(self.request.search_db).exists())
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
            for suffix in ("-wal", "-shm", "-journal"):
                with self.assertRaises(ValueError):
                    validate_output_paths([alias], [str(alias) + suffix])
                with self.assertRaises(ValueError):
                    validate_output_paths(
                        [],
                        [
                            self.root / "cache.sqlite3",
                            self.root / ("cache.sqlite3" + suffix),
                        ],
                    )

    def test_symlink_caller_side_wal_sidecar_is_protected_before_analysis_writes(self):
        alias_dir = self.root / "links"
        alias_dir.mkdir()
        alias_db = alias_dir / "alias.sqlite3"
        alias_db.symlink_to(self.db)
        alias_wal = Path(str(alias_db) + "-wal")
        alias_wal.write_bytes(b"caller-sidecar")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            run_evaluation(
                replace(self.request, db=str(alias_db), output=str(alias_wal)),
                runner=Mock(),
            )
        self.assertEqual(alias_wal.read_bytes(), b"caller-sidecar")
        real_cache = self.root / "real-cache.sqlite3"
        real_cache.touch()
        alias_cache = alias_dir / "cache-alias.sqlite3"
        alias_cache.symlink_to(real_cache)
        alias_cache_wal = Path(str(alias_cache) + "-wal")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            run_evaluation(
                replace(
                    self.request,
                    search_db=str(alias_cache),
                    output=str(alias_cache_wal),
                ),
                runner=Mock(),
            )
        self.assertFalse(alias_cache_wal.exists())

    def test_isolated_runner_forwards_progress_without_mutating_environment(self):
        before = dict(os.environ)
        process = Mock()
        process.stdout = iter(
            [
                json.dumps(
                    {
                        "type": "progress",
                        "event": {"phase": "evaluation_queries", "completed": 10},
                    }
                )
                + "\n",
                json.dumps({"type": "result", "result": {"model": "minilm"}}) + "\n",
            ]
        )
        # A real TextIO is closable and iterable.
        import io

        process.stdout = io.StringIO("".join(process.stdout))
        process.stdin = io.StringIO()
        process.wait.return_value = 0
        process.poll.return_value = 0
        progress = []
        with patch(
            "yt_workflows.evaluation.subprocess.Popen", return_value=process
        ) as popen:
            report = run_isolated_model(
                replace(self.request, offline=True),
                "minilm",
                self.root / "snapshot",
                self.root / "model.json",
                progress.append,
            )
        self.assertEqual(report, {"model": "minilm"})
        self.assertEqual(progress[0]["completed"], 10)
        self.assertEqual(popen.call_args.kwargs["env"]["HF_HUB_OFFLINE"], "1")
        self.assertNotIn("start_new_session", popen.call_args.kwargs)
        self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()

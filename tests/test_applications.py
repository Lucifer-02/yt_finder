"""Application boundaries and CLI dispatch without network or model calls."""

import ast
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yt_cli import launcher
from yt_web import create_app
from yt_web.config import WebConfig, find_repository
from yt_web.processes import ProcessWorker, WorkerError

ROOT = Path(__file__).resolve().parents[1]


class CLIApplicationTests(unittest.TestCase):
    def test_launcher_selects_correct_interpreter_and_preserves_arguments_and_environment(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / ".venv" / "bin" / "python"
            binary.parent.mkdir(parents=True)
            binary.touch()
            before = dict(os.environ)
            for command, module in (
                ("collect", "yt_cli.collector"),
                ("search", "yt_cli.search"),
            ):
                arguments = ["argument with spaces", "$(literal)", "--json"]
                with (
                    self.subTest(command=command),
                    patch(
                        "yt_cli.launcher.subprocess.Popen",
                        return_value=Mock(wait=Mock(return_value=7)),
                    ) as run,
                ):
                    self.assertEqual(
                        launcher.run_component(command, arguments, root=root), 7
                    )
                self.assertEqual(
                    run.call_args.args[0],
                    [
                        str(binary),
                        "-m",
                        module,
                        *arguments,
                    ],
                )
                self.assertNotIn("shell", run.call_args.kwargs)
                self.assertNotIn("cwd", run.call_args.kwargs)
                self.assertEqual(
                    run.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0],
                    str(root / "src"),
                )
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("benchmark", launcher.COMMANDS)
            self.assertNotIn("eval", launcher.COMMANDS)

    def test_launcher_handles_interrupts_and_normalizes_signal_exit_codes(self):
        cases = (
            ([0], 0, [None], False, False),
            ([-15], 143, [None], False, False),
            ([KeyboardInterrupt(), -2], 130, [None, 10], False, False),
            (
                [KeyboardInterrupt(), subprocess.TimeoutExpired("python", 10), -15],
                143,
                [None, 10, 5],
                True,
                False,
            ),
            (
                [
                    KeyboardInterrupt(),
                    subprocess.TimeoutExpired("python", 10),
                    subprocess.TimeoutExpired("python", 5),
                    -9,
                ],
                137,
                [None, 10, 5, None],
                True,
                True,
            ),
            (
                [KeyboardInterrupt(), KeyboardInterrupt(), -9],
                137,
                [None, 10, None],
                False,
                True,
            ),
        )
        for waits, expected, timeouts, terminated, killed in cases:
            process = Mock(wait=Mock(side_effect=waits))
            with (
                self.subTest(waits=waits),
                patch("yt_cli.launcher.Path.is_file", return_value=True),
                patch("yt_cli.launcher.subprocess.Popen", return_value=process),
            ):
                self.assertEqual(
                    launcher.run_component("search", ["query"], root=ROOT), expected
                )
            self.assertEqual(
                [call.kwargs.get("timeout") for call in process.wait.call_args_list],
                timeouts,
            )
            self.assertEqual(process.terminate.call_count, int(terminated))
            self.assertEqual(process.kill.call_count, int(killed))

    def test_missing_environment_is_reported_without_running_installation(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("yt_cli.launcher.subprocess.Popen") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "uv sync --all-extras"):
                launcher.run_component("collect", [], root=Path(temporary))
            run.assert_not_called()

    def test_main_routes_collect_search_and_subcommand_help(self):
        for argv, expected in (
            (
                ["collect", "URL", "--mode", "direct", "--language", "vi"],
                ("collect", ["collect", "URL", "--mode", "direct", "--language", "vi"]),
            ),
            (["collect", "--help"], ("collect", ["collect", "--help"])),
            (
                ["search", "description", "--top", "3"],
                ("search", ["description", "--top", "3"]),
            ),
        ):
            with (
                self.subTest(argv=argv),
                patch("yt_cli.launcher.run_component", return_value=0) as run,
                self.assertRaises(SystemExit) as exc,
            ):
                launcher.main(argv)
            self.assertEqual(exc.exception.code, 0)
            run.assert_called_once_with(*expected)

    def test_interrupt_is_mapped_to_exit_130(self):
        with (
            patch("yt_cli.launcher.run_component", side_effect=KeyboardInterrupt),
            self.assertRaises(SystemExit) as exc,
        ):
            launcher.main(["search", "query"])
        self.assertEqual(exc.exception.code, 130)


class RepositoryDiscoveryTests(unittest.TestCase):
    def test_discovers_the_single_project_from_source_locations(self):
        for discover in (launcher.repository_root, find_repository):
            with (
                self.subTest(discover=discover.__module__),
                patch.dict(os.environ, {"YT_FINDER_ROOT": ""}),
            ):
                self.assertEqual(discover(), ROOT)

    def test_configured_repository_needs_only_the_root_project_and_library_packages(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "pyproject.toml").touch()
            for name in ("transcript_collector", "video_search"):
                package = root / "src" / name
                package.mkdir(parents=True)
                (package / "__init__.py").touch()
            for discover in (launcher.repository_root, find_repository):
                with (
                    self.subTest(discover=discover.__module__),
                    patch.dict(os.environ, {"YT_FINDER_ROOT": str(root)}),
                ):
                    self.assertEqual(discover(), root)

    def test_invalid_configured_repository_is_rejected_without_falling_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").touch()
            for discover, error in (
                (launcher.repository_root, RuntimeError),
                (find_repository, ValueError),
            ):
                with (
                    self.subTest(discover=discover.__module__),
                    patch.dict(os.environ, {"YT_FINDER_ROOT": str(root)}),
                    self.assertRaisesRegex(error, "YT_FINDER_ROOT"),
                ):
                    discover()


class SharedInterpreterTests(unittest.TestCase):
    def test_web_workers_use_the_root_environment_on_each_platform(self):
        root = Path("/repository")
        config = WebConfig(
            root, root / "input.sqlite3", root / "search.sqlite3", root / "web.sqlite3"
        )
        for platform, relative in (
            ("posix", "bin/python"),
            ("nt", "Scripts/python.exe"),
        ):
            with (
                self.subTest(platform=platform),
                patch("yt_web.config.os.name", platform),
            ):
                self.assertEqual(config.interpreter(), root / ".venv" / relative)

    def test_cli_uses_the_root_windows_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / ".venv/Scripts/python.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with (
                patch("yt_cli.launcher.os.name", "nt"),
                patch(
                    "yt_cli.launcher.subprocess.Popen",
                    return_value=Mock(wait=Mock(return_value=0)),
                ) as run,
            ):
                self.assertEqual(
                    launcher.run_component("search", ["query"], root=root), 0
                )
            self.assertEqual(run.call_args.args[0][0], str(binary))

    def test_missing_shared_worker_environment_does_not_start_a_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = WebConfig(
                root,
                root / "input.sqlite3",
                root / "search.sqlite3",
                root / "web.sqlite3",
            )
            worker = ProcessWorker(config, "video_search", "yt_web.workers.search")
            with (
                patch("yt_web.processes.subprocess.Popen") as process,
                self.assertRaisesRegex(WorkerError, "uv sync --all-extras"),
            ):
                worker._start()
            process.assert_not_called()

    def test_worker_uses_shared_sources_and_preserves_existing_pythonpath(self):
        config = WebConfig(
            ROOT,
            ROOT / "input.sqlite3",
            ROOT / "search.sqlite3",
            ROOT / "web.sqlite3",
        )
        worker = ProcessWorker(config, "video_search", "yt_web.workers.search")
        process = Mock(pid=123)
        with (
            patch.dict(os.environ, {"PYTHONPATH": "/existing/sources"}),
            patch("yt_web.config.Path.is_file", return_value=True),
            patch("yt_web.processes.subprocess.Popen", return_value=process) as spawn,
        ):
            before = dict(os.environ)
            self.assertIs(worker._start(), process)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(
            spawn.call_args.kwargs["env"]["PYTHONPATH"],
            os.pathsep.join([str(ROOT / "src"), "/existing/sources"]),
        )


class WebApplicationTests(unittest.TestCase):
    def test_factory_is_side_effect_free_and_delegates_to_injected_gateways(self):
        collector, search = Mock(), Mock()
        result = {"scope": "single video", "counts": {"indexed": 1}}
        found = [{"video_id": "v", "text": "caption"}]
        collector.collect.return_value = result
        search.search.return_value = found
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            app = create_app(collector=collector, search=search)
            collector.assert_not_called()
            search.assert_not_called()
            request = {"target": "URL", "mode": "direct", "language": "vi"}
            on_event = Mock()
            self.assertIs(app.collect(request, on_event=on_event), result)
            self.assertIs(app.search("query", 3, 0.2), found)
        collector.collect.assert_called_once_with(request, on_event=on_event)
        search.search.assert_called_once_with("query", 3, 0.2, moments_per_video=3)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_gateway_failures_propagate_without_becoming_terminal_exits(self):
        collector, search = Mock(), Mock()
        collector.collect.side_effect = ValueError("invalid request")
        search.search.side_effect = RuntimeError("worker unavailable")
        app = create_app(collector=collector, search=search)
        with self.assertRaisesRegex(ValueError, "invalid request"):
            app.collect({})
        with self.assertRaisesRegex(RuntimeError, "worker unavailable"):
            app.search("query")


class DependencyBoundaryTests(unittest.TestCase):
    def test_business_libraries_have_no_application_service_modules(self):
        for name in ("transcript_collector", "video_search"):
            package = ROOT / "src" / name
            self.assertTrue((package / "__init__.py").is_file())
            self.assertFalse((package / "service.py").exists())

    def test_component_sources_never_import_applications_or_each_other(self):
        for name, forbidden in (
            (
                "transcript_collector",
                {"yt_cli", "yt_web", "yt_workflows", "video_search"},
            ),
            (
                "video_search",
                {"yt_cli", "yt_web", "yt_workflows", "transcript_collector"},
            ),
        ):
            paths = list((ROOT / "src" / name).rglob("*.py"))
            self.assertTrue(paths, name)
            for path in paths:
                for node in ast.walk(ast.parse(path.read_text())):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        modules = [node.module]
                    self.assertFalse(
                        forbidden.intersection(
                            module.split(".")[0] for module in modules
                        ),
                        str(path),
                    )

    def test_web_and_cli_launcher_do_not_import_component_libraries(self):
        # Component adapters use subprocesses in the root environment; the web parent must
        # not import those modules or either model library.
        paths = [
            path
            for path in (ROOT / "src/yt_web").rglob("*.py")
            if "workers" not in path.parts
        ]
        self.assertTrue(paths)
        paths += [ROOT / "src/yt_cli/launcher.py", ROOT / "src/yt_cli/__init__.py"]
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(
                        node.module.split(".")[0],
                        {"transcript_collector", "video_search", "yt_cli"}
                        if "yt_web" in path.parts
                        else {"transcript_collector", "video_search"},
                        str(path),
                    )

import contextlib
import io
import os
import sqlite3
import unicodedata
import unittest
from unittest.mock import Mock, call, patch

from video_search.search import SearchEngine, SearchResult
from yt_cli.interactive import InteractiveUI, run_interactive
from yt_cli.search import parser


def result() -> SearchResult:
    return {
        "video_id": "video",
        "channel_id": "channel",
        "channel_title": "Kênh",
        "title": "Chăm sóc sức khỏe",
        "transcript_source": "youtube",
        "source_mode": "manual",
        "language": "vi",
        "transcript_config": "",
        "start": 150.0,
        "end": 210.0,
        "text": "Nội dung transcript. " * 80 + "PHẦN CUỐI.",
        "url": "https://www.youtube.com/watch?v=video&t=150s",
        "score": 0.01,
        "similarity": 0.5,
    }


class InteractiveTests(unittest.TestCase):
    def run_session(self, engine, inputs, query=None):
        args = parser().parse_args(["--interactive"])
        args.query = query
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch("builtins.input", side_effect=inputs),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            run_interactive(engine, args)
        return output.getvalue(), errors.getvalue()

    def test_commands_do_not_search_and_show_keeps_full_transcript(self):
        engine = Mock(spec=SearchEngine)
        engine.search.return_value = [result()]
        output, _ = self.run_session(
            engine,
            ["/help", "sức khỏe", "/show 0", "/show abc", "/show 1", "/quit"],
        )
        engine.search.assert_called_once_with("sức khỏe", 5, None, moments_per_video=3)
        self.assertEqual(output.count("PHẦN CUỐI."), 1)
        self.assertIn("https://www.youtube.com/watch?v=video&t=150s", output)
        self.assertNotIn("\033", output)

    def test_search_errors_allow_retry_without_showing_stale_results(self):
        for error in (
            ValueError("query too long"),
            RuntimeError("model failure"),
            OSError("download failure"),
            sqlite3.OperationalError("database is locked"),
            ImportError("missing dependency"),
        ):
            with self.subTest(error=error):
                engine = Mock(spec=SearchEngine)
                engine.search.side_effect = [[result()], error, []]
                output, errors = self.run_session(
                    engine, ["first", "failed", "/show 1", "retry", "/quit"]
                )
                self.assertEqual(engine.search.call_count, 3)
                self.assertNotIn("PHẦN CUỐI.", output)
                self.assertIn(str(error), errors)
                self.assertIn("Không có transcript phù hợp", output)

    def test_empty_results_replace_previous_results(self):
        engine = Mock(spec=SearchEngine)
        engine.search.side_effect = [[result()], []]
        output, _ = self.run_session(engine, ["second", "/show 1", ""], query="first")
        self.assertEqual(engine.search.call_count, 2)
        self.assertNotIn("PHẦN CUỐI.", output)

    def test_eof_and_interrupt_at_prompt_exit_without_search(self):
        for ending in (EOFError, KeyboardInterrupt):
            with self.subTest(ending=ending):
                engine = Mock(spec=SearchEngine)
                self.run_session(engine, [ending])
                engine.search.assert_not_called()

    def test_terminal_prompt_is_passed_to_readline(self):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", return_value="query") as read,
        ):
            self.assertEqual(InteractiveUI().read_query(), "query")
        read.assert_called_once_with("Tìm > ")

    def test_session_commands_reuse_results_and_apply_top_to_next_search(self):
        engine = Mock(spec=SearchEngine)
        engine.search.return_value = [result()]
        output, _ = self.run_session(
            engine,
            ["first", "/top 2", "/settings", "/results", "second", "/quit"],
        )
        self.assertEqual(
            engine.search.call_args_list,
            [
                call("first", 5, None, moments_per_video=3),
                call("second", 2, None, moments_per_video=3),
            ],
        )
        self.assertEqual(output.count("1. Chăm sóc sức khỏe"), 3)
        self.assertIn("Kết quả cho: first", output)
        self.assertIn("Top: 2", output)
        self.assertNotIn("PHẦN CUỐI.", output)

    def test_invalid_top_does_not_change_limit(self):
        engine = Mock(spec=SearchEngine)
        engine.search.return_value = []
        output, _ = self.run_session(
            engine,
            ["/top", "/top 0", "/top -2", "/top abc", "/top 2 extra", "find", ""],
        )
        engine.search.assert_called_once_with("find", 5, None, moments_per_video=3)
        self.assertEqual(output.count("Dùng /top N"), 5)

    def test_results_command_does_not_reuse_results_after_error(self):
        engine = Mock(spec=SearchEngine)
        engine.search.side_effect = [[result()], RuntimeError("failed")]
        output, _ = self.run_session(
            engine, ["/results", "first", "failed", "/results", "/quit"]
        )
        self.assertEqual(output.count("Chưa có kết quả"), 2)
        self.assertEqual(output.count("1. Chăm sóc sức khỏe"), 1)

    def test_highlight_matches_vietnamese_and_literal_terms_after_wrapping(self):
        ui = InteractiveUI()
        ui.color = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ui.paragraph(
                unicodedata.normalize("NFD", "SỨC KHỎE và NAD+ trong transcript."),
                query="sức khỏe NAD+",
            )
        self.assertIn("\033[1;33mSỨC\033[0m", output.getvalue())
        self.assertIn("\033[1;33mKHỎE\033[0m", output.getvalue())
        self.assertIn("\033[1;33mNAD+\033[0m", output.getvalue())
        self.assertEqual(ui.highlight("vitamin", "vi"), "vitamin")

    def test_no_color_disables_highlight_on_tty(self):
        with (
            patch("sys.stdout.isatty", return_value=True),
            patch.dict(os.environ, {"NO_COLOR": "1"}),
        ):
            self.assertEqual(InteractiveUI().highlight("sức khỏe", "sức"), "sức khỏe")

    def test_narrow_output_and_control_characters(self):
        item = result()
        item["title"] = "Title\033[2J\rInjected"
        engine = Mock(spec=SearchEngine)
        engine.search.return_value = [item]
        with patch(
            "yt_cli.interactive.shutil.get_terminal_size",
            return_value=os.terminal_size((24, 24)),
        ):
            output, _ = self.run_session(engine, ["find", "/show 1", "/quit"])
        self.assertNotIn("\033", output)
        self.assertNotIn("\r", output)
        self.assertIn("PHẦN CUỐI.", output)


if __name__ == "__main__":
    unittest.main()

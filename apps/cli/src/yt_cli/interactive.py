"""Small, line-oriented terminal UI; no additional runtime dependencies."""

import argparse
import contextlib
import os
import re
import shutil
import sqlite3
import sys
import textwrap
import unicodedata
from time import perf_counter

from video_search.search import SearchResult, result_moments

from .search_session import Searcher


def display_text(text: str) -> str:
    """Keep stored text and queries from emitting terminal control sequences."""
    return unicodedata.normalize(
        "NFC", "".join(char for char in " ".join(text.split()) if char.isprintable())
    )


def timestamp(seconds: float) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class InteractiveUI:
    def __init__(self):
        self.color = (
            sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )

    def styled(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    @property
    def width(self) -> int:
        return max(20, min(100, shutil.get_terminal_size((100, 24)).columns))

    def divider(self, label: str = ""):
        label = display_text(label)
        line = f"── {label} ".ljust(self.width, "─") if label else "─" * self.width
        print(self.styled(line, "2"))

    def highlight(self, text: str, query: str) -> str:
        # Match literal words, including Vietnamese and terms such as NAD+.
        terms = sorted(set(display_text(query).split()), key=len, reverse=True)
        if not self.color or not terms:
            return text
        pattern = "|".join(
            (r"(?<!\w)" if term[0].isalnum() else "")
            + re.escape(term)
            + (r"(?!\w)" if term[-1].isalnum() else "")
            for term in terms
        )
        return re.sub(
            pattern,
            lambda match: self.styled(match[0], "1;33"),
            text,
            flags=re.IGNORECASE,
        )

    def paragraph(
        self,
        text: str,
        *,
        preview: bool = False,
        style: str | None = None,
        query: str = "",
    ) -> bool:
        wrapper = textwrap.TextWrapper(
            width=self.width,
            initial_indent="   ",
            subsequent_indent="   ",
            placeholder=" …",
        )
        text = display_text(text)
        truncated = preview and len(wrapper.wrap(text)) > 3
        wrapper.max_lines = 3 if preview else None
        lines = wrapper.wrap(text)
        content = "\n".join(lines)
        print(self.styled(content, style) if style else self.highlight(content, query))
        return truncated

    def settings(self, args: argparse.Namespace):
        rerank_enabled = (
            args.mode == "hybrid" and getattr(args, "rerank", None) is not False
        )
        rerank_note = (
            f" · Rerank: {'bật' if rerank_enabled else 'tắt'}"
            if args.mode == "hybrid"
            else ""
        )
        self.paragraph(
            f"Model: {args.model if args.mode in {'hybrid', 'semantic'} else 'không dùng'} · Top: {args.top} · Ngôn ngữ: {args.language} · "
            f"Nguồn: {args.transcript_source} · Chế độ: {args.mode}{rerank_note}"
        )
        self.paragraph(f"Tối đa {args.moments_per_video} đoạn/video")
        if args.source_mode:
            self.paragraph(f"Phương thức: {args.source_mode}")
        if args.video_ids is not None:
            self.paragraph(f"Video IDs: {', '.join(args.video_ids)}")
        if args.channel:
            self.paragraph(f"Kênh: {args.channel}")
        if args.min_similarity is not None:
            self.paragraph(f"Cosine tối thiểu: {args.min_similarity}")

    def help(self):
        print("Nhập nội dung cần tìm rồi Enter. ↑/↓: lịch sử (nếu terminal hỗ trợ).")
        print(
            "/show N: xem đầy đủ video N · /show N M: xem đoạn M · /results: xem lại danh sách"
        )
        print(
            "/top N: đổi số video · /moments N: đổi số đoạn/video · /settings: cấu hình phiên"
        )
        print("/help: trợ giúp · /quit: thoát")
        print("Dòng trống hoặc Ctrl+D cũng kết thúc phiên.\n", flush=True)
        print("Link video mở từ đầu cửa sổ transcript, có thể sớm hơn câu cần tìm.\n")

    def read_query(self) -> str:
        sys.stdout.flush()
        if sys.stdin.isatty() and sys.stdout.isatty():
            # Let readline know the prompt so editing/history redraw correctly.
            return input("Tìm > ")
        print("Tìm > ", file=sys.stderr, end="", flush=True)
        return input()

    def result(
        self,
        rank: int,
        result: SearchResult,
        *,
        preview: bool = True,
        query: str = "",
        moment_index: int | None = None,
    ):
        print()
        self.divider(f"Kết quả {rank}" if preview else f"Kết quả {rank} · đầy đủ")
        self.paragraph(f"{rank}. {result['title']}", style="1")
        metadata = (
            f"{result['channel_title']} · {result['language']} · "
            f"{result['transcript_source']}/{result['source_mode']}"
            + (
                f" · {result['transcript_config']}"
                if result["transcript_config"]
                else ""
            )
        )
        self.paragraph(metadata, style="2")
        moments = result_moments(result)
        for index, moment in enumerate(moments):
            if moment_index is not None and index != moment_index:
                continue
            self.paragraph(
                f"Đoạn {index + 1} · {timestamp(moment['start'])}–{timestamp(moment['end'])}",
                style="2",
            )
            print(
                f"   Xem từ {timestamp(moment['start'])}: "
                f"{self.styled(display_text(moment['url']), '36')}"
            )
            print()
            if self.paragraph(moment["text"], preview=preview, query=query):
                self.paragraph(f"/show {rank} {index + 1} để đọc đầy đủ", style="2")

    def results(self, results: list[SearchResult], query: str):
        for rank, result in enumerate(results, 1):
            self.result(rank, result, query=query)
        if results:
            print(
                "\n/show N: đầy đủ · /results: xem lại · Nhập truy vấn mới để tìm tiếp."
            )
        print(flush=True)


def show_result(
    ui: InteractiveUI, command: list[str], results: list[SearchResult], query: str
):
    try:
        rank = (
            int(command[1]) if len(command) in {2, 3} and command[0] == "/show" else 0
        )
        moment = int(command[2]) if len(command) == 3 else None
    except ValueError:
        rank, moment = 0, None
    if 1 <= rank <= len(results) and (
        moment is None or 1 <= moment <= len(result_moments(results[rank - 1]))
    ):
        ui.result(
            rank,
            results[rank - 1],
            preview=False,
            query=query,
            moment_index=moment - 1 if moment is not None else None,
        )
        print()
    else:
        print("Dùng /show N hoặc /show N M với số video và đoạn trong kết quả vừa tìm.")


def run_interactive(engine: Searcher, args: argparse.Namespace):
    ui = InteractiveUI()
    print(ui.styled("\nyt-search · Tìm trong transcript", "1;36"))
    ui.settings(args)
    print(
        "/show N: đầy đủ · /help: trợ giúp · /quit hoặc dòng trống: thoát\n", flush=True
    )
    if sys.stdin.isatty():
        with contextlib.suppress(ImportError):
            # readline adds editing and in-memory history to input() on supported OSes.
            import readline  # noqa: F401

    results: list[SearchResult] = []
    results_query = ""
    query = args.query
    first_search = True
    while True:
        if query is None:
            try:
                query = ui.read_query()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                return
        query = query.strip()
        if not query or query == "/quit":
            return
        if query == "/help":
            ui.help()
            query = None
            continue
        if query.startswith("/"):
            command = query.split()
            if command == ["/settings"]:
                ui.settings(args)
            elif command == ["/results"]:
                if results:
                    ui.paragraph(f"Kết quả cho: {results_query}", style="1")
                    ui.results(results, results_query)
                else:
                    print("Chưa có kết quả để hiển thị. Nhập truy vấn để tìm.")
            elif command[0] in {"/top", "/moments"}:
                try:
                    top = int(command[1]) if len(command) == 2 else 0
                except ValueError:
                    top = 0
                if top > 0:
                    if command[0] == "/top":
                        args.top = top
                        print(f"Lượt tìm tiếp theo trả tối đa {top} video.")
                    else:
                        args.moments_per_video = top
                        print(f"Lượt tìm tiếp theo trả tối đa {top} đoạn/video.")
                else:
                    print(f"Dùng {command[0]} N với N là số nguyên lớn hơn 0.")
            else:
                show_result(ui, command, results, results_query)
            query = None
            continue

        status = "Đang tìm…"
        if first_search and args.mode in {"hybrid", "semantic"}:
            status += " Lần đầu có thể cần tải mô hình và tạo chỉ mục."
        print(status, file=sys.stderr, flush=True)
        first_search = False
        # /show always refers to the latest attempt, never stale results after a failure.
        results = []
        results_query = ""
        started = perf_counter()
        try:
            found = engine.search(
                query,
                args.top,
                args.min_similarity,
                moments_per_video=args.moments_per_video,
            )
        except (ValueError, RuntimeError, OSError, sqlite3.Error, ImportError) as exc:
            print(f"Không thể tìm: {display_text(str(exc))}", file=sys.stderr)
            print("Nhập lại truy vấn hoặc /quit để thoát.", file=sys.stderr)
            query = None
            continue
        elapsed = perf_counter() - started
        results = found
        results_query = query
        print(ui.styled(f"\n{len(results)} video · {elapsed:.2f}s", "1"))
        ui.paragraph(query)
        if not results:
            print(
                "Không có transcript phù hợp. Thử cách diễn đạt khác hoặc kiểm tra bộ lọc."
            )
        ui.results(results, results_query)
        query = None

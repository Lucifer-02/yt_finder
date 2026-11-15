import argparse
import json
import os
import sqlite3
import sys

from video_search.config import DATA_DIR, SearchConfig
from video_search.models import DEFAULT_RERANKER, MODELS
from video_search.search import result_moments

from .arguments import positive
from .interactive import display_text, run_interactive, timestamp
from .logging_config import (
    add_logging_arguments,
    command_logging,
    redact,
    validate_logging_arguments,
)
from .search_session import SearchSession
from .search_terminal import print_progress


def parser():
    p = argparse.ArgumentParser(
        prog="yt-search",
        description="Search existing transcripts by meaning or keywords.",
    )
    p.add_argument("query", nargs="?", help="description of the desired content")
    p.add_argument(
        "--db",
        default=str(DATA_DIR / "index.sqlite3"),
        help="read-only transcript database",
    )
    p.add_argument(
        "--search-db",
        default=str(DATA_DIR / "search.sqlite3"),
        help="separate vector cache",
    )
    p.add_argument(
        "--channel",
        help="cached channel UC ID, exact name, or canonical URL; default: all",
    )
    p.add_argument(
        "--video-id",
        action="append",
        dest="video_ids",
        type=str.strip,
        help="only search this video ID; repeat for multiple videos (combined with other filters)",
    )
    p.add_argument(
        "--transcript-source",
        choices=["auto", "youtube", "generate"],
        default="auto",
        help="transcript source (default: YouTube, then generated Whisper)",
    )
    p.add_argument(
        "--language",
        default="vi",
        help="transcript language (default: vi; includes regional variants)",
    )
    p.add_argument(
        "--source-mode",
        help="manual, auto_generated, translated, unknown, or an engine name such as whisper",
    )
    p.add_argument(
        "--batch-size",
        type=positive,
        default=16,
        help="embedding batch size (default: 16)",
    )
    p.add_argument("--top", type=positive, default=5, help="maximum number of videos")
    p.add_argument(
        "--moments-per-video",
        type=positive,
        default=3,
        help="maximum non-overlapping moments per video (default: 3)",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--model", choices=list(MODELS), default="minilm")
    p.add_argument("--device", choices=["cpu", "cuda"], default=None)
    p.add_argument(
        "--mode",
        choices=["hybrid", "semantic", "keyword", "exact"],
        default="hybrid",
        help='hybrid: BM25 + vector (RRF + rerank, default); semantic: vector cosine; keyword: BM25 all terms/"phrases"; exact: verbatim substring',
    )
    p.add_argument(
        "--rerank",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="rerank top hybrid candidates with Cross-Encoder (default: on for hybrid)",
    )
    p.add_argument(
        "--min-similarity",
        type=float,
        help="optional model-specific cosine cutoff; requires calibration",
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        help="keep models loaded; /help for commands, empty line to exit",
    )
    p.add_argument("--offline", action="store_true", help="use downloaded models only")
    add_logging_arguments(p)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    validate_logging_arguments(args, p)
    with command_logging("search", args.log_file, args.log_level):
        _run(args)


def _run(args):
    os.environ.setdefault("HF_HOME", str(DATA_DIR / "models"))
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        if (not args.interactive and not args.query) or (
            args.query is not None and not args.query.strip()
        ):
            raise ValueError("Search description cannot be empty.")
        if args.min_similarity is not None and not -1 <= args.min_similarity <= 1:
            raise ValueError("--min-similarity must be between -1 and 1.")
        if args.mode not in {"hybrid", "semantic"} and args.min_similarity is not None:
            raise ValueError(
                "--min-similarity only applies to hybrid and semantic (vector) modes."
            )
        if args.mode != "hybrid" and args.rerank is True:
            raise ValueError("--rerank only applies to hybrid mode.")
        config = SearchConfig(
            db=args.db,
            search_db=args.search_db,
            channel=args.channel,
            video_ids=tuple(args.video_ids) if args.video_ids is not None else None,
            model=args.model,
            batch_size=args.batch_size,
            transcript_source=args.transcript_source,
            language=args.language,
            source_mode=args.source_mode,
            device=args.device,
            mode=args.mode,
            rerank=args.rerank,
        )
        with SearchSession(config, on_progress=print_progress) as session:
            if args.interactive and not args.json:
                run_interactive(session, args)
                return
            query = args.query
            while True:
                if query is None:
                    try:
                        print("query> ", file=sys.stderr, end="", flush=True)
                        query = input()
                    except EOFError:
                        break
                    if not query.strip():
                        break
                results = session.search(
                    query,
                    args.top,
                    args.min_similarity,
                    moments_per_video=args.moments_per_video,
                )
                print_results(results, args, session.channel_id, query)
                if not args.interactive:
                    break
                query = None
    except (RuntimeError, ValueError, OSError, sqlite3.Error, ImportError) as exc:
        print(f"Error: {redact(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)


def print_results(results, args, channel_id, query):
    if args.json:
        print(
            json.dumps(
                {
                    "query": query,
                    "channel": channel_id,
                    "video_ids": args.video_ids,
                    "mode": args.mode,
                    "moments_per_video": args.moments_per_video,
                    "model": MODELS[args.model]
                    if args.mode in {"hybrid", "semantic"}
                    else None,
                    "reranker": DEFAULT_RERANKER
                    if (args.mode == "hybrid" and args.rerank is not False)
                    else None,
                    "transcript_source": args.transcript_source,
                    "language": args.language,
                    "source_mode": args.source_mode,
                    "results": results,
                },
                ensure_ascii=False,
                indent=None if args.interactive else 2,
            )
        )
    else:
        if not results:
            print("No matching transcripts found.")
        for rank, result in enumerate(results, 1):
            print(f"\n{rank}. {display_text(result['title'])}")
            print(
                f"Kênh: {display_text(result['channel_title'])} ({result['channel_id']})"
            )
            print(
                f"Transcript: {result['language']} · {result['transcript_source']} / {result['source_mode']}"
                + (
                    f" · {result['transcript_config']}"
                    if result["transcript_config"]
                    else ""
                )
            )
            for index, moment in enumerate(result_moments(result), 1):
                print(
                    f"Đoạn {index} · phút {timestamp(moment['start'])}–{timestamp(moment['end'])}"
                )
                print(f"{moment['url']}\n{display_text(moment['text'])}")


if __name__ == "__main__":
    main()

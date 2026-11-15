import argparse
import os
import sqlite3
import sys

from transcript_collector.config import DATA_DIR, CollectionRequest
from transcript_collector.engines import ENGINE_NAMES
from transcript_collector.errors import SourceError
from yt_workflows.paths import aliases, protected_variants

from .collector_runtime import collect
from .collector_terminal import print_event
from .logging_config import (
    add_logging_arguments,
    command_logging,
    redact,
    validate_logging_arguments,
)


def parser():
    p = argparse.ArgumentParser(
        prog="yt-collect", description="Collect transcript results into SQLite."
    )
    p.add_argument(
        "--db",
        default=str(DATA_DIR / "index.sqlite3"),
        help="collector result database (SQLite)",
    )
    sub = p.add_subparsers(dest="command", required=True)
    collect = sub.add_parser(
        "collect", help="collect transcripts from a video, channel or playlist"
    )
    collect.add_argument(
        "target", help="YouTube video/playlist URL, @handle, channel URL or UC ID"
    )
    collect.add_argument(
        "--db", default=argparse.SUPPRESS, help="collector result database (SQLite)"
    )
    collect.add_argument(
        "--language",
        help="one YouTube caption language, e.g. vi or en (required in direct mode)",
    )
    collect.add_argument(
        "--mode",
        choices=["direct", "generate"],
        required=True,
        help="direct: fetch YouTube captions; generate: download audio and transcribe",
    )
    collect.add_argument(
        "--engine",
        choices=ENGINE_NAMES,
        help="audio transcription engine (generate mode; default: whisper)",
    )
    collect.add_argument(
        "--engine-model", help="engine model (Whisper default: medium)"
    )
    collect.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    collect.add_argument("--cookies", help="Netscape cookies for yt-dlp")
    collect.add_argument("--refresh", action="store_true")
    add_logging_arguments(p)
    add_logging_arguments(collect, inherited=True)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    validate_logging_arguments(args, p)
    with command_logging("collect", args.log_file, args.log_level):
        _run(args)


def _run(args):
    os.environ.setdefault("HF_HOME", str(DATA_DIR / "models"))
    try:
        request = CollectionRequest(
            target=args.target,
            mode=args.mode,
            db=args.db,
            language=args.language,
            engine=args.engine,
            engine_model=args.engine_model,
            device=args.device,
            cookies=args.cookies,
            refresh=args.refresh,
        )
        if args.cookies:
            cookie_variants = protected_variants(args.cookies)
            db_variants = protected_variants(args.db)
            if any(
                aliases(left, right)
                for left in cookie_variants
                for right in db_variants
            ):
                raise ValueError(
                    "The transcript database must be different from the cookies file."
                )
        result = collect(request, on_event=print_event)
        counts = result.counts
        if counts["blocked"]:
            raise SourceError(
                "YouTube blocked collection; saved work and inventory remain. Retry later."
            )
        if counts["errors"] or counts["audio_errors"]:
            raise SourceError("Some videos failed; run again to retry.")
    except (SourceError, RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {redact(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("Interrupted; completed transcripts remain saved.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()

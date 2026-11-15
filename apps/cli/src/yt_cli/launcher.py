"""Launch terminal adapters in the shared repository Python environment."""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

COMMANDS = {
    "collect": ("transcript_collector", "yt_cli.collector"),
    "search": ("video_search", "yt_cli.search"),
}


def repository_root() -> Path:
    configured = os.environ.get("YT_FINDER_ROOT")
    candidates = (
        [Path(configured).expanduser().resolve()]
        if configured
        else Path(__file__).resolve().parents
    )
    for root in candidates:
        if all(
            (root / name / "pyproject.toml").is_file()
            for name in ("transcript_collector", "video_search")
        ):
            return root
    raise RuntimeError(
        "Cannot find the component projects. Set YT_FINDER_ROOT to the repository path."
    )


def run_component(
    command: str, arguments: Sequence[str], *, root: Path | None = None
) -> int:
    """Pass terminal streams and argv through without importing either component."""
    _, module = COMMANDS[command]
    root = root if root is not None else repository_root()
    python = (
        root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not python.is_file():
        raise RuntimeError(
            f"Missing repository environment at {root / '.venv'}. Run uv sync --all-extras from {root} first."
        )
    environment = os.environ.copy()
    source = os.pathsep.join(
        str(root / "apps" / name / "src") for name in ("cli", "workflows")
    )
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
    process = subprocess.Popen([str(python), "-m", module, *arguments], env=environment)
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
        except KeyboardInterrupt:
            process.kill()
            returncode = process.wait()
    return returncode if returncode >= 0 else 128 - returncode


def _launch(command: str, arguments: Sequence[str]) -> None:
    try:
        code = run_component(command, arguments)
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="YouTube transcript CLI application.")
    parser.add_argument("command", choices=list(COMMANDS))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    # The collector adapter preserves the yt-collect collect ... command syntax.
    arguments = (
        ["collect", *args.arguments] if args.command == "collect" else args.arguments
    )
    _launch(args.command, arguments)


def collector_entry() -> None:
    _launch("collect", sys.argv[1:])


def search_entry() -> None:
    _launch("search", sys.argv[1:])

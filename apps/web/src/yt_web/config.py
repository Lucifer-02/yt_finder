"""Local runtime paths using the shared repository Python environment."""

import os
from dataclasses import dataclass
from pathlib import Path


def database_files(path: Path) -> list[Path]:
    bases = {path.expanduser().absolute(), path.expanduser().resolve()}
    return [
        Path(str(base) + suffix)
        for base in bases
        for suffix in ("", "-wal", "-shm", "-journal")
    ]


def same_file(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def find_repository() -> Path:
    configured = os.environ.get("YT_FINDER_ROOT")
    candidates = (
        [Path(configured).expanduser().resolve()]
        if configured
        else Path(__file__).resolve().parents
    )
    for candidate in candidates:
        if all(
            (candidate / component / "pyproject.toml").is_file()
            for component in ("transcript_collector", "video_search")
        ):
            return candidate
    raise ValueError(
        "Set YT_FINDER_ROOT to the repository containing transcript_collector and video_search."
    )


@dataclass(frozen=True)
class WebConfig:
    root: Path
    db: Path
    search_db: Path
    state_db: Path
    log_file: Path | None = None
    log_level: str = "INFO"

    @property
    def log_path(self) -> Path:
        return (
            self.log_file
            or self.state_db.parent / "logs" / f"{self.state_db.stem}.jsonl"
        )

    @property
    def log_files(self) -> list[Path]:
        base = self.log_path.expanduser().resolve()
        return [base, *[Path(f"{base}.{number}") for number in range(1, 4)]]

    @classmethod
    def defaults(cls) -> "WebConfig":
        root = find_repository()
        return cls(
            root,
            root / "data/index.sqlite3",
            root / "data/search.sqlite3",
            root / "data/web.sqlite3",
        )

    def validate(self) -> None:
        groups = [
            database_files(path) for path in (self.db, self.search_db, self.state_db)
        ]
        paths = [path for group in groups for path in group]
        for i, left in enumerate(groups):
            for right in groups[i + 1 :]:
                if any(same_file(a, b) for a in left for b in right):
                    raise ValueError(
                        "Transcript, search cache and web state databases must be different files."
                    )
        for candidate in self.log_files:
            candidate = candidate.expanduser().resolve()
            if any(same_file(candidate, protected) for protected in paths):
                raise ValueError(
                    "Log files must differ from transcript, search cache and web state databases."
                )

    def interpreter(self, component: str) -> Path:
        """Keep component routing while every worker uses the root environment."""
        return (
            self.root
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )

"""File alias checks shared by application adapters without business imports."""

from pathlib import Path

SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def protected_variants(path) -> set[Path]:
    """Include SQLite sidecars at both caller and canonical base pathnames."""
    raw = Path(path).expanduser().absolute()
    return {
        Path(str(base) + suffix).resolve()
        for base in (raw, raw.resolve())
        for suffix in SQLITE_SUFFIXES
    }


def aliases(left: Path, right: Path) -> bool:
    return left == right or (left.exists() and right.exists() and left.samefile(right))

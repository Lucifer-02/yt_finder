"""Validated search application settings, reusable outside the CLI."""

from dataclasses import dataclass
from pathlib import Path

from .filtering import VideoFilters
from .predicate import compile_where

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class SearchConfig:
    db: str = str(DATA_DIR / "index.sqlite3")
    search_db: str = str(DATA_DIR / "search.sqlite3")
    channel: str | None = None
    video_ids: tuple[str, ...] | None = None
    model: str = "minilm"
    batch_size: int = 16
    transcript_source: str = "auto"
    language: str = "vi"
    source_mode: str | None = None
    device: str | None = None
    mode: str = "hybrid"
    rerank: bool | None = None
    where: str | None = None

    @property
    def use_rerank(self) -> bool:
        """Resolve Cross-Encoder reranking default: enabled only for hybrid mode."""
        if self.rerank is None:
            return self.mode == "hybrid"
        return self.rerank

    def filters(self) -> VideoFilters:
        return VideoFilters(
            channel=self.channel,
            video_ids=tuple(self.video_ids) if self.video_ids is not None else None,
            language=self.language,
            transcript_source=self.transcript_source,
            source_mode=self.source_mode,
        )

    def validate(self) -> tuple[Path, Path]:
        if self.where is None:
            self.filters().validate()
        else:
            if (
                self.channel is not None
                or self.video_ids is not None
                or self.source_mode is not None
                or self.language != "vi"
                or self.transcript_source != "auto"
            ):
                raise ValueError(
                    "WHERE cannot be combined with legacy channel, video IDs, language, source or source-mode filters."
                )
            compile_where(self.where)
        if self.mode not in {"hybrid", "semantic", "keyword", "exact"}:
            raise ValueError("Choose hybrid, semantic, keyword, or exact search mode.")
        if self.mode != "hybrid" and self.rerank is True:
            raise ValueError("Reranking is only available for hybrid search.")
        if self.batch_size < 1:
            raise ValueError("Batch size must be greater than zero.")
        if not self.model.strip():
            raise ValueError("Search model cannot be empty.")
        if self.device not in {None, "cpu", "cuda"}:
            raise ValueError("Choose cpu or cuda as device.")
        source = Path(self.db).expanduser().resolve()
        cache = Path(self.search_db).expanduser().resolve()
        source_files = [
            Path(str(base) + suffix)
            for base in {source, Path(self.db).expanduser().absolute()}
            for suffix in ("", "-wal", "-shm", "-journal")
        ]
        cache_files = [
            Path(str(base) + suffix)
            for base in {cache, Path(self.search_db).expanduser().absolute()}
            for suffix in ("", "-wal", "-shm", "-journal")
        ]
        if any(
            left == right or (left.exists() and right.exists() and left.samefile(right))
            for left in source_files
            for right in cache_files
        ):
            raise ValueError(
                "The vector cache must be different from the transcript database."
            )
        return source, cache

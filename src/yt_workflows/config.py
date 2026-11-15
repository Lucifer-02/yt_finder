"""Transport-neutral requests; importing these does not load business dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class EvaluationRequest:
    db: str = str(DATA_DIR / "index.sqlite3")
    search_db: str = str(DATA_DIR / "evaluation/search.sqlite3")
    language: str = "vi"
    transcript_source: str = "auto"
    source_mode: str | None = None
    dataset: str = "evaluation/vi_queries.jsonl"
    models: tuple[str, ...] = ("minilm", "e5-small", "qwen3", "jina-v5")
    max_windows: int = 512
    batch_size: int = 4
    top: int = 5
    device: str = "cpu"
    rerank: bool = False
    rerank_top: int = 40
    min_similarity: float | None = None
    offline: bool = False
    output: str = str(DATA_DIR / "evaluation/report.json")
    corpus_file: str | None = None

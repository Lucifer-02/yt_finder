"""Embedding progress independent of any terminal or web interface."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingProgress:
    completed: int
    total: int


ProgressHandler = Callable[[EmbeddingProgress], None]

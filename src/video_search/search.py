"""Hybrid (RRF + Cross-Encoder), vector/semantic, keyword (BM25), and exact retrieval."""

import logging
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from time import perf_counter
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

import numpy as np

from .cache import EmbeddingStore, lexical_signature
from .documents import VideoTranscript, Window, video_windows
from .events import EmbeddingProgress, ProgressHandler
from .models import (
    DEFAULT_MODEL,
    DEFAULT_RERANKER,
    RetrievalModel,
    split_texts,
    text_hash,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from sentence_transformers.base.modality_types import PairInput


class SearchMoment(TypedDict):
    start: float
    end: float
    text: str
    score: float
    similarity: float | None
    url: str
    rerank_score: NotRequired[float]


class SearchResult(Window):
    # Keep the best moment's fields for existing consumers and evaluation metrics.
    score: float
    similarity: float | None
    url: str
    rerank_score: NotRequired[float]
    moments: NotRequired[list[SearchMoment]]


def result_moments(result: SearchResult) -> list[SearchMoment]:
    """Read grouped results, including older single-moment adapters."""
    return result.get("moments", [result])


def embedding_text(passage: Mapping[str, Any]) -> str:
    """Include video title context alongside transcript text without mutating stored text."""
    title = str(passage.get("title") or "").strip()
    text = passage["text"]
    if not title:
        return text
    return f"{title}\n{text}" if text else title


DENSE_MODES = frozenset({"hybrid", "semantic"})
LEXICAL_MODES = frozenset({"keyword", "exact"})
ALL_MODES = DENSE_MODES | LEXICAL_MODES


def _normalize_exact(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def exact_scores(query: str, passages: Sequence[Window]) -> np.ndarray:
    """Case-insensitive verbatim substring score preserving diacritics and punctuation."""
    needle = _normalize_exact(query.strip())
    scores = np.zeros(len(passages), dtype=np.float32)
    if not needle or not passages:
        return scores
    for index, passage in enumerate(passages):
        text_hits = _normalize_exact(passage["text"]).count(needle)
        title_hit = 1 if needle in _normalize_exact(passage.get("title", "")) else 0
        if text_hits or title_hit:
            scores[index] = float(text_hits) + (0.5 if title_hit else 0.0)
    return scores


def lexical_scores(query: str, passages: list[Window]) -> list[float]:
    cache = EmbeddingStore(":memory:")
    try:
        return cache.lexical_scores(query, passages).tolist()
    finally:
        cache.close()


class SearchEngine:
    """Keep the model and matrix alive across interactive/evaluation queries."""

    def __init__(
        self,
        cache: EmbeddingStore,
        model_name: str = DEFAULT_MODEL,
        batch_size: int = 16,
        device: str | None = None,
        reranker_name: str | None = None,
        *,
        mode: str = "hybrid",
        on_progress: ProgressHandler | None = None,
    ):
        if batch_size < 1:
            raise ValueError("Batch size must be greater than zero.")
        if mode not in ALL_MODES:
            raise ValueError("Unknown search mode.")
        if mode != "hybrid" and reranker_name:
            raise ValueError("Reranking is only available for hybrid search.")
        self.mode = mode
        self.cache = cache
        self.batch_size = batch_size
        self.device, self.reranker_name = device, reranker_name
        self.model_name = model_name
        self.on_progress = on_progress
        self.model = None
        self.reranker = None
        self._signature: tuple[str, bool, str | None] | None = None
        self._videos: tuple[VideoTranscript, ...] | None = None
        self._lexical_signature = None
        self.passages: list[Window] = []
        self.matrix = np.empty((0, 0), dtype=np.float32)
        self.owners = np.empty(0, dtype=np.intp)

    def prepare(self, videos: Sequence[VideoTranscript]) -> None:
        """Build windows only from the supplied immutable video records."""
        snapshot = tuple(videos)
        if snapshot != self._videos:
            self._videos = snapshot
            self.prepare_windows(video_windows(snapshot))
        else:
            self.prepare_windows(self.passages)

    def prepare_windows(self, passages: Sequence[Window]) -> None:
        """Use already frozen windows for evaluation without rebuilding their boundaries."""
        started = perf_counter()
        # Repeated input records must not duplicate FTS rows or affect ranking scores.
        passages = list({lexical_signature((p,)): p for p in passages}.values())
        version = lexical_signature(passages)
        is_dense = self.mode in DENSE_MODES
        signature = (version, is_dense, self.model_name if is_dense else None)
        if signature == self._signature:
            logger.debug(
                "Prepared windows reused: windows=%d",
                len(self.passages),
                extra={"phase": "prepare_reuse"},
            )
            return
        self._videos = None
        if not passages:
            logger.info(
                "Corpus has no matching windows", extra={"phase": "prepare_empty"}
            )
            self.passages = []
            self.matrix = np.empty((0, 0), dtype=np.float32)
            self.owners = np.empty(0, dtype=np.intp)
            self._signature = signature
            return
        if self.mode in LEXICAL_MODES:
            self.passages = passages
            self._lexical_signature = version if self.mode == "keyword" else None
            self._signature = signature
            return
        if self.model is None or getattr(self.model, "name", None) != self.model_name:
            self.model = RetrievalModel(self.model_name, self.device)
        model = self.model
        embed_inputs = [embedding_text(p) for p in passages]
        keys = [text_hash(text) for text in embed_inputs]
        splits = self.cache.splits(list(dict.fromkeys(keys)), model.split_config)
        missing = {
            key: text for key, text in zip(keys, embed_inputs) if key not in splits
        }
        logger.info(
            "Token split cache: hits=%d misses=%d",
            len(splits),
            len(missing),
            extra={"phase": "split_cache"},
        )
        pending = list(missing)
        for offset in range(0, len(pending), 256):
            batch = pending[offset : offset + 256]
            chunks = model.split([missing[key] for key in batch])
            self.cache.save_splits(list(zip(batch, chunks)), model.split_config)
            splits.update(zip(batch, chunks))
            logger.debug(
                "Token split batch saved: completed=%d total=%d chunks=%d",
                offset + len(batch),
                len(pending),
                sum(map(len, chunks)),
                extra={"phase": "split_batch"},
            )
        chunk_texts: dict[str, str] = {}
        chunk_keys = []
        owners = []
        for i, key in enumerate(keys):
            for text in splits[key]:
                chunk_key = text_hash(text)
                chunk_texts[chunk_key] = text
                chunk_keys.append(chunk_key)
                owners.append(i)
        vectors = self.cache.vectors(list(chunk_texts), model.name, model.config)
        absent = [key for key in chunk_texts if key not in vectors]
        logger.info(
            "Embedding cache: hits=%d misses=%d chunks=%d windows=%d",
            len(vectors),
            len(absent),
            len(chunk_keys),
            len(passages),
            extra={"phase": "embedding_cache"},
        )
        embedding_started = perf_counter()
        for offset in range(0, len(absent), 256):
            batch = absent[offset : offset + 256]
            encoded = model.encode([chunk_texts[key] for key in batch], self.batch_size)
            self.cache.save_vectors(zip(batch, encoded), model.name, model.config)
            vectors.update(zip(batch, encoded))
            logger.info(
                "Embedding progress: completed=%d total=%d elapsed_seconds=%.3f",
                offset + len(batch),
                len(absent),
                perf_counter() - embedding_started,
                extra={
                    "phase": "embedding_progress",
                    "completed": offset + len(batch),
                    "total": len(absent),
                },
            )
            if self.on_progress is not None:
                self.on_progress(EmbeddingProgress(offset + len(batch), len(absent)))
        self.matrix = np.stack([vectors[key] for key in chunk_keys])
        self.owners = np.asarray(owners, dtype=np.intp)
        self.passages = passages
        self._lexical_signature = version
        self._signature = signature
        logger.info(
            "Corpus prepared: windows=%d chunks=%d new_embeddings=%d elapsed_seconds=%.3f",
            len(passages),
            len(chunk_keys),
            len(absent),
            perf_counter() - started,
            extra={"phase": "prepare_ready"},
        )

    def search(
        self,
        query: str,
        videos: Sequence[VideoTranscript],
        top: int = 5,
        rerank_top: int = 40,
        min_similarity: float | None = None,
        *,
        moments_per_video: int = 3,
    ) -> list[SearchResult]:
        return self._run_query(
            query,
            top,
            rerank_top,
            min_similarity,
            lambda: self.prepare(videos),
            moments_per_video,
        )

    def search_windows(
        self,
        query: str,
        passages: Sequence[Window],
        top: int = 5,
        rerank_top: int = 40,
        min_similarity: float | None = None,
        *,
        moments_per_video: int = 1,
    ) -> list[SearchResult]:
        """Evaluate the supplied snapshot; default metrics still use one moment/video."""
        return self._run_query(
            query,
            top,
            rerank_top,
            min_similarity,
            lambda: self.prepare_windows(passages),
            moments_per_video,
        )

    def _run_query(
        self,
        query: str,
        top: int,
        rerank_top: int,
        min_similarity: float | None,
        prepare: Callable[[], None],
        moments_per_video: int,
    ) -> list[SearchResult]:
        started = perf_counter()
        logger.info(
            "Search started: query_length=%d top=%d moments_per_video=%d rerank=%s model=%s",
            len(query),
            top,
            moments_per_video,
            bool(self.reranker_name),
            self.model_name,
            extra={"phase": "query_start"},
        )
        try:
            results = self._search(
                query, top, rerank_top, min_similarity, prepare, moments_per_video
            )
        except BaseException as error:
            # Exception messages from model providers can include user text.
            logger.error(
                "Search failed: error_type=%s elapsed_seconds=%.3f",
                type(error).__name__,
                perf_counter() - started,
                extra={"phase": "query_failed"},
            )
            raise
        logger.info(
            "Search complete: results=%d elapsed_seconds=%.3f",
            len(results),
            perf_counter() - started,
            extra={"phase": "query_ready", "total": len(results)},
        )
        return results

    def _search(
        self,
        query: str,
        top: int,
        rerank_top: int,
        min_similarity: float | None,
        prepare: Callable[[], None],
        moments_per_video: int,
    ) -> list[SearchResult]:
        if not query.strip() or top < 1 or rerank_top < 1 or moments_per_video < 1:
            raise ValueError(
                "Query must be nonempty and result limits must be positive."
            )
        if min_similarity is not None and not -1 <= min_similarity <= 1:
            raise ValueError("Minimum similarity must be between -1 and 1.")
        if self.mode in LEXICAL_MODES and min_similarity is not None:
            raise ValueError(
                "Minimum similarity only applies to hybrid and vector/semantic search."
            )
        prepare()
        if not self.passages:
            return []
        similarity = np.full(len(self.passages), np.nan, dtype=np.float32)
        if self.mode in DENSE_MODES:
            if self.model is None:
                raise RuntimeError("Embedding model was not initialized.")
            semantic_started = perf_counter()
            query_vector = self.model.encode([query], self.batch_size, query=True)[0]
            if self.matrix.shape[1] != len(query_vector):
                raise ValueError(
                    "Cached vector dimensions differ from the current model."
                )
            similarity = np.full(len(self.passages), -np.inf, dtype=np.float32)
            np.maximum.at(similarity, self.owners, self.matrix @ query_vector)
            logger.info(
                "Semantic retrieval complete: windows=%d elapsed_seconds=%.3f",
                len(similarity),
                perf_counter() - semantic_started,
                extra={"phase": "semantic_ready"},
            )
        if self.mode == "semantic":
            scores = similarity
            order = [
                int(i)
                for i in np.argsort(-scores, kind="stable")
                if min_similarity is None or similarity[i] >= min_similarity
            ]
        elif self.mode == "exact":
            match_started = perf_counter()
            scores = exact_scores(query, self.passages)
            order = [
                int(i) for i in np.argsort(-scores, kind="stable") if scores[i] > 0
            ]
            logger.info(
                "Exact retrieval complete: matches=%d elapsed_seconds=%.3f",
                len(order),
                perf_counter() - match_started,
                extra={"phase": "exact_ready"},
            )
        else:
            lexical = self.cache.lexical_scores(
                query,
                self.passages,
                self._lexical_signature,
                keyword_query=self.mode == "keyword",
            )
            if self.mode == "keyword":
                scores = lexical
                order = [
                    int(i) for i in np.argsort(-scores, kind="stable") if scores[i] > 0
                ]
            else:
                scores = np.zeros(len(similarity), dtype=np.float64)
                dense_order = np.argsort(-similarity, kind="stable")
                scores[dense_order] += 1 / (61 + np.arange(len(dense_order)))
                lexical_order = np.argsort(-lexical, kind="stable")
                lexical_order = lexical_order[lexical[lexical_order] > 0]
                scores[lexical_order] += 1 / (61 + np.arange(len(lexical_order)))
                order = [
                    int(i)
                    for i in np.argsort(-scores, kind="stable")
                    if min_similarity is None or similarity[i] >= min_similarity
                ]
                logger.info(
                    "RRF complete: candidates=%d",
                    len(order),
                    extra={"phase": "rrf_ready"},
                )
        order, rerank_scores = self._rerank(
            query, order, scores, top, rerank_top, moments_per_video
        )
        # Visit all ranked candidates: --top limits videos, not the total number of moments.
        grouped: dict[str, SearchResult] = {}
        for i in order:
            p = self.passages[i]
            video_id = p["video_id"]
            existing = grouped.get(video_id)
            if existing is None and len(grouped) >= top:
                continue
            selected = result_moments(existing) if existing is not None else []
            if len(selected) >= moments_per_video:
                continue
            # Greedy suppression keeps the highest-ranked window in each overlapping region.
            # Windows that only touch at an endpoint represent separate moments.
            if any(
                max(p["start"], moment["start"]) < min(p["end"], moment["end"])
                for moment in selected
            ):
                continue
            moment: SearchMoment = {
                "start": p["start"],
                "end": p["end"],
                "text": p["text"],
                "score": round(float(scores[i]), 6),
                "similarity": None
                if self.mode in LEXICAL_MODES
                else round(float(similarity[i]), 4),
                "url": f"https://www.youtube.com/watch?v={video_id}&t={int(p['start'])}s",
            }
            if i in rerank_scores:
                moment["rerank_score"] = round(rerank_scores[i], 6)
            if existing is None:
                item: SearchResult = {
                    **p,
                    "score": moment["score"],
                    "similarity": moment["similarity"],
                    "url": moment["url"],
                    "moments": [moment],
                }
                if "rerank_score" in moment:
                    item["rerank_score"] = moment["rerank_score"]
                grouped[video_id] = item
            else:
                existing["moments"].append(moment)
            if len(grouped) == top and all(
                len(video["moments"]) >= moments_per_video for video in grouped.values()
            ):
                break
        return list(grouped.values())

    def _rerank(
        self,
        query: str,
        order: list[int],
        scores: np.ndarray,
        top: int,
        rerank_top: int,
        moments_per_video: int,
    ) -> tuple[list[int], dict[int, float]]:
        """Rerank diverse video candidates, pooling scores over token-sized chunks."""
        if not self.reranker_name or not order:
            return order, {}
        rerank_started = perf_counter()
        if self.reranker is None:
            from sentence_transformers import CrossEncoder

            logger.info(
                "Loading reranker: model=%s device=%s",
                self.reranker_name,
                self.device or "auto",
                extra={"phase": "reranker_load"},
            )
            self.reranker = CrossEncoder(
                self.reranker_name, device=self.device, max_length=512
            )
        candidates = []
        counts: dict[str, int] = {}
        candidate_windows: dict[str, list[Window]] = {}
        for i in order:
            p = self.passages[i]
            video = p["video_id"]
            if video not in counts:
                candidates.append(i)
                counts[video] = 1
                candidate_windows[video] = [p]
            if len(candidates) >= top:
                break
        for i in order:
            if len(candidates) >= max(top, rerank_top):
                break
            p = self.passages[i]
            video = p["video_id"]
            if (
                counts.get(video, 0) < max(3, moments_per_video)
                and i not in candidates
                and not any(
                    max(p["start"], prev["start"]) < min(p["end"], prev["end"])
                    for prev in candidate_windows.get(video, [])
                )
            ):
                candidates.append(i)
                counts[video] = counts.get(video, 0) + 1
                candidate_windows.setdefault(video, []).append(p)
        tokenizer = self.reranker.tokenizer
        reserved = len(tokenizer.encode(query, add_special_tokens=False))
        reserved += tokenizer.num_special_tokens_to_add(
            pair=True
        ) - tokenizer.num_special_tokens_to_add(pair=False)
        chunks = split_texts(
            tokenizer,
            [embedding_text(self.passages[i]) for i in candidates],
            512,
            reserved_tokens=reserved,
        )
        pairs: list[PairInput] = [[query, text] for group in chunks for text in group]
        owners = np.repeat(np.arange(len(chunks)), [len(group) for group in chunks])
        predicted = np.asarray(
            self.reranker.predict(
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
            )
        ).reshape(-1)
        if len(predicted) != len(pairs) or not np.isfinite(predicted).all():
            raise RuntimeError("Reranker did not return all requested finite scores.")
        pooled = np.full(len(candidates), -np.inf)
        np.maximum.at(pooled, owners, predicted)
        rerank_scores = dict(zip(candidates, pooled.tolist()))
        cand_set = set(candidates)
        order = sorted(
            candidates, key=lambda i: (-rerank_scores[i], -scores[i], i)
        ) + [i for i in order if i not in cand_set]
        logger.info(
            "Reranking complete: candidates=%d pairs=%d elapsed_seconds=%.3f",
            len(candidates),
            len(pairs),
            perf_counter() - rerank_started,
            extra={"phase": "rerank_ready"},
        )
        return order, rerank_scores


def search(
    videos: Sequence[VideoTranscript],
    cache: EmbeddingStore,
    query: str,
    top: int = 5,
    model_name: str = DEFAULT_MODEL,
    batch_size: int = 16,
    *,
    device: str | None = None,
    mode: str = "hybrid",
    rerank: bool | None = None,
    min_similarity: float | None = None,
    moments_per_video: int = 3,
) -> list[SearchResult]:
    """One-shot ranking of selected videos; no transcript database is accessed."""
    use_rerank = mode == "hybrid" if rerank is None else rerank
    engine = SearchEngine(
        cache,
        model_name=model_name,
        batch_size=batch_size,
        device=device,
        reranker_name=DEFAULT_RERANKER if use_rerank else None,
        mode=mode,
    )
    return engine.search(
        query,
        videos,
        top=top,
        min_similarity=min_similarity,
        moments_per_video=moments_per_video,
    )

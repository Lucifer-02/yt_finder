"""Synchronous evaluation algorithms and orchestration shared by CLI and web."""

import hashlib
import importlib.metadata
import json
import logging
import math
import os
import resource
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
from video_search.cache import EmbeddingStore
from video_search.corpus import Corpus, Window, validate_filters
from video_search.models import (
    DEFAULT_RERANKER,
    MODELS,
)
from video_search.search import SearchEngine

from .config import EvaluationRequest, ProgressCallback
from .paths import aliases, protected_variants

logger = logging.getLogger(__name__)


def emit(callback: ProgressCallback | None, phase: str, **fields: Any) -> None:
    logger.info("Workflow progress: %s", phase, extra={"phase": phase, **fields})
    if callback is not None:
        callback({"phase": phase, **fields})


def known_match(result, judgment, moment=False):
    if result["video_id"] != judgment["video_id"]:
        return False
    if judgment.get("source") and result["transcript_source"] != judgment["source"]:
        return False
    if (
        judgment.get("source_mode")
        and result.get("source_mode") != judgment["source_mode"]
    ):
        return False
    if judgment.get("language") and result.get("language") != judgment["language"]:
        return False
    if not moment:
        return True
    overlap = min(result["end"], judgment["end"]) - max(
        result["start"], judgment["start"]
    )
    return overlap >= min(10, (judgment["end"] - judgment["start"]) / 2)


def metrics(rows, predictions, top=5):
    positives = []
    negatives = []
    for row, results in zip(rows, predictions):
        gold = row["relevant"]
        if not gold:
            negatives.append(bool(results))
            continue
        hits = [any(known_match(r, j) for j in gold) for r in results[:top]]
        moments = [any(known_match(r, j, True) for j in gold) for r in results[:top]]
        rank = next((i + 1 for i, hit in enumerate(hits) if hit), None)
        dcg = sum(int(hit) / math.log2(i + 2) for i, hit in enumerate(hits))
        ideal = sum(
            1 / math.log2(i + 2)
            for i in range(min(top, len({j["video_id"] for j in gold})))
        )
        positives.append(
            [
                float(any(hits)),
                1 / rank if rank else 0,
                dcg / ideal,
                float(any(moments)),
            ]
        )
    avg = np.mean(positives, axis=0).tolist() if positives else [None] * 4
    return {
        "positive_queries": len(positives),
        "negative_queries": len(negatives),
        f"known_video_hit@{top}": avg[0],
        f"mrr@{top}": avg[1],
        f"ndcg@{top}": avg[2],
        f"known_moment_hit@{top}": avg[3],
        "negative_false_return_rate": float(np.mean(negatives)) if negatives else None,
    }


def select_windows(windows: list[Window], rows, limit: int) -> list[Window]:
    """Include gold moments and deterministic distractors, independently of models."""
    if limit == 0 or len(windows) <= limit:
        return windows
    selected = set()
    for row in rows:
        for gold in row["relevant"]:
            matches = [i for i, w in enumerate(windows) if known_match(w, gold, True)]
            if not matches:
                raise ValueError(f"No stored transcript covers judgment {row['id']}.")
            selected.update(matches)
    if len(selected) > limit:
        raise ValueError("Window limit is too small to include the labeled moments.")
    if len(selected) == limit:
        return [windows[i] for i in sorted(selected)]
    candidates = np.random.default_rng(42).permutation(len(windows))
    for candidate in candidates:
        selected.add(candidate)
        if len(selected) >= limit:
            break
    return [windows[i] for i in sorted(selected)]


def validate_output_paths(read_paths, write_paths):
    """Reject aliases before opening any cache, report or corpus snapshot for writing."""
    protected_paths = {
        variant for path in read_paths for variant in protected_variants(path)
    }
    for writable in write_paths:
        targets = protected_variants(writable)
        if any(
            aliases(source, target) for source in protected_paths for target in targets
        ):
            raise ValueError(
                f"Output aliases a read-only input or SQLite sidecar: {Path(writable).expanduser().resolve()}"
            )
        protected_paths.update(targets)


def validate_evidence(rows, windows):
    """Reject stale reference excerpts; overlap windows may repeat parts of the text."""
    grouped: dict[tuple[str, str, str, str], list[Window]] = {}
    for w in windows:
        key = (w["video_id"], w["transcript_source"], w["language"], w["source_mode"])
        grouped.setdefault(key, []).append(w)
    for row in rows:
        for gold in row["relevant"]:
            evidence = " ".join(gold.get("evidence", "").split())
            if not evidence:
                continue
            candidates = [
                " ".join(" ".join(w["text"] for w in group).split())
                for group in grouped.values()
                if known_match(group[0], gold)
            ]
            if not any(
                all(evidence[i : i + 80] in text for i in range(0, len(evidence), 40))
                for text in candidates
            ):
                raise ValueError(
                    f"Transcript evidence changed for {row['id']}; review its labels."
                )


class FixedCorpus(Corpus):
    def __init__(self, path: str, passages: list[Window]):
        if any(
            p.get("transcript_source") not in {"youtube", "generate"} for p in passages
        ):
            raise ValueError(
                "Corpus snapshots only support YouTube and generated transcripts."
            )
        if any(
            not {"language", "source_mode", "transcript_config"} <= p.keys()
            for p in passages
        ):
            raise ValueError(
                "Corpus snapshot lacks schema v2 transcript metadata; recreate the snapshot."
            )
        for passage in passages:
            validate_filters(
                passage["transcript_source"],
                passage["language"],
                passage["source_mode"],
            )
        super().__init__(path)
        self.passages = passages

    def windows(
        self, channel_id=None, source="auto", language="vi", source_mode=None
    ) -> list[Window]:
        return self.passages

    def revision(self) -> int:
        return 0


def run_model(args, on_progress=None):
    import torch

    rows = [
        json.loads(line)
        for line in Path(args.dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    passages = [
        json.loads(line)
        for line in Path(args.corpus_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    FixedCorpus(args.db, passages).close()
    with closing(EmbeddingStore(args.search_db)) as cache:
        start = time.perf_counter()
        use_cuda = args.device == "cuda" and torch.cuda.is_available()
        if use_cuda:
            torch.cuda.reset_peak_memory_stats()
        engine = SearchEngine(
            cache,
            model_name=MODELS[args.worker],
            mode="hybrid",
            batch_size=args.batch_size,
            device=args.device,
            reranker_name=DEFAULT_RERANKER if args.rerank else None,
        )
        engine.prepare_windows(passages)
        search_query = partial(engine.search_windows, passages=passages)
        preparation = time.perf_counter() - start
        # Warm model kernels, reranker and FTS before recording interactive latency.
        search_query(
            rows[0]["query"],
            top=args.top,
            rerank_top=args.rerank_top,
            min_similarity=args.min_similarity,
        )
        latencies, predictions = [], []
        for i, row in enumerate(rows):
            t = time.perf_counter()
            predictions.append(
                search_query(
                    row["query"],
                    top=args.top,
                    rerank_top=args.rerank_top,
                    min_similarity=args.min_similarity,
                )
            )
            latencies.append(time.perf_counter() - t)
            if (i + 1) % 10 == 0:
                emit(
                    on_progress,
                    "evaluation_queries",
                    model=args.worker,
                    completed=i + 1,
                    total=len(rows),
                )
        output = {
            "model": args.worker,
            "embedding_model": engine.model.name if engine.model else None,
            "embedding_config": json.loads(engine.model.config)
            if engine.model
            else None,
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("sentence-transformers", "transformers", "torch", "numpy")
            },
            "reranker": DEFAULT_RERANKER if args.rerank else None,
            "device": args.device,
            "batch_size": args.batch_size,
            "language": args.language,
            "transcript_source": args.transcript_source,
            "source_mode": args.source_mode,
            "min_similarity": args.min_similarity,
            "rerank_top": args.rerank_top if args.rerank else None,
            "windows": len(passages),
            "videos": len({p["video_id"] for p in passages}),
            "corpus_sha256": hashlib.sha256(
                Path(args.corpus_file).read_bytes()
            ).hexdigest(),
            "dataset_sha256": hashlib.sha256(
                Path(args.dataset).read_bytes()
            ).hexdigest(),
            "preparation_seconds": preparation,
            "latency_p50_seconds": float(np.median(latencies)),
            "latency_p95_seconds": float(np.percentile(latencies, 95)),
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "peak_torch_vram_mib": torch.cuda.max_memory_allocated() / 2**20
            if use_cuda
            else None,
            "metrics": metrics(rows, predictions, args.top),
            "queries": [
                {"id": r["id"], "query": r["query"], "seconds": t, "results": p}
                for r, t, p in zip(rows, latencies, predictions)
            ],
        }
        Path(args.output).write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return output


def _validate(request: EvaluationRequest) -> None:
    validate_filters(request.transcript_source, request.language, request.source_mode)
    if request.max_windows < 0:
        raise ValueError("max_windows must be nonnegative")
    if request.min_similarity is not None and not -1 <= request.min_similarity <= 1:
        raise ValueError("min_similarity must be between -1 and 1")
    if min(request.batch_size, request.top, request.rerank_top) < 1:
        raise ValueError("Batch size, top and rerank top must be positive.")
    if request.device not in {"cpu", "cuda"}:
        raise ValueError("Choose cpu or cuda as device.")
    if not request.models or len(set(request.models)) != len(request.models):
        raise ValueError("Choose one or more distinct evaluation models.")
    if any(name not in MODELS for name in request.models):
        raise ValueError("Unknown evaluation model.")


def run_isolated_model(
    request: EvaluationRequest,
    model: str,
    snapshot: Path,
    target: Path,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Keep each model in an isolated process, retaining the caller's process group."""
    environment = os.environ.copy()
    if request.offline:
        environment["HF_HUB_OFFLINE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "yt_workflows.eval_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    result = None
    try:
        assert process.stdin is not None and process.stdout is not None
        payload = {
            "request": asdict(request),
            "model": model,
            "snapshot": str(snapshot),
            "target": str(target),
        }
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.close()
        except OSError:
            pass
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "Evaluation worker emitted invalid JSON",
                    extra={"phase": "evaluation_worker_protocol_error", "model": model},
                )
                break
            if event.get("type") == "progress" and on_progress is not None:
                on_progress(event["event"])
            elif event.get("type") == "log":
                record = event["record"]
                logging.getLogger(record["logger"]).log(
                    record["level"],
                    record["message"],
                    extra=record.get("context", {}),
                )
            elif event.get("type") == "result":
                result = event["result"]
        code = process.wait()
        if code or result is None:
            return {"model": model, "status": "failed", "returncode": code}
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


def run_evaluation(
    request: EvaluationRequest,
    on_progress: ProgressCallback | None = None,
    *,
    runner=None,
) -> dict[str, Any]:
    """Evaluate all requested models against one immutable window snapshot."""
    _validate(request)
    output = Path(request.output).expanduser().resolve()
    plans = list(request.models)
    targets = [output.with_name(output.stem + "-" + name + ".json") for name in plans]
    snapshot = output.with_suffix(".corpus.jsonl")
    validate_output_paths(
        [request.db, request.dataset]
        + ([request.corpus_file] if request.corpus_file else []),
        [request.search_db, request.output, *targets]
        + ([] if request.corpus_file else [snapshot]),
    )
    request = replace(
        request,
        db=str(Path(request.db).expanduser().resolve()),
        search_db=str(Path(request.search_db).expanduser().resolve()),
        dataset=str(Path(request.dataset).expanduser().resolve()),
        output=str(output),
        corpus_file=str(Path(request.corpus_file).expanduser().resolve())
        if request.corpus_file
        else None,
    )
    rows = [
        json.loads(line)
        for line in Path(request.dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("Evaluation dataset is empty.")
    emit(on_progress, "evaluation_snapshot_start")
    if request.corpus_file:
        snapshot = Path(request.corpus_file).expanduser().resolve()
        windows = [
            json.loads(line)
            for line in snapshot.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        with_snapshot = FixedCorpus(request.db, windows)
        with_snapshot.close()
        for passage in windows:
            if (
                request.transcript_source != "auto"
                and passage["transcript_source"] != request.transcript_source
                or request.source_mode is not None
                and passage["source_mode"] != request.source_mode
                or not (
                    passage["language"].casefold() == request.language.casefold()
                    or passage["language"]
                    .casefold()
                    .startswith(request.language.casefold() + "-")
                )
            ):
                raise ValueError(
                    "Existing corpus snapshot does not match the requested transcript filters; recreate it or use matching filters."
                )
        selected = windows
    else:
        corpus = Corpus(request.db)
        try:
            windows = corpus.windows(
                source=request.transcript_source,
                language=request.language,
                source_mode=request.source_mode,
            )
        finally:
            corpus.close()
        selected = select_windows(windows, rows, request.max_windows)
    if not selected:
        raise ValueError("Evaluation corpus is empty.")
    validate_evidence(rows, selected)
    for row in rows:
        for gold in row["relevant"]:
            if not any(known_match(window, gold, True) for window in selected):
                raise ValueError(f"Snapshot does not cover labeled moment {row['id']}.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not request.corpus_file:
        snapshot.write_text(
            "".join(
                json.dumps(window, ensure_ascii=False) + "\n" for window in selected
            ),
            encoding="utf-8",
        )
    emit(
        on_progress,
        "evaluation_snapshot_ready",
        total=len(selected),
        query_count=len(rows),
        full_corpus_windows=len(windows),
    )
    reports = []
    run = runner or run_isolated_model
    for completed, (model, target) in enumerate(zip(plans, targets)):
        emit(
            on_progress,
            "evaluation_model_start",
            model=model,
            completed=completed,
            total=len(plans),
        )
        reports.append(run(request, model, snapshot, target, on_progress))
        emit(
            on_progress,
            "evaluation_model_complete",
            model=model,
            completed=completed + 1,
            total=len(plans),
        )
    report = {
        "sampled": None if request.corpus_file else len(selected) < len(windows),
        "full_corpus_windows": None if request.corpus_file else len(windows),
        "judgments": "Agent-authored partial seed labels; not independent human evaluation.",
        "runs": reports,
    }
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    emit(on_progress, "evaluation_complete", completed=len(plans), total=len(plans))
    return report

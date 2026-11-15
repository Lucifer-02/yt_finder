"""Model-specific retrieval prompts and token-aware splitting without lost tails."""

import hashlib
import json
import logging
from time import perf_counter

import numpy as np

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODELS = {
    "minilm": DEFAULT_MODEL,
    "e5-small": "intfloat/multilingual-e5-small",
    "qwen3": "Qwen/Qwen3-Embedding-0.6B",
    "jina-v5": "jinaai/jina-embeddings-v5-text-small-retrieval",
    "embeddinggemma": "google/embeddinggemma-300m",
}
DEFAULT_RERANKER = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


class RetrievalModel:
    def __init__(self, name: str, device: str | None = None):
        from sentence_transformers import SentenceTransformer

        started = perf_counter()
        self.name = MODELS.get(name, name)
        logger.info(
            "Loading embedding model and tokenizer: model=%s device=%s",
            self.name,
            device or "auto",
            extra={"phase": "model_load", "model": self.name},
        )
        self.model = SentenceTransformer(self.name, device=device)
        self.tokenizer = self.model.tokenizer
        # Bound inference cost on modest hardware, including long-context models.
        sequence_length = self.model.max_seq_length
        if not isinstance(sequence_length, int) or sequence_length < 16:
            raise ValueError("Embedding model must declare a valid token limit.")
        self.limit = min(sequence_length, 512)
        self.model.max_seq_length = self.limit
        prompts = self.model.prompts
        if "multilingual-e5" in self.name:
            self.document_prompt, self.query_prompt = "passage: ", "query: "
        else:
            self.document_prompt = prompts.get("document", "") or ""
            self.query_prompt = prompts.get("query", "") or ""
        self.config = json.dumps(
            {
                "version": 3,
                "limit": self.limit,
                "document_prompt": self.document_prompt,
                "query_prompt": self.query_prompt,
                "revision": getattr(
                    getattr(self.model[0], "auto_model").config, "_commit_hash", None
                ),
            },
            sort_keys=True,
        )
        self.split_config = self.name + ":" + self.config
        logger.info(
            "Embedding model ready: model=%s device=%s token_limit=%d elapsed_seconds=%.3f",
            self.name,
            str(self.model.device),
            self.limit,
            perf_counter() - started,
            extra={"phase": "model_ready", "model": self.name},
        )

    def split(self, texts: list[str]) -> list[list[str]]:
        return split_texts(self.tokenizer, texts, self.limit, self.document_prompt)

    def encode(self, texts: list[str], batch_size: int, *, query: bool = False):
        started = perf_counter()
        logger.debug(
            "Encoding batch: count=%d batch_size=%d query=%s model=%s",
            len(texts),
            batch_size,
            query,
            self.name,
            extra={"phase": "encode_batch"},
        )
        prompt = self.query_prompt if query else self.document_prompt
        if query and any(
            len(self.tokenizer.encode(prompt + t, verbose=False)) > self.limit
            for t in texts
        ):
            raise ValueError(
                f"Query exceeds the model's {self.limit}-token limit; shorten it."
            )
        encoded = self.model.encode(
            texts,
            prompt=prompt,
            normalize_embeddings=True,
            batch_size=batch_size,
            show_progress_bar=False,
        )
        array = np.asarray(encoded, dtype=np.float32)
        if (
            array.ndim != 2
            or len(array) != len(texts)
            or not array.shape[1]
            or not np.isfinite(array).all()
        ):
            raise RuntimeError(
                "Embedding model did not return all requested finite vectors."
            )
        logger.debug(
            "Encoded batch: count=%d dimensions=%d elapsed_seconds=%.3f",
            len(texts),
            array.shape[1],
            perf_counter() - started,
            extra={"phase": "encode_batch_ready"},
        )
        return array


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_texts(
    tokenizer, texts: list[str], limit: int, prompt: str = "", reserved_tokens: int = 0
) -> list[list[str]]:
    started = perf_counter()
    result = []
    overhead = len(tokenizer.encode(prompt, add_special_tokens=True, verbose=False))
    budget = limit - overhead - reserved_tokens - 1
    if budget < 8:
        raise ValueError("Model token budget is too small for transcript search.")
    encoded = tokenizer(
        texts,
        add_special_tokens=False,
        truncation=False,
        return_offsets_mapping=True,
        verbose=False,
    )
    for text, offsets in zip(texts, encoded["offset_mapping"]):
        if not offsets:
            result.append([text])
            continue
        chunks = []
        begin = 0
        while begin < len(offsets):
            end = min(begin + budget, len(offsets))
            start_char = offsets[begin][0]
            end_char = offsets[end - 1][1]
            chunk = text[start_char:end_char]
            while (
                len(tokenizer.encode(prompt + chunk, verbose=False))
                > limit - reserved_tokens
            ):
                end -= 1
                if end <= begin:
                    raise ValueError(
                        "Cannot split transcript within the model token limit."
                    )
                end_char = offsets[end - 1][1]
                chunk = text[start_char:end_char]
            chunks.append(chunk)
            if end == len(offsets):
                break
            begin = max(begin + 1, end - min(24, (end - begin) // 4))
        result.append(chunks)
    logger.debug(
        "Token splitting complete: texts=%d chunks=%d token_budget=%d elapsed_seconds=%.3f",
        len(texts),
        sum(map(len, result)),
        budget,
        perf_counter() - started,
        extra={"phase": "token_split"},
    )
    return result

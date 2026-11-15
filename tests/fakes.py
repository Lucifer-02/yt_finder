"""Deterministic tokenizer/model doubles; never download model weights in unit tests."""

import re
from unittest.mock import MagicMock

import numpy as np


class FakeTokenizer:
    def encode(self, text, add_special_tokens=True, **kwargs):
        return list(range(len(text.split()) + (2 if add_special_tokens else 0)))

    def num_special_tokens_to_add(self, pair=False):
        return 3 if pair else 2

    def __call__(self, texts, **kwargs):
        return {
            "offset_mapping": [
                [m.span() for m in re.finditer(r"\S+", text)] for text in texts
            ]
        }


def fake_model():
    model = MagicMock()
    model.tokenizer = FakeTokenizer()
    model.max_seq_length = 128
    model.prompts = {}
    model[0].auto_model.config._commit_hash = "test-revision"
    model.encode.side_effect = lambda texts, **kwargs: np.array(
        [[1.0, 0.0]] * len(texts), dtype=np.float32
    )
    return model


def fake_reranker():
    reranker = MagicMock()
    reranker.tokenizer = FakeTokenizer()
    reranker.predict.side_effect = lambda pairs, **kwargs: np.zeros(
        len(pairs), dtype=np.float32
    )
    return reranker

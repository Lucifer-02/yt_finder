"""Search-owned binary vectors, token splits and full-text index."""

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from pathlib import Path
from time import perf_counter

import numpy as np

logger = logging.getLogger(__name__)


def tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", text.casefold()).replace("đ", "d")
    return re.findall(
        r"\w+", "".join(c for c in normalized if not unicodedata.combining(c))
    )


def lexical_signature(passages) -> str:
    signature = hashlib.sha256()
    for p in passages:
        signature.update(json.dumps(p, ensure_ascii=False, sort_keys=True).encode())
    return signature.hexdigest()


class EmbeddingStore:
    def __init__(self, path: str):
        db_path = Path(path).expanduser().resolve()
        if path != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            ":memory:" if path == ":memory:" else str(db_path), timeout=60
        )
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS embedding_vectors (
                model TEXT NOT NULL, config TEXT NOT NULL, text_hash TEXT NOT NULL,
                dimension INTEGER NOT NULL, vector BLOB NOT NULL,
                PRIMARY KEY (model, config, text_hash));
            CREATE TABLE IF NOT EXISTS token_splits (
                config TEXT NOT NULL, text_hash TEXT NOT NULL, chunks TEXT NOT NULL,
                PRIMARY KEY (config, text_hash));
            CREATE TABLE IF NOT EXISTS search_state (name TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS lexical_windows USING fts5(text);
            CREATE TABLE IF NOT EXISTS lexical_rows (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE,
                position INTEGER NOT NULL);
            """
        )

    def vectors(self, keys: list[str], model: str, config: str):
        result = {}
        unique = list(dict.fromkeys(keys))
        for offset in range(0, len(unique), 500):
            batch = unique[offset : offset + 500]
            marks = ",".join("?" for _ in batch)
            for key, dimension, blob in self.db.execute(
                "SELECT text_hash,dimension,vector FROM embedding_vectors "
                f"WHERE model=? AND config=? AND text_hash IN ({marks})",
                [model, config, *batch],
            ):
                vector = np.frombuffer(blob, dtype="<f4")
                if len(vector) != dimension or not np.isfinite(vector).all():
                    raise ValueError(
                        "Invalid cached embedding; use a separate search cache."
                    )
                result[key] = vector
        logger.debug(
            "Vector cache lookup: model=%s requested=%d hits=%d misses=%d",
            model,
            len(unique),
            len(result),
            len(unique) - len(result),
            extra={"phase": "vector_cache_lookup"},
        )
        return result

    def save_vectors(self, rows, model: str, config: str) -> None:
        values = []
        for text_hash, vector in rows:
            array = np.asarray(vector, dtype="<f4")
            if array.ndim != 1 or not array.size or not np.isfinite(array).all():
                raise ValueError("Embedding must be a finite nonempty vector.")
            values.append((model, config, text_hash, array.size, array.tobytes()))
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO embedding_vectors VALUES(?,?,?,?,?)", values
            )

        logger.debug(
            "Vector cache saved: model=%s vectors=%d",
            model,
            len(values),
            extra={"phase": "vector_cache_save"},
        )

    def splits(self, keys: list[str], config: str) -> dict[str, list[str]]:
        result = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset : offset + 500]
            marks = ",".join("?" for _ in batch)
            for key, chunks in self.db.execute(
                "SELECT text_hash,chunks FROM token_splits "
                f"WHERE config=? AND text_hash IN ({marks})",
                [config, *batch],
            ):
                result[key] = json.loads(chunks)
        return result

    def save_splits(self, rows: list[tuple[str, list[str]]], config: str) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO token_splits VALUES(?,?,?)",
                [
                    (config, key, json.dumps(chunks, ensure_ascii=False))
                    for key, chunks in rows
                ],
            )

    def lexical_scores(
        self,
        query: str,
        passages,
        fingerprint: str | None = None,
        *,
        keyword_query: bool = False,
    ) -> np.ndarray:
        """Synchronize and query FTS5 atomically across search processes."""
        started = perf_counter()
        scores = np.zeros(len(passages), dtype=np.float32)
        terms = list(dict.fromkeys(tokens(query)))
        if not terms or not passages:
            return scores
        if keyword_query:
            if query.count('"') % 2:
                raise ValueError("Close the double quote around the search phrase.")
            clauses = []
            for phrase, word in re.findall(r'"([^"]*)"|([^\s"]+)', query):
                normalized = tokens(phrase or word)
                if normalized:
                    if phrase:
                        clauses.append('"' + " ".join(normalized) + '"')
                    else:
                        clauses.extend('"' + term + '"' for term in normalized)
            expression = " AND ".join(dict.fromkeys(clauses))
        else:
            expression = " OR ".join('"' + t + '"' for t in terms)
        if not expression:
            return scores
        fingerprint = fingerprint or lexical_signature(passages)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT value FROM search_state WHERE name='lexical-v2'"
            ).fetchone()
            if row is None or row[0] != fingerprint:
                added = 0
                if row is None:
                    # Upgrade the derived FTS index once; retain old vector tables.
                    self.db.execute("DELETE FROM lexical_windows")
                    self.db.execute("DELETE FROM lexical_rows")
                current = {
                    key: (rowid, position)
                    for key, rowid, position in self.db.execute(
                        "SELECT key,id,position FROM lexical_rows"
                    )
                }
                positions = []
                for position, passage in enumerate(passages):
                    key = hashlib.sha256(
                        json.dumps(passage, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest()
                    existing = current.pop(key, None)
                    if existing is not None:
                        if existing[1] != position:
                            positions.append((position, existing[0]))
                        continue
                    added += 1
                    cursor = self.db.execute(
                        "INSERT INTO lexical_rows(key,position) VALUES(?,?)",
                        (key, position),
                    )
                    self.db.execute(
                        "INSERT INTO lexical_windows(rowid,text) VALUES(?,?)",
                        (
                            cursor.lastrowid,
                            " ".join(tokens(passage["text"] + " " + passage["title"])),
                        ),
                    )
                self.db.executemany(
                    "UPDATE lexical_rows SET position=? WHERE id=?", positions
                )
                removed = [(value[0],) for value in current.values()]
                self.db.executemany(
                    "DELETE FROM lexical_windows WHERE rowid=?", removed
                )
                self.db.executemany("DELETE FROM lexical_rows WHERE id=?", removed)
                self.db.execute(
                    "INSERT OR REPLACE INTO search_state VALUES('lexical-v2',?)",
                    (fingerprint,),
                )
                logger.info(
                    "BM25 index updated: windows=%d added=%d removed=%d repositioned=%d",
                    len(passages),
                    added,
                    len(removed),
                    len(positions),
                    extra={"phase": "lexical_index_update"},
                )
            else:
                logger.info(
                    "BM25 index reused: windows=%d",
                    len(passages),
                    extra={"phase": "lexical_index_reuse"},
                )
            for position, score in self.db.execute(
                "SELECT r.position,bm25(lexical_windows) FROM lexical_windows "
                "JOIN lexical_rows r ON r.id=lexical_windows.rowid "
                "WHERE lexical_windows MATCH ?",
                (expression,),
            ):
                scores[position] = -score
        logger.info(
            "BM25 retrieval complete: terms=%d matches=%d elapsed_seconds=%.3f",
            len(terms),
            np.count_nonzero(scores),
            perf_counter() - started,
            extra={"phase": "lexical_ready"},
        )
        return scores

    def close(self) -> None:
        self.db.close()

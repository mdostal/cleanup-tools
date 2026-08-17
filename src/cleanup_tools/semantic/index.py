"""Local, incremental content-hash-keyed embedding cache.

A plain stdlib ``sqlite3`` database (NOT ``sqlite-vec`` -- see
``.pHive/epics/document-topic-clustering/docs/design-discussion.md`` §3: a
compiled sqlite extension is more dependency/build risk than benefit at this
project's actual scale, thousands not millions of vectors -- brute-force
in-process cosine similarity over a plain BLOB table is simpler and
sufficient), living at ``~/.config/cleanup-tools/semantic_index.sqlite3``,
next to ``config.yaml``/``approval_queue.yaml``.

**Content hash is a cache key only** -- "has this exact content already been
embedded, skip if so" -- never a cap on what actually got embedded (see
``extract.py``: the FULL extracted text is what's embedded, this module never
truncates it). This mirrors ``queue.build_plan_snapshot``'s own
capped-prefix-hash-as-staleness-signal contract, applied to a different
purpose.

**One row per (kind, content_hash, face_index)**, not one row per file: a
document has exactly one embedding (``face_index`` defaults to ``0``), but a
photo can contain multiple detected faces, each its own row (``face_index``
0..N-1) -- see the design discussion §4. ``kind`` (``"document"`` |
``"face"``) keeps the two embedding spaces from ever being clustered
together, even if their vector dimensions happened to coincide.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..adapters.base import OSAdapter

INDEX_FILENAME = "semantic_index.sqlite3"

KIND_DOCUMENT = "document"
KIND_FACE = "face"


@dataclass
class IndexedEmbedding:
    """One stored embedding -- one document, or one detected face within a photo."""

    content_hash: str
    kind: str
    path: str
    embedding: list[float]
    face_index: int = 0
    bbox: tuple[float, float, float, float] | None = None


def default_index_path(adapter: OSAdapter) -> Path:
    """Return the default index path: ``~/.config/cleanup-tools/semantic_index.sqlite3``."""
    return adapter.resolve_home() / ".config" / "cleanup-tools" / INDEX_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            kind TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            face_index INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL,
            bbox TEXT,
            embedding BLOB NOT NULL,
            PRIMARY KEY (kind, content_hash, face_index)
        )
        """
    )
    return conn


def is_indexed(
    adapter: OSAdapter,
    content_hash: str,
    *,
    kind: str = KIND_DOCUMENT,
    face_index: int = 0,
    path: Path | None = None,
) -> bool:
    """Whether ``content_hash`` (at ``face_index``, for ``kind``) is already
    stored -- the incremental-reindex check callers use to skip re-embedding
    unchanged content.
    """
    index_path = path or default_index_path(adapter)
    conn = _connect(index_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM embeddings WHERE kind = ? AND content_hash = ? AND face_index = ?",
            (kind, content_hash, face_index),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def add_embedding(
    adapter: OSAdapter,
    content_hash: str,
    file_path: str,
    embedding: list[float],
    *,
    kind: str = KIND_DOCUMENT,
    face_index: int = 0,
    bbox: tuple[float, float, float, float] | None = None,
    path: Path | None = None,
) -> None:
    """Store (or overwrite) one embedding row."""
    index_path = path or default_index_path(adapter)
    conn = _connect(index_path)
    try:
        conn.execute(
            """
            INSERT INTO embeddings (kind, content_hash, face_index, path, bbox, embedding)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (kind, content_hash, face_index)
            DO UPDATE SET path = excluded.path, bbox = excluded.bbox, embedding = excluded.embedding
            """,
            (
                kind,
                content_hash,
                face_index,
                file_path,
                json.dumps(bbox) if bbox is not None else None,
                _encode_embedding(embedding),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_embeddings(
    adapter: OSAdapter, *, kind: str = KIND_DOCUMENT, path: Path | None = None
) -> list[IndexedEmbedding]:
    """Every stored embedding of ``kind`` -- the input clustering operates over.

    Never mixes kinds: a caller asking for ``kind="face"`` never sees a
    ``"document"`` row, regardless of vector dimensions.
    """
    index_path = path or default_index_path(adapter)
    conn = _connect(index_path)
    try:
        rows = conn.execute(
            "SELECT content_hash, face_index, path, bbox, embedding FROM embeddings WHERE kind = ?",
            (kind,),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for content_hash, face_index, file_path, bbox_json, embedding_blob in rows:
        bbox = tuple(json.loads(bbox_json)) if bbox_json is not None else None
        result.append(
            IndexedEmbedding(
                content_hash=content_hash,
                kind=kind,
                path=file_path,
                embedding=_decode_embedding(embedding_blob),
                face_index=face_index,
                bbox=bbox,
            )
        )
    return result


def _encode_embedding(embedding: list[float]) -> bytes:
    return json.dumps(embedding).encode("utf-8")


def _decode_embedding(blob: bytes) -> list[float]:
    return json.loads(blob.decode("utf-8"))

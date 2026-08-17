"""Cosine-similarity threshold + union-find clustering, plus a local
word-frequency label/slug heuristic -- no HDBSCAN/scikit-learn.

Per the research brief's own explicit steer ("avoid pulling in scikit-learn
as a new heavy dep if it can be avoided") and this project's personal-library,
thousands-not-millions scale (the brief's own performance ballpark: HDBSCAN
over a few thousand vectors is "seconds" either way, so brute-force O(N^2)
pairwise comparison is genuinely fine here) -- see
``.pHive/epics/document-topic-clustering/docs/design-discussion.md`` §2.4.

A singleton "cluster" (no other vector similar enough) is never returned --
mirrors ``sort``'s own "other" bucket precedent: unclustered items are simply
left alone, never force-grouped.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

DEFAULT_THRESHOLD = 0.75

# A small, hardcoded English stopword list -- no new dependency (nltk/spacy)
# for what is deliberately a cheap, unglamorous local heuristic, not a real
# NLP pipeline. Not exhaustive; good enough to keep the label heuristic from
# picking "the"/"and"/"for" as a cluster's name.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
        "was", "were", "be", "been", "this", "that", "these", "those", "with",
        "at", "by", "from", "as", "it", "its", "you", "your", "we", "our", "i",
        "me", "my", "he", "she", "they", "them", "his", "her", "their", "not",
        "no", "yes", "all", "any", "each", "if", "so", "than", "then", "there",
        "here", "will", "would", "can", "could", "should", "have", "has", "had",
        "do", "does", "did", "but", "into", "onto", "up", "down", "out", "over",
        "under", "again", "further", "once", "about",
    }
)

_WORD_RE = re.compile(r"[A-Za-z]{3,}")


@dataclass
class Cluster:
    """One group of similar items (by index into the input list)."""

    member_indices: list[int]
    label: str
    slug: str


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0.0:
        return 0.0
    return float(np.dot(va, vb) / denom)


class _UnionFind:
    """Plain list-based union-find (no set/dict iteration involved anywhere)
    so clustering output is deterministic regardless of Python's per-process
    hash randomization -- a real, load-bearing property this module's own
    tests check directly.
    """

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        root_i, root_j = self.find(i), self.find(j)
        if root_i != root_j:
            self._parent[max(root_i, root_j)] = min(root_i, root_j)


def cluster(
    embeddings: list[list[float]],
    texts: list[str] | None = None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    prefix: str = "cluster",
) -> list[Cluster]:
    """Group ``embeddings`` (by index) into clusters at ``threshold`` cosine
    similarity, via brute-force pairwise comparison + union-find.

    ``texts``, if given (same length/order as ``embeddings``), feeds the
    label/slug heuristic for each resulting cluster -- omit it (e.g. for
    faces, which have no meaningful "text") and every cluster gets the
    generic ``f"{prefix}-<n>"`` fallback (see :func:`label_and_slug`).
    ``prefix`` defaults to ``"cluster"`` (document-topic-clustering's
    convention); ``photo-face-clustering`` passes ``"person"`` so an
    unlabeled face cluster falls back to ``"person-1"``, ``"person-2"``, ...
    rather than the generic document-oriented name -- the SAME clustering
    math either way, just a different fallback label namespace per domain.

    Deterministic: the SAME input, run twice, produces identical output --
    no set/dict-iteration-order dependency anywhere in this function.
    Singleton groups (no other vector above threshold) are never included in
    the result.
    """
    n = len(embeddings)
    uf = _UnionFind(n)

    for i in range(n):
        for j in range(i + 1, n):
            if cosine_similarity(embeddings[i], embeddings[j]) >= threshold:
                uf.union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = uf.find(i)
        groups.setdefault(root, []).append(i)

    clusters: list[Cluster] = []
    # Iterate roots in ascending order (not dict/set iteration) so cluster
    # ordering itself is deterministic too.
    for root in sorted(groups):
        member_indices = groups[root]
        if len(member_indices) < 2:
            continue  # singleton -- never proposed, see module docstring.
        member_texts = [texts[i] for i in member_indices] if texts is not None else None
        label, slug = label_and_slug(member_texts, fallback_id=len(clusters) + 1, prefix=prefix)
        clusters.append(Cluster(member_indices=member_indices, label=label, slug=slug))

    return clusters


def label_and_slug(texts: list[str] | None, *, fallback_id: int, prefix: str = "cluster") -> tuple[str, str]:
    """The human-readable **label** (shown in the UI) and the deterministic,
    filesystem-safe **slug** derived from it (used as an actual directory
    name in ``dest``) -- related but distinct, per this epic's own grill
    finding V1: never just one or the other.

    The label is the single most common non-stopword token (3+ letters)
    across ``texts``, if any token clears a minimum-frequency bar (>= 2
    occurrences, or the only candidate at all). Falls back to
    ``f"{prefix}-{fallback_id}"`` (label == slug in the fallback case) if no
    real text was given or no token qualifies.
    """
    if texts:
        counts: Counter[str] = Counter()
        for text in texts:
            for word in _WORD_RE.findall(text.lower()):
                if word not in _STOPWORDS:
                    counts[word] += 1
        if counts:
            top_word, top_count = counts.most_common(1)[0]
            if top_count >= 2 or len(counts) == 1:
                return top_word, _slugify(top_word)

    fallback = f"{prefix}-{fallback_id}"
    return fallback, fallback


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return slug[:60] or "cluster"

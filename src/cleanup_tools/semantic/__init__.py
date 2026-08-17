"""Local, offline semantic document/photo clustering.

Structurally parallel to ``ai/`` and ``adapters/`` -- an ABC + factory pattern
for the one part that's genuinely platform-specific (embedding/OCR), pure
Python everywhere else. See ``embeddings.py``/``extract.py`` for the
macOS-only (phase 1) platform boundary, ``index.py``/``cluster.py`` for the
platform-independent storage/grouping layer, and ``pipeline.py`` for the
orchestrator that stages real, reviewable ``QueueEntry`` proposals via
``queue.stage_entries()`` -- never a parallel state store, never a new
approval flow.

See ``.pHive/epics/document-topic-clustering/docs/design-discussion.md`` for
the full architecture and every load-bearing design decision.
"""

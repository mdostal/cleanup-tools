# Horizontal plan: document-topic-clustering

## Layers

**1. Local extraction + embedding (new, platform-gated)**
`src/cleanup_tools/semantic/embeddings.py` (`TextEmbedder` ABC + `AppleTextEmbedder` via
PyObjC `NLContextualEmbedding` + `get_embedder(adapter)` factory, `NotImplementedError` off
macOS) and `semantic/extract.py` (`extract_text(path) -> str | None` — PDFKit text layer for
`.pdf` with Vision OCR fallback for empty/near-empty layers, Vision OCR directly for image
files, plain read for `.txt`/`.md`; never raises). No queue/UI/index dependency — pure
functions over a real file path in, an embedding vector or text string out.

**2. Local vector index + clustering (new, platform-independent)**
`src/cleanup_tools/semantic/index.py` (plain stdlib `sqlite3` at
`~/.config/cleanup-tools/semantic_index.sqlite3`, keyed by content hash used purely as an
incremental-reindex cache key — see design discussion's grill-T2 resolution) and
`semantic/cluster.py` (cosine-similarity threshold + union-find grouping over stored
embeddings, plus the local word-frequency label/slug heuristic). Depends only on numpy +
stdlib — fully testable with fake embedding vectors, no PyObjC/macOS dependency at all.

**3. Pipeline orchestration + queue integration (new)**
`src/cleanup_tools/semantic/pipeline.py`: recursive walk of configured locations (reusing
`config.configured_locations`), the iCloud-placeholder-materialized guard (design
discussion §2.6), per-file extract→embed→index (layers 1+2), then cluster→stage as
`QueueEntry(action="move", group_key=f"cluster:{location}:{slug}", source="local:cluster")`
via the EXISTING `queue.stage_entries()` — plus extending `queue.parse_group_key`/
`queue.group_entries_hierarchical` to recognize the new `"cluster"` pipeline prefix
alongside `"sort"`/`"reclaim"`/`"corral-screenshots"`. This is the layer where grill-T1's
dest-collision disambiguation and `queue.is_protected_path` reuse both live.

**4. Routes + UI (extend existing surfaces, one new trigger)**
`src/cleanup_tools/ui/routes.py`: a new `/plan/cluster-documents` route mirroring
`plan_sort`/`plan_reclaim`'s exact background-job/poll shape, plus a nav link. A small
addition to `queue.html`'s entry rendering — an extracted-text-snippet disclosure for
`source="local:cluster"` entries, reusing the `short_path()`-style `<details>/<summary>`
pattern. No new page; the dashboard tree and Review Queue already render anything
group_key-shaped.

**5. Config + Settings (extend existing)**
One new `Config` field (`semantic_cluster_threshold: float = 0.75`), surfaced on Settings —
following the exact `chat_turn_cap` precedent (a new field/section, load/save/
config_to_dict, a form + POST route). No new location-selection UI (reuses
`configured_locations`, same as `chat_turn_cap` reused the AI Provider pane rather than
inventing a new one).

**6. Packaging + platform verification (new dependency, real risk)**
`pyproject.toml` gains a `semantic` extras group (`pyobjc-framework-NaturalLanguage`,
`pyobjc-framework-Vision`, `pyobjc-framework-Quartz`) plus `numpy` as a base dependency.
`packaging/pyinstaller/cleanup_ui.spec` gains hiddenimports for the PyObjC modules. This
layer closes with a real manual verification pass against a frozen build — PyObjC-under-
PyInstaller is unverified in this project, per the design discussion's stated risk.

## Cross-layer dependencies

Layer 1 and layer 2 are independent of each other (extraction/embedding vs. storage/
clustering) — both can be built and tested in isolation, layer 2 using fake embedding
vectors that never touch layer 1's real PyObjC calls.
Layer 3 depends on BOTH layer 1 (for real extraction/embedding) and layer 2 (for storage/
clustering) — it's the orchestrator that wires them together, plus the new queue
integration (`parse_group_key`/`group_entries_hierarchical` extension).
Layer 4 depends on layer 3 (the trigger route calls the pipeline; the snippet disclosure
reads what layer 3 staged).
Layer 5 (the threshold config field) is read by layer 3 at cluster time and surfaced by
layer 4's settings addition — same "needed by two layers independently, no ordering
constraint between them" shape as `chat_turn_cap` had last epic.
Layer 6 depends on layers 1-5 being functionally complete (it packages the finished
feature), but the PyObjC IMPORT/CALL verification half of layer 6 should happen as early as
possible (folded into layer 1's first research step) since it's a real go/no-go signal for
the whole epic's dependency-footprint decision, not something to discover at the end.

## Sequencing

1 ∥ 2 (parallelizable — no shared state) → 3 (needs both) → 4 ∥ 5 (parallelizable — UI
trigger and settings field are independent additions once 3 exists) → 6 (packages the
finished, working feature; its early PyObjC-import spike happens inside layer 1's first
step, not deferred to the end).

# Design Discussion: photo-face-clustering

## 0. Prelude

Builds directly on `document-topic-clustering` (phase 1, planned and committed this
session, not yet executed) — reuses that epic's `semantic/index.py`/`semantic/cluster.py`
and the `"cluster"` `group_key` pipeline prefix it adds to `queue.py`. No prior KG/
north_star block to cite (see `.pHive/CONTEXT.md`).

## 1. Goal

Group photos by the person(s) in them, staged into the SAME approval queue every other
pipeline uses — the second half of the semantic-clustering vision, following documents.
Entirely local (no network calls). Pets and cross-photo/cross-document identity linking
stay explicitly out of scope (research brief §2.1, §4).

## 2. The dependency reality check that reshapes this epic

`document-topic-clustering`'s central win was "Apple already ships a real on-device text
embedder (`NLContextualEmbedding`), reachable via PyObjC, zero bundle cost." **That win does
not repeat here.** Apple's public Vision framework detects faces
(`VNDetectFaceRectanglesRequest`) but does not expose a public identity-embedding API —
Apple Photos' own person-clustering is closed-source. There is no local-native, zero-cost
alternative to InsightFace (SCRFD + ArcFace `buffalo_l`, via `onnxruntime`) on **any**
platform, Mac or Arch. This epic's dependency footprint is therefore genuinely different in
kind from phase 1's, not just degree:

- ~300-500MB of ONNX model weights (one-time, per research brief §2.1's storage ballpark).
- A real, non-trivial `onnxruntime` + `insightface`-adjacent dependency tree (heavier than
  this project's current lean footprint of PyYAML/Flask/Pillow/anthropic/numpy).
- **Cross-platform by necessity, not by extra effort**: since there's no cheaper
  macOS-native path to sidestep, this is the FIRST feature in this codebase that reaches
  full Mac+Arch parity for a semantic-ML capability — a genuine upside buried inside an
  otherwise heavier dependency decision.
- CoreML execution-provider acceleration auto-engages on Apple Silicon (per research
  brief's performance ballpark); plain CPU execution on Arch/Intel Mac — same code path,
  different runtime performance, not a capability gap. This is NOT a
  `NotImplementedError`-on-Arch situation like `document-topic-clustering`'s text embedder
  or `set_screenshot_save_location` — it's a genuine cross-platform feature from day one.

**Decision, stated plainly**: ship it anyway, because there is no cheaper alternative that
delivers the actual feature, and because this is explicitly a user-triggered, opt-in-cost
action (mirroring `reclaim`'s own "visible, explicit, never ambient" precedent) — the
weight is real but it's paid once, on-demand, not silently at every app launch.

### 2.1 Offline-reliability: the InsightFace model zoo is its own version of the HF_HUB_OFFLINE gotcha

The `insightface` Python package's own default loading path
(`insightface.app.FaceAnalysis`) downloads model weights from a remote model zoo on first
use unless explicitly told not to — the EXACT class of gotcha the research brief flags for
Hugging-Face-ecosystem loaders (`HF_HUB_OFFLINE=1` not being fully reliable), just a
different package. This epic's implementation must NOT rely on `insightface`'s own
convenience auto-download: model weights are vendored as static files (bundled with the
app, same discipline `ai/anthropic_provider.py` and `document-topic-clustering` already
established for third-party network-touching code), and the ONNX Runtime session is
constructed directly from those local files via `onnxruntime.InferenceSession`, never
through `insightface`'s high-level auto-downloading wrapper. This is a concrete engineering
task in this epic (see semantic-packaging-and-verification-faces), not an assumption that
"it's local so it's fine."

## 3. Clustering: reuse `cluster.py` as-is, explicit tradeoff stated

The research brief recommends HDBSCAN specifically for faces (density-adaptive: "a person
in 500 photos vs. 3"). This epic deliberately reuses `document-topic-clustering`'s
threshold+union-find `cluster.py` UNCHANGED instead, for consistency (one clustering
algorithm across both epics, one thing to reason about/tune/fix) rather than introducing a
second, different grouping algorithm for a second content type. **Accepted, explicitly
worse-than-optimal tradeoff**: a person with very few photos may cluster less reliably than
HDBSCAN would handle. If real usage shows this matters, upgrading `cluster.py` to HDBSCAN
benefits BOTH epics' cluster quality simultaneously, behind the same interface — a
deliberate reason to defer, not an oversight.

## 4. Index schema: faces are many-per-file, documents are one-per-file

`document-topic-clustering`'s `semantic/index.py` is keyed one row per content hash (one
embedding per file). A photo can contain **multiple faces** — this epic's embeddings are
one row per **detected face**, not per file: `(content_hash, face_index, bbox, embedding)`.
`index.py` gains a `kind` column (`"document"` | `"face"`) so clustering NEVER mixes the two
embedding spaces (512-d ArcFace vectors and whatever-dimension `NLContextualEmbedding`
vectors are not comparable, and even same-dimension coincidence must never be trusted) —
`cluster()` always operates within one `kind` at a time, by construction, not by convention.

## 5. The multi-person-photo problem — the central new tension this epic must resolve

A document belongs to at most one topic cluster; a **photo can contain multiple people**,
and each detected face clusters independently by identity. But `QueueEntry.action="move"`
can only place a file in ONE destination — a group photo with 3 people can't simultaneously
"move" into 3 different person-folders.

**Decision**: this epic proposes a move ONLY for a photo where every detected face in it
maps to the SAME person-cluster (the common case: a solo portrait, a headshot, a burst of
photos of one person) — group/multi-person photos are detected, embedded, and indexed but
**never staged** in this phase. This is a deliberate, stated limitation — not a silent gap
discovered later — chosen specifically because it keeps `action="move"` semantics 100%
consistent with every other pipeline in this codebase (`sort`, `reclaim`,
`document-topic-clustering`, the chat agent's `propose_moves`) rather than inventing a new
"tag, don't move" `QueueEntry` action type, which the research brief's own metadata-signals
section (§2.4) notes IS a real, zero-copy mechanism (Finder tags/xattr) that a LATER epic
could use to properly handle multi-person photos without file duplication — explicitly
deferred, not solved here.

**Why index (not just detect-and-discard) group photos at all (grill T2)**: per-face
detection+embedding is the same per-photo cost whether or not the result ends up staged —
skipping the embed/index step for multi-face photos saves nothing (the expensive part,
running the model, already happened to determine there's more than one face), while
indexing them means a LATER epic that solves multi-membership tagging never needs a full
re-scan of a library that's already been processed once. An explicit, deliberate tradeoff,
not assumed self-evident.

`group_key` follows the EXACT scheme `document-topic-clustering` already added to
`queue.py`: `f"cluster:{location}:{person-slug}"` — a person-cluster and a topic-cluster
are structurally identical from the queue/dashboard's perspective, so zero further
`queue.py` changes are needed beyond what phase 1 already shipped. **Destination
subdirectory is domain-separated (grill T1)**: `<location>/_clusters/by-person/<slug>/`
for people, versus `document-topic-clustering`'s `<location>/_clusters/by-topic/<slug>/`
for topics (a small, backward-compatible refinement to phase 1's own dest construction,
applied when that story is implemented) — a human browsing `_clusters/` sees two clearly
separated kinds of grouping, and a topic slug can never coincidentally collide with a
person slug.

## 6. UI: no new review surface needed beyond what phase 1 already built

Face-cluster `QueueEntry` objects are ordinary image-file entries — the EXISTING thumbnail
route (`is_image_entry`, already rendering full-photo thumbnails for any image entry
regardless of source) works unmodified; a human reviewing "person cluster, 6 photos" sees
real photo thumbnails, which is more than sufficient to recognize their own people (no new
face-crop-rendering infrastructure needed). What IS new: a second "Plan: Cluster Photos by
Person" trigger (mirrors phase 1's "Plan: Cluster Documents" exactly) and a SEPARATE
`semantic_face_cluster_threshold` Settings field (face-embedding cosine similarity and
text-embedding cosine similarity are different metric spaces with different meaningful
ranges — reusing `semantic_cluster_threshold` across both would be a real correctness trap,
not just a naming nicety).

Cluster labeling: unlike a document topic (extractable from text), a person's actual name
is not discoverable from file content at all — clusters default to a generic
`"person-<n>"` slug (phase 1's same unlabeled-cluster fallback pattern), renamed by a human
at review time via the existing per-entry edit primitive. No local heuristic attempts a
real name guess.

## 7. Risks

- **Bundle size / packaging**: ~300-500MB of vendored ONNX weights is a real, first-of-its-
  kind PyInstaller payload for this project. Mitigated by treating packaging+frozen-build
  verification as its own dedicated story, same discipline `document-topic-clustering`
  already established, not an afterthought.
- **InsightFace's own model-zoo auto-download must be fully bypassed**, not merely
  disabled by a flag that might not be fully honored (§2.1's stated gotcha) — mitigated by
  constructing the ONNX Runtime session directly from vendored local files, never through
  `insightface`'s high-level convenience wrapper, plus the same "grep for phone-home
  behavior, add a regression test that fails loudly on drift" audit discipline
  `ai/anthropic_provider.py` already established.
- **Face-clustering accuracy on real personal libraries** is genuinely lower than curated
  benchmarks (age variation, lighting, look-alikes) — accepted, mitigated by the
  multi-person-photo exclusion (§5, reduces false-confidence blast radius) and the fact
  that every proposal is still human-reviewed before anything moves, same as every other
  pipeline.
- **Emotionally higher stakes than document misclassification** (research brief's own
  explicit callout) — mitigated by NOT attempting an ambitious full merge/split editor in
  this phase (§6), keeping the review surface simple and using the existing, already-
  trusted per-entry approve/reject/edit primitives rather than a new, unproven UI.

## 8. Dependencies

Depends on `document-topic-clustering` being complete (reuses its `index.py`/`cluster.py`/
`queue.py` "cluster" pipeline extension directly, not a parallel reimplementation).

## 9. Scale assessment

**Large.** New heavy dependency tree, first genuinely cross-platform local-ML capability in
this codebase, a real unresolved-until-now architectural question (multi-face-per-photo)
requiring an explicit scoping decision, and real packaging risk at a much larger bundle
size than phase 1. Proceeding to H/V decomposition.

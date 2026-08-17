# Horizontal plan: photo-face-clustering

## Layers

**1. Face detection + embedding (new, platform-independent this time)**
`src/cleanup_tools/semantic/faces.py`: InsightFace (SCRFD detector + ArcFace `buffalo_l`
embeddings, 512-d) via `onnxruntime`, CoreML execution provider auto-engaging on Apple
Silicon, plain CPU elsewhere. Unlike `document-topic-clustering`'s `embeddings.py`, this
works on Mac AND Arch identically (see design discussion §2) — no `NotImplementedError`
branch needed here. Model weights are vendored static files, loaded directly via
`onnxruntime.InferenceSession`, never through `insightface`'s own auto-downloading
convenience wrapper (design discussion §2.1).

**2. Index schema extension + reused clustering (extend existing)**
`semantic/index.py` gains a `kind` column (`"document" | "face"`) and a per-face composite
key (`content_hash`, `face_index`, `bbox`) instead of one-row-per-file (design discussion
§4). `semantic/cluster.py` is reused UNCHANGED — same threshold+union-find interface,
called once more for the `"face"` kind, never mixed with `"document"` embeddings.

**3. Pipeline: reused walk, new staging rule (extend existing)**
`semantic/pipeline.py` reuses `document-topic-clustering`'s existing recursive-scan +
protected-path + iCloud-guard walk (parameterized by extractor: text vs. face), adding the
multi-person-photo exclusion rule (design discussion §5: stage a move only when every
detected face in a photo maps to the same cluster) and the `_clusters/by-person/<slug>/`
dest convention (domain-separated from `by-topic/`, per this epic's own grill T1).

**4. Routes + UI (extend existing, one new trigger)**
A second "Plan: Cluster Photos by Person" trigger (mirrors "Plan: Cluster Documents"
exactly), reusing the EXISTING image-thumbnail rendering (`is_image_entry`) unmodified —
face-cluster entries are ordinary image `QueueEntry`s. A new, SEPARATE
`semantic_face_cluster_threshold` Settings field (different metric space than
`semantic_cluster_threshold` — design discussion §6).

**5. Packaging + verification (new, heavier than phase 1)**
`pyproject.toml`'s `semantic` extras group grows to include `onnxruntime` +
`insightface`-adjacent packages; vendored ONNX model weights (~300-500MB) as bundled data
files; `packaging/pyinstaller/cleanup_ui.spec` gains both hiddenimports AND a much larger
`datas` entry than anything this project has shipped before. The offline-reliability audit
(design discussion §2.1: grep for any phone-home behavior in the loading path, add a
regression test that fails loudly on drift) is a concrete task in this layer, not an
assumption. Closes with a real frozen-build manual verification pass, same discipline as
phase 1's closing story.

## Cross-layer dependencies

Layer 1 is independent of layer 2 (detection/embedding vs. storage/clustering) — testable
in isolation with fake vectors for layer 2.
Layer 3 depends on BOTH 1 and 2, plus reuses `document-topic-clustering`'s ALREADY-BUILT
walk logic directly (a real cross-epic dependency, not just a conceptual precedent).
Layer 4 depends on layer 3.
Layer 5 depends on layers 1-4 being functionally complete, but — mirroring phase 1's own
sequencing lesson — the InsightFace model-loading/offline-reliability verification should
happen as early as possible (folded into layer 1's first research step), since it's a real
go/no-go signal, not a late-discovered risk.

## Sequencing

1 ∥ 2 (parallelizable, same reasoning as phase 1) → 3 (needs both, plus phase 1's existing
walk) → 4 (needs 3) → 5 (packages the finished feature; its early offline-reliability/
model-loading spike happens inside layer 1's first step).

# Research Brief: photo-face-clustering (Phase 2 of semantic-desktop-organization)

*Reuses `.pHive/research/semantic-desktop-organization.md` in full — no new research agents
dispatched. Relevant sections: §2.1 (face/pet clustering), §2.4 (metadata signals), §2.8
(local-first architecture constraints), §3 (recommended architecture), §5 (phasing — this
epic is §5 point 5, "photo/face clustering should follow as a second phase, reusing the
same index.py/cluster.py infrastructure").

## What this epic takes from the research, verbatim

- **Detection + embedding**: InsightFace (SCRFD detector + ArcFace-family `buffalo_l`
  embeddings, 512-d) via `onnxruntime` with the CoreML execution provider (auto-engages
  ANE/GPU acceleration on Apple Silicon, falls back to CPU elsewhere, including Arch).
- **Clustering algorithm**: the brief recommends HDBSCAN specifically for faces (handles
  "a person in 500 photos vs. 3" density variance better than a flat threshold). This
  epic's design discussion (§3) makes a deliberate, explicit call to stay consistent with
  `document-topic-clustering`'s already-shipped threshold+union-find `cluster.py` instead
  — a documented tradeoff, not an oversight.
- **Performance ballpark**: 3-10 images/sec CPU-only, a few minutes for ~5,000 photos with
  CoreML/ANE engaged. Model weights ~300-500MB one-time; embeddings themselves single-digit
  MB.
- **Accuracy reality check**: ArcFace-class models exceed human-level LFW accuracy, but real
  personal libraries (age variation, lighting, profile shots, look-alike relatives) are
  harder than curated benchmarks — every serious reference tool resolves this with a human
  merge/split review step, not full automation. Apple Photos itself is called out for
  parent/child merge errors "with no clean undo at scale."
- **Pets are explicitly out of scope regardless of this epic** — no mature zero-shot
  clustering tool exists; few-shot reference-photo matching is a fundamentally different,
  separately-scoped feature.

## A genuinely new finding from THIS epic's own design work (not in the original brief)

Apple's public Vision framework exposes face **detection** (`VNDetectFaceRectanglesRequest`,
landmarks) but — unlike `NLContextualEmbedding` for text — does **not** publicly expose a
face-**identity-embedding** API suitable for clustering. Apple Photos' own clustering
pipeline is closed-source (per the brief's own §2.1 note). This means the
`document-topic-clustering` epic's "Apple-native only, zero bundle cost" pattern does
**not** carry over to faces: this epic genuinely needs InsightFace+`onnxruntime` — a
heavier, ~300-500MB bundled-model dependency, on every platform (not gated to
Mac-only/Arch-NotImplementedError the way phase 1 was), because there is no
lighter-weight local-native alternative on ANY platform. See design-discussion.md §2 for
the full reasoning and the resulting packaging/offline-reliability implications.

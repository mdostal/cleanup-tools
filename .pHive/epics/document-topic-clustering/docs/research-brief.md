# Research Brief: document-topic-clustering (Phase 1 of semantic-desktop-organization)

*This epic reuses the existing 8-agent research pass at
`.pHive/research/semantic-desktop-organization.md` in full — no new research agents were
dispatched. This document extracts and pins down only the subset of that research that
governs THIS epic's scope (documents-by-topic, local-only, phase 1), and records the
scoping decisions made from it.*

## Source

`.pHive/research/semantic-desktop-organization.md`, compiled 2026-08-14. Relevant sections
for this epic: §2.2 (OCR + topic clustering), §2.4 (metadata signals), §2.6 (embeddings vs.
LLM classification), §2.8 (local-first architecture constraints grounded in this codebase),
§3 (recommended architecture table), §5 (phasing recommendation — this epic implements
exactly steps (a) and (b) of §5 point 4, explicitly deferring step (c)).

## What this epic takes from the research, verbatim

- **OCR**: Apple Vision framework (`VNRecognizeTextRequest`) is "the best 'free lunch' on
  macOS specifically" — fully on-device, already installed, no bundle-size cost.
- **Embeddings**: `NLContextualEmbedding` (NaturalLanguage framework) is "a real on-device
  BERT-like sentence embedder, already shipped in the OS," reachable via PyObjC.
- **Clustering**: the brief explicitly flags "avoid pulling in scikit-learn as a new heavy
  dep if it can be avoided" — this epic honors that by using a threshold/union-find grouping
  over cosine similarity rather than HDBSCAN, deferring HDBSCAN to a later epic if simple
  thresholding proves too coarse in practice.
- **Vector storage**: the brief recommends `sqlite-vec`; this epic's design discussion
  revisits that specific choice (see design-discussion.md) given this is a personal-library,
  thousands-not-millions scale where a compiled sqlite extension may be more dependency risk
  than benefit versus a plain BLOB table + in-process cosine similarity.
- **AI-provider boundary**: the brief's `propose_cluster_label` extension (step (c)) is
  explicitly the ONLY network-touching piece of the whole semantic-clustering vision that
  the brief judges safe to ship without a fresh design conversation — and this epic does
  NOT include it. Zero network calls anywhere in this epic.
- **Dependency-footprint tradeoff** (§2.8, §4 open questions): Apple-native (PyObjC, zero
  bundle cost, macOS-only) vs. cross-platform (bundled ONNX MiniLM ~90MB + Tesseract,
  works on Arch too) is flagged as needing "its own explicit statement in a design doc, not
  silent asymmetric feature availability discovered later." Resolved in design-discussion.md
  §3.
- **Offline-reliability gotcha** (`HF_HUB_OFFLINE=1` not fully reliable per a documented
  fastembed issue) only applies if the cross-platform ONNX/HF-ecosystem path is chosen — see
  design-discussion.md §3 for why this epic's dependency choice sidesteps it entirely.
- **Manual-correction/review UX** is called out as "likely the single largest scope item,
  larger than the ML pipeline itself" — this epic's H/V decomposition treats the review
  surface as a first-class slice, not an afterthought bolted onto the pipeline.

## What this epic explicitly does NOT re-derive

Face/pet clustering (§2.1), near-duplicate detection (§2.3), network/shared-drive scope
(§2.7, §3b), and the competitive landscape survey (§2.5) are all out of scope for this epic
per the brief's own §5 phasing recommendation — see epic.yaml's description for the full
out-of-scope list.

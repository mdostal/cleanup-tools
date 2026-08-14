# Phase 2 Research Brief: Person / Topic / Project Clustering for cleanup-tools

*Compiled 2026-08-14 from 8 parallel research agents (this session's model + real web search — not
literal Gemini, per the user's original request; noted as a substitution at dispatch time). Scope:
extending the shipped type-based `sort` feature into semantic clustering — "all photos of person X,"
"all documents about the house sale," "all files for Project Y" — potentially across network/shared
drives. This is exploratory research for a FUTURE epic, not yet planned or scoped as stories.*

---

## 1. Executive summary

Person-clustering for photos and topic-clustering for documents are both **buildable fully locally
today** on a modern Mac, using well-trodden, mature techniques (InsightFace/ArcFace embeddings +
HDBSCAN for faces; OCR + sentence-embeddings + HDBSCAN/BERTopic for documents), and every serious
open-source or Apple-native reference implementation (Apple Photos, PhotoPrism, digiKam, Immich,
paperless-ai) keeps a human-in-the-loop merge/rename/split step rather than claiming full automation
— this project's existing approval-queue pattern is exactly that shape already. The honest
constraints: (1) accuracy on messy real personal libraries (kids' faces over years, handwriting, poor
scans) never matches curated benchmark numbers, and manual correction is the realistic bar, not
zero-touch automation; (2) pet identification has no mature "just cluster automatically" tool — it's
few-shot reference-matching, not turnkey; (3) handwriting/degraded-scan OCR is where local tools
measurably lose to frontier cloud vision-LLMs, so a fully-local promise has one honest crack in it;
(4) network/cloud-synced locations change the cost model and correctness assumptions enough
(round-trip-dominated I/O, cloud-placeholder files that silently download on touch) that they deserve
a separate go/no-go decision, not a "works the same, just slower" assumption. The right architectural
move is a hybrid pipeline — cheap local embeddings do the clustering (near-zero cost,
seconds-to-minutes for a personal-scale library), and the existing narrowly-scoped AI-provider
exception is extended by exactly one method to *label* clusters from filenames only, never file
content — which keeps the whole design inside the trust boundary this codebase has already drawn and
had reviewed.

---

## 2. Per-topic findings

### 2.1 Face/pet clustering (photos)

Every tool in this space — commercial or open source — implements the same three-stage pipeline:
**detect** faces → **embed** into a fixed vector (128–512 dims) → **cluster** embeddings
(HDBSCAN/DBSCAN/Chinese Whispers) into identities, then a human names/merges/splits clusters.

**Libraries usable today, no cloud, on a Mac:**
- **InsightFace** ([github.com/deepinsight/insightface](https://github.com/deepinsight/insightface))
  — the modern de-facto standard: SCRFD detector + ArcFace-family embeddings (`buffalo_l`, 512-d,
  ~99.8% LFW), runs via `onnxruntime` with CoreML execution provider auto-enabling ANE/GPU
  acceleration on Apple Silicon
  ([CoreML EP docs](https://onnxruntime.ai/docs/execution-providers/CoreML-ExecutionProvider.html)).
  This is what Immich uses under the hood and what most 2025–2026 DIY face-clustering scripts
  converge on.
- **dlib / `face_recognition`** ([github.com/ageitgey/face_recognition](https://github.com/ageitgey/face_recognition))
  — 128-d ResNet embeddings, ships a ready-made **Chinese Whispers** clustering example
  ([dlib docs](https://dlib.net/face_clustering.py.html)) needing only one threshold parameter, less
  tuning than DBSCAN. Fine for a few thousand photos on CPU; noticeably weaker than ArcFace-class
  models on hard cases (profile faces, masks, kids' faces changing over years); wrapper has seen
  little maintenance since ~2021.
- **MediaPipe** ([Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker/python))
  — solid on-device detection/landmarks/alignment, explicitly "no round-trips, no user data sent to
  servers," but ships **no identity-recognition embedding** — pair with ArcFace/FaceNet for the
  actual recognition step.
- **mlx-uniface** ([github.com/CodeWithBehnam/mlx-uniface](https://github.com/CodeWithBehnam/mlx-uniface))
  — Apple-Silicon-native face pipeline on MLX for ANE-efficient inference; promising but young/thin
  community versus InsightFace+onnxruntime.

**Reference implementations worth studying (or just using directly):**
- **Apple Photos** ([Apple ML research](https://machinelearning.apple.com/research/recognizing-people-photos))
  — the best "what good looks like" reference: on-device DNN, face embedding <4ms on ANE, fused with
  **upper-body/clothing embeddings** so it still groups a person when their face is turned away,
  custom agglomerative clustering run periodically overnight. Closed-source; only reachable via the
  Vision framework (`VNDetectFaceRectanglesRequest`), not pip-installable.
- **PhotoPrism** — local TensorFlow FaceNet (512-d) + DBSCAN, distance threshold 0.60–0.70
  ([docs](https://docs.photoprism.app/developer-guide/vision/face-recognition/)). GPU acceleration
  only "planned"; quality generally regarded as behind Apple/Google's proprietary models.
- **digiKam** — rewrote 8.6/8.7 around **semi-supervised KNN+SVM** rather than pure unsupervised
  clustering: user seeds a few faces per person, tool propagates/suggests matches, rejecting a
  suggestion surfaces the next-best candidate
  ([8.6 release notes](https://www.digikam.org/news/2025-03-15-8.6.0_release_announcement/),
  [8.7](https://www.digikam.org/news/2025-06-30-8.7.0_release_announcement/)). Writes metadata to
  file XMP/IPTC, not locked in a proprietary DB — genuinely portable, most "batteries-included" free
  option if zero coding is the goal.
- **Immich** — separate Python ML microservice, InsightFace models via ONNX, DBSCAN-derived
  clustering into Postgres/pgvector "Person" entities
  ([docs](https://docs.immich.app/features/facial-recognition/)); most actively developed
  modern-stack option (90k+ stars). Community add-on **immich-pet-tagger**
  ([github](https://github.com/tedornitier/immich-pet-tagger)) extends the same embed-and-match
  pattern to pets via YOLO + CLIP against reference photos.

**Clustering algorithm choice:** HDBSCAN over cosine-distance ArcFace embeddings is the most
commonly recommended 2025-era default — handles variable cluster density (a person in 500 photos vs.
3) better than DBSCAN, gives per-point membership-probability as a UI confidence score. Chinese
Whispers is a reasonable simpler alternative already paired with dlib.

**Pets are a genuinely different problem.** Human face models don't transfer. State of the art
(AvitoTech CLIP/SigLIP2/DINOv2 animal-ID models trained on the 257k-individual PetFace dataset —
[HF](https://huggingface.co/AvitoTech/CLIP-ViT-base-for-animal-identification); MDPI dual-stream
CLIP-ViT cat/dog re-ID, Rank-1 0.974 — [paper](https://www.mdpi.com/2313-433X/12/8/354)) is few-shot
matching against reference photos you supply per pet (as immich-pet-tagger does), not zero-shot
automatic clustering. There is no mature "just cluster all my dog photos" packaged tool yet.

**Performance planning ballpark** (Apple Silicon, InsightFace+onnxruntime): detection+embedding is
the dominant cost; roughly 3–10 images/sec CPU-only, likely a few minutes for ~5,000 photos with
CoreML/ANE engaged, versus 10–30 minutes CPU-only. Clustering itself (HDBSCAN over a few thousand
512-d vectors) is seconds. Storage: ~300–500MB model weights one-time, embeddings themselves are
single-digit MB.

**Accuracy reality check:** ArcFace-class and Apple's proprietary model both exceed human-level LFW
accuracy (~99.8% vs. human ~97.5%), but LFW is curated and easy — real personal libraries (age
variation, low light, motion blur, profile shots, look-alike siblings) are harder than published
numbers suggest. Every serious tool resolves this with a human merge/split UI, not full automation —
that's the realistic bar.

**Privacy note:** all discussed tools run fully locally (explicitly documented for MediaPipe, Apple
Photos ML, PhotoPrism/digiKam/Immich's self-hosted design). This matters mainly if a tool is ever
distributed to process *other people's* photos — 2025 saw a wave of Illinois BIPA litigation
(Clearview AI $51.75M, Aura Frames $1.857M+) specifically targeting companies processing face
biometrics
([2025 BIPA year-in-review](https://www.privacyworld.blog/2025/12/2025-year-in-review-biometric-privacy-litigation/)).
For a single-user local tool over one's own library this is a non-issue legally, but it's exactly why
every serious open-source project here defaults to on-device processing.

---

### 2.2 OCR + topic clustering (documents)

**Local OCR options**, roughly fastest/simplest → most capable/heaviest:
- **Tesseract** (via **OCRmyPDF** for PDF text-layering) — CPU-only, fast (~450ms/page), best on
  clean printed scans; ~95% word-error-rate on handwriting, essentially unusable there
  ([OCRmyPDF](https://github.com/ocrmypdf/ocrmypdf)).
- **Apple Vision framework** (`VNRecognizeTextRequest`) — fully on-device, no network call, already
  installed on every Mac; system-wide **Live Text** and Preview/Finder search build on the same
  stack. Scriptable via `ocrmac` ([github](https://github.com/straussmaximilian/ocrmac)) or raw
  PyObjC. The best "free lunch" on macOS specifically.
- **PaddleOCR** (PP-OCRv5) — full detect+recognize+layout pipeline, 100+ languages, higher precision
  than Tesseract on layout-heavy docs but slower without GPU (~2.1s/page) and heavier to install
  ([github](https://github.com/PaddlePaddle/PaddleOCR)).
- **EasyOCR**, **docTR**, **Surya/Marker**
  ([datalab-to/surya](https://github.com/datalab-to/surya)) — Surya does OCR+layout+reading-order
  +tables in one pass, 87.2% on olmOCR-bench, arguably the best local option for turning scanned docs
  into clean structured text.
- **Local VLMs** (Qwen2.5-VL 7B via MLX-VLM on Apple Silicon) close much of the handwriting gap
  versus Tesseract/PaddleOCR but need several GB RAM and run slow on CPU (~3–8 tok/s).

**Where cloud genuinely wins:** multiple 2026 benchmarks agree frontier VLMs (GPT-5 ≈1.22% CER,
Claude Opus ≈1.31%, Gemini ≈1.44%) crush every local engine specifically on **handwriting and
degraded scans**, versus Tesseract's ~95% WER there
([handwriting OCR comparison](https://www.codesota.com/ocr/best-for-handwriting)). This is the one
place "fully local" for documents genuinely breaks down — old handwritten letters, faded receipts,
doctor's notes.

**Clustering extracted text by topic — two combinable approaches:**
- **Embeddings + unsupervised clustering (BERTopic pattern)**: embed docs (Sentence-BERT/local
  models) → **UMAP** dimensionality reduction → **HDBSCAN** clustering (auto-detects cluster count,
  flags outliers as noise instead of forcing a wrong bucket) → label clusters with class-based TF-IDF
  ([BERTopic overview](https://www.emergentmind.com/topics/bertopic)). This is the natural fit for
  "discover 'house sale,' 'tax documents,' 'Project X' clusters without predefined categories."
- **LLM classification/labeling**: a 2025 arXiv study found embeddings beat zero-shot LLM prompting
  by ~49.5% accuracy for multiclass document classification, at 10–575x lower cost
  ([arXiv:2504.04277](https://arxiv.org/pdf/2504.04277)). LLMs earn their keep **labeling cluster
  centroids after the fact** and triaging the HDBSCAN noise bucket — a hybrid gets higher recall than
  LLM-only at under a third of the cost ([ClusterFusion](https://arxiv.org/pdf/2512.04350)).
- Fully local embedding models exist and are competitive: **nomic-embed-text** (~0.3GB via Ollama),
  **Qwen3-Embedding** (first local family competitive with commercial APIs on MTEB as of mid-2025).
  Pair with Chroma/FAISS/LanceDB for local vector storage.
- **Apple's Foundation Models framework** (WWDC25+) exposes an on-device ~3B LLM directly to Swift
  apps for summarization/classification, no API key, no network — a legitimate native-Mac
  alternative to Ollama for the labeling step.

**Existing end-to-end tool worth knowing about:** **paperless-ngx** + **paperless-ai** add-on already
implements almost this exact pipeline in production, open source: OCR → poll new docs → local LLM
(Mistral/Llama3/Phi3 via Ollama) auto-suggests title/tags/type/correspondent, plus local embedding
generation for semantic search — a working reference implementation, not just a research direction
([paperless-ai + Ollama walkthrough](https://yanghu.github.io/posts/paperless-ai-setup/)).

**Practical architecture this research converges on:** OCR locally (Tesseract/Vision, VLM/cloud
fallback only for the handwritten/bad-scan subset) → embed locally (nomic-embed-text/Qwen3-Embedding)
→ UMAP+HDBSCAN to discover topic clusters unsupervised → small local LLM names each cluster and
triages noise. Stays 100% offline except the optional handwriting fallback.

---

### 2.3 Near-duplicate detection at scale (extending existing exact-dedupe)

The project's current `dedupe.py` (size-bucket → full SHA-256) is architecturally correct and
matches every serious tool's stage 1–3 — the gap is **near-duplicates** (same photo re-encoded, same
document resaved/lightly edited), not exact matching.

**Images — perceptual hashing:**
- **aHash/dHash/pHash/wHash** — fixed-length hash, small edits move Hamming distance only a little.
  pHash (DCT-based) is most robust to recompression/color shifts; digiKam uses wHash (Haar wavelet)
  internally with a `similarity.db` sidecar requiring an explicit fingerprint-build step.
- **imagededup** ([idealo/imagededup](https://github.com/idealo/imagededup)) — purpose-built library
  offering all four hash methods plus CNN embeddings; published benchmarks (AWS r5.xlarge, CPU-only)
  show hashing is **4–10x faster than CNN** for the same corpus (~18–112 sec vs. ~191–397 sec across
  5k–10.8k images), but CNN catches genuine transforms (rotated/cropped) that hashes miss
  ([benchmarks](https://idealo.github.io/imagededup/user_guide/benchmarks/)).
- **Scaling gotcha**: naive all-pairs Hamming comparison is O(N²) — fine at a few thousand images,
  painful past ~50–100k. czkawka's real solution is a **BK-tree** (leverages Hamming distance's
  triangle inequality to prune search) rather than pairwise comparison
  ([BK-tree](https://en.wikipedia.org/wiki/BK-tree)).
- Sensible distance banding (64-bit hash): 0 = identical, 0–5 = same image resaved, 6–10 = minor
  edit/crop, 11–20 = loose/related, worth human glance before auto-action.

**Documents — fuzzy hashing:**
- **ssdeep** (Context-Triggered Piecewise Hashing) — de facto standard for "this file was
  edited/resaved," compact signature diffed for a similarity score
  ([ssdeep-project](https://ssdeep-project.github.io/ssdeep/index.html)); pure-Python fallback
  **ppdeep**.
- **TLSH** — more robust than ssdeep on larger/binary files, higher compute cost.
- **MinHash+LSH** (Jaccard similarity over shingles) is the standard for large-corpus text dedup
  (used in LLM training-data pipelines) but is overkill at personal-collection scale unless there's
  specifically lots of reordered/reformatted text content; ssdeep/TLSH is the more directly useful
  primitive for "same document, re-saved."

**Recommendation for this codebase:** keep the exact-match SHA-256 stage as-is; add an image
near-dup pass (dHash/pHash via `imagehash` or `imagededup`) as a *separate* stage, grouped via
BK-tree or bucket+threshold rather than all-pairs once past a few thousand images; surface a
**distance threshold, not binary yes/no**, mirroring czkawka's slider and this project's existing
"candidate group + human confirms" philosophy. Skip CNN embeddings — the accuracy gain doesn't
justify 4–10x slowdown for the common "same photo saved 3 places" case.

---

### 2.4 Metadata / OS-level signals (zero content analysis)

macOS provides an unusually rich pre-computed signal set, queryable without opening/parsing file
bytes semantically:

- **Spotlight (`mdls`/`mdfind`/`NSMetadataQuery`)** — `kMDItemContentTypeTree` gives full UTI
  ancestry for type-without-extension classification; `kMDItemLastUsedDate`/`kMDItemUseCount` (from
  Launch Services) are a strong "is this file actually still used" signal directly relevant to
  `reclaim`-style logic; `kMDItemDownloadedDate`/`kMDItemWhereFroms` give download provenance.
- **EXIF/IPTC/XMP via ExifTool** ([exiftool commands](https://ninedegreesbelow.com/photography/exiftool-commands.html))
  or **Exiv2** — cross-platform, reads GPS/camera/date metadata for bucketing by date/location
  without touching pixels. **phockup** ([github](https://github.com/ivandokov/phockup)) is a ready
  reference implementation of exactly this ("sort into year/month/day, dedupe by checksum on
  collision").
- **Finder tags/comments** are just extended attributes
  (`com.apple.metadata:_kMDItemUserTags`), fully readable/settable without Finder via `xattr` or the
  **`tag`** CLI ([jdberry/tag](https://github.com/jdberry/tag)) or **`osxmetadata`** Python library.
- **Provenance**: `com.apple.quarantine` xattr tells you exactly when/how a file arrived
  (Safari/Chrome/Mail, timestamp) — useful for staleness heuristics on Downloads-folder clutter.
- **`osxphotos`** ([RhetTbull/osxphotos](https://github.com/RhetTbull/osxphotos)) reads Photos.app's
  *already-computed* person/album/keyword tags directly from its SQLite database — a way to get
  Apple's face-clustering results for free without reimplementing the ML, if the user already uses
  Photos.app.
- **Filesystem birth time** (`st_birthtime` on APFS, distinct from mtime/atime) cleanly separates
  "created" from "last touched."

Linux/Arch has weaker but real parallels: ExifTool/Exiv2/ffprobe identically; a looser tagging layer
(KDE Baloo, GVfs metadata, the semi-adopted freedesktop `user.xdg.*` xattr convention); less reliable
birth-time support depending on kernel/filesystem.

**Bottom line**: metadata-only signals are a cheap, zero-risk complement to content-based clustering
— they can pre-filter or corroborate clusters (e.g., GPS+date proximity supporting a "same trip"
grouping) without any ML at all, and Hazel's rule-engine model (match on any Spotlight attribute →
move/rename/tag) is a good reference for a rules DSL layer.

---

### 2.5 Existing consumer/competitive landscape

Surveying Apple Photos, Google Photos, PhotoPrism, digiKam, Immich, Mylio, Excire (photo-scoped);
Hazel, TagSpaces (rule-based, any file type, no ML); DEVONthink, paperless-ngx (document-scoped); and
the 2025–2026 crop of "AI-native" organizers (Sortio, Renamer.ai, Filex AI, The Drive AI) surfaces a
clear gap:

1. **No tool spans both photos and everything else.** Face-clustering ML is siloed to photo
   libraries; document tools are siloed to text-bearing files; rule engines touch any file type but
   only via user-authored rules, never ML clustering. Nobody does unified person/topic/event
   clustering across a whole heterogeneous computer in one pass.
2. **"Person" clustering is solved-ish only for faces in photos** — cross-modal identity linking (a
   name in a PDF ↔ a face in a photo) doesn't exist in any surveyed consumer tool.
3. **"Topic" clustering for non-photo files is new (2025–2026) and LLM-summarization-based**, with
   uneven privacy defaults — the newest entrants (Sortio, Filex AI) default to cloud processing
   unless explicitly toggled to local.
4. **Local-first options exist across the stack but require self-hosting/technical assembly**
   (PhotoPrism, digiKam, Immich, paperless-ngx, TagSpaces) — no single self-hosted product unifies
   photos + documents + generic files.
5. **Recurring failure modes** across every tool checked: (a) identity-merge errors in face
   clustering at scale (lighting/angle false-splits, look-alike-relative false-merges), and (b)
   OCR/content-matching brittleness on poorly scanned or unusually laid-out documents.

This is a genuinely unfilled niche, and it's the one `cleanup-tools` is positioned to fill given its
existing local-first survey/sort/dedupe/find-wallets foundation.

---

### 2.6 Embeddings vs. direct LLM classification (cost/latency/accuracy)

For organizing thousands of personal files, **local embeddings should do ~80–95% of the work**, LLM
calls reserved for cluster labeling plus a small ambiguous tail.

**Cost at 10,000 files** (≈300 tokens context/file): embeddings-only (OpenAI
`text-embedding-3-small`) ≈ **$0.06** total one-time, or **$0** with a local model; naive
one-LLM-call-per-file classification ≈ **$0.57–$15+** depending on model, scaling linearly
(and realistically 3–5x higher once real prompts include taxonomy/examples) — at 100k files this is
$6–150+ vs. embeddings staying under $1.

**Latency is the more decisive gap**, not dollars: local `all-MiniLM-L6-v2` embeds 10,000 files in
well under a minute on CPU; direct per-file LLM calls run ~0.5–3s each — sequentially, 10,000 files ≈
4+ hours; batch APIs cut cost ~50% but add up to 24h async turnaround.

**Accuracy**: a March 2026 benchmark (BTZSC, [arXiv:2603.11991](https://arxiv.org/abs/2603.11991))
across 22 datasets/38 models found strong embedding models offer the best accuracy/latency trade-off
overall; instruction-tuned LLMs are competitive but not categorically better, at vastly higher cost
per unit accuracy. Embeddings are very good at "does this belong in the same semantic neighborhood"
but genuinely weak at multi-hop reasoning ("this PDF is a lease renewal because it mentions a
landlord's name from three other files") — exactly the gap LLM-assisted labeling closes.

**Local models get most of the way there with zero network calls**: `sentence-transformers`
(`all-MiniLM-L6-v2`, or higher-quality `BGE-M3`/`gte-large-en-v1.5`) for text; **CLIP**/`open_clip`
for zero-shot image classification against candidate labels, fully offline. Open models are now
competitive with API models on MTEB (Jina v5-text-small 71.7 vs. OpenAI `text-embedding-3-large`
64.6).

**Recommended hybrid pipeline**: extract signal per file → embed locally (cache by content hash, so
re-runs are incremental) → UMAP+HDBSCAN cluster → **one LLM call per cluster** (not per file) to name
it from 5–10 representative filenames → route only HDBSCAN-flagged outliers/low-confidence files to
an explicit per-file LLM call. This makes LLM call volume scale with **cluster count**, not file
count — typically 1–2 orders of magnitude cheaper/faster than per-file classification, while keeping
real reasoning where it's actually needed.

---

### 2.7 Network/shared-drive scope

*(See §3b below — this angle is deliberately surfaced as its own decision point, not folded into
the general architecture.)*

Key findings, condensed:
- **Metadata operations, not bulk transfer, dominate cost** over NFS/SMB — `stat`/`readdir`/`open`
  are round-trips that can cost as much as reading megabytes locally
  ([MIT NFS perf guide](https://tig.csail.mit.edu/data-storage/nfs/nfs-performance/);
  [NetApp READDIR latency KB](https://kb.netapp.com/onprem/ontap/hardware/High_READDIR_latency_on_NFS_or_CIFS_workload)).
  Real benchmark: on a 1TB NAS snapshot, some dedupe tools exceeded a 15-minute cutoff while
  `fclones` (parallelized, I/O-order-aware) finished in under a minute
  ([DuoBolt NAS notes](https://duobolt.app/nas/)).
- **FSEvents (file-watching) doesn't work reliably on network volumes** — any "watch this folder"
  feature must fall back to polling for network roots, which reintroduces the same expensive-walk
  cost, recurring.
- **Spotlight/`mdfind` doesn't index network volumes by design** ("good network citizen" rationale) —
  a network-aware `find`/wallet-finder can't lean on it and needs its own indexed walk.
- **Cloud-placeholder files are the sharpest correctness risk**, and this applies **today, not just
  to a hypothetical future network feature**: on macOS with iCloud "Optimize Mac Storage" enabled,
  evicted files appear as `.name.ext.icloud` (dot-prefixed, wrong visible extension) — a naive
  glob/extension classifier misfiles or skips them, and worse, a naive `stat`/hash/OCR pass
  **silently forces a full download** of a file that was never actually resident locally. The
  correct check is `NSURLUbiquitousItemDownloadingStatusKey` before any read; explicit download
  requires `startDownloadingUbiquitousItemAtURL:`. This is a real bug in the *current* local-only
  tool, not a new one introduced by network support.
- Since macOS 11, Dropbox/Google Drive/OneDrive/Box are all on Apple's unified **File Provider**
  framework — a single vendor-neutral API surface can answer "is this file materialized" for any of
  them, rather than special-casing each vendor.
- **Per-component impact if network roots are added**: `survey`/`sort` need batched listings +
  cached mtime/size instead of many discrete stats; `dedupe` needs a cheap partial-hash prefilter
  before full-hash (reading = downloading, over the wire); `reclaim` needs a policy gap closed —
  deleting from a *shared* NAS or cloud-synced folder can affect other machines/users, so it should
  refuse or require extra confirmation by default; `find`/wallet-finder is the sharpest case, since
  content-scanning for private-key material would force-materialize cloud placeholders just to grep
  them, directly conflicting with "never touch content you don't need to."

---

### 2.8 Local-first architecture constraints (grounded in this codebase)

Read directly from this repo: `src/cleanup_tools/ai/{base,wiring,anthropic_provider}.py`,
`.pHive/CONTEXT.md`, `docs/REQUIREMENTS.md`, `docs/CLEANUP-PLAN.md`, `queue.py`, `adapters/base.py`.

Three existing patterns should govern phase 2's shape rather than being redesigned around:

1. **The AI-provider exception is scoped by contract, not convenience.**
   `AIProvider.propose_bucket(filename, metadata)` sends filename + shallow metadata only, never
   content, never images — enforced by slicing the candidate list *before* any call, never filtering
   after. `AnthropicProvider` was only shipped after grepping the installed SDK for phone-home
   behavior and pinning `max_retries=0`.
2. **Everything destructive/uncertain lands in the approval queue** via `queue.stage_entries()`
   with a `source` tag — AI-sourced entries get zero special-casing downstream because they flow
   through the same function as manual planning.
3. **OS capability gaps are explicit `NotImplementedError`, not silent degradation** — e.g.
   `OSAdapter.set_screenshot_save_location` raises on Arch, documented plainly in the PKGBUILD.

**What can stay fully local on macOS, at genuinely zero bundling/network cost:**
`NLContextualEmbedding` (NaturalLanguage framework) — a real on-device BERT-like sentence embedder,
already shipped in the OS, "works without network connectivity," reachable from Python via PyObjC
(`pyobjc-framework-NaturalLanguage`). `VNRecognizeTextRequest` (Vision) for on-device OCR, same
PyObjC path. Both avoid the model-bundling/PyInstaller-size problem entirely on the primary platform.
Cross-platform (Arch) fallback: Tesseract for OCR, a bundled ONNX MiniLM (~90MB) for embeddings —
same size-vs-portability tradeoff the project already navigated once for its PyInstaller build.
**Local vector storage**: `sqlite-vec` — no server, embeds directly in a local `.db` file, fits the
project's existing "local files as the entire persistence layer" pattern precisely.

**One concrete, load-bearing gotcha**: Hugging-Hub-integrated loaders (`transformers`, most
`sentence-transformers` paths) check the Hub for model updates on load by default unless
`local_files_only=True`/`HF_HUB_OFFLINE=1` is set — and even that flag isn't fully reliable in every
reported case (an open `fastembed` issue shows `HF_HUB_OFFLINE=1` still triggering a GCS download —
[issue #615](https://github.com/qdrant/fastembed/issues/615)). Any ONNX/sentence-transformers backend
needs the identical audit discipline `anthropic_provider.py` already applied to the Anthropic SDK:
vendor weights as static files, use loader code that never calls anything Hub-aware, add a regression
test that fails loudly on drift.

**Recommended extension to the AI-provider layer** — additive, narrow, not a widening: add
`propose_cluster_label(sample_filenames, cluster_metadata) -> ProposalResult` to the `AIProvider`
ABC. Local clustering runs first and produces the candidate list; the existing cap slices it; results
stage into the queue via the same `stage_entries()` with `group_key=f"semantic:{cluster_id}"`. This
makes the AI call **cheaper and rarer** than today's per-file `propose_bucket` (O(clusters) vs.
O(files)) and strictly narrower in what it sees (only filenames that were already going to surface in
the approvals UI).

**Explicitly out of scope without a new, separately-gated decision:** sending OCR'd text or images to
the cloud provider (a materially larger exception than filenames — the model would see quotes from
private documents, not just names); authoritative entity resolution (semantic clustering gives
similarity-based *suggestions*, always queue-staged, never auto-executed, same as today);
ambient/background indexing (should be an explicit user-triggered action with visible cost, like
`reclaim`'s job pattern, not a silent startup task).

---

## 3. Recommended architecture

Given the constraints above, a new `src/cleanup_tools/semantic/` package, structurally parallel to
`ai/` and `adapters/`:

| Module | Responsibility | Local implementation | Notes |
|---|---|---|---|
| `embeddings.py` | Text embedding | `AppleNLEmbedder` (PyObjC → `NLContextualEmbedding`, macOS, zero bundle cost) / `OnnxEmbedder` (bundled MiniLM, cross-platform, ~90MB) | ABC + factory, same shape as `OSAdapter` |
| `ocr.py` | Text extraction from images/scans | `AppleVisionOCR` / `TesseractOCR` | Feeds extracted *text* into the same pipeline PDFs use — image bytes never leave this module |
| `faces.py` | Face detection + embedding (photos only) | InsightFace (ONNX + CoreML EP) or Apple Vision `VNDetectFaceRectanglesRequest` | Separate from `embeddings.py` — different domain, different model family |
| `index.py` | Vector cache | `sqlite-vec`, keyed by content hash (reuse `queue.py`'s existing capped hash helper for staleness, not as a duplicate-detection claim) | Sits next to `config.yaml`/`approval_queue.yaml` in `~/.config/cleanup-tools/` |
| `cluster.py` | Grouping | HDBSCAN (or simple nearest-neighbor threshold grouping — avoid pulling in scikit-learn as a new heavy dep if it can be avoided) | Pure local, produces candidate groups, no AI call |

**What stays fully local, no exception needed:**
- Face detection + embedding + clustering (photos/pets)
- OCR of printed/typed text (Vision/Tesseract)
- Document embedding + topic clustering (HDBSCAN/BERTopic pattern)
- Metadata-signal enrichment (EXIF/GPS/dates/Finder tags/quarantine provenance) — zero ML at all
- The entire review/merge/split UI

**What would need an explicit, user-gated network call — and exactly how narrow it should be:**
- **Cluster labeling** (`propose_cluster_label`): sends a handful of *filenames* per cluster to the
  existing Anthropic provider, capped, tagged with distinct provenance (`ai:anthropic:cluster` vs.
  `ai:anthropic:bucket`), staged into the same approval queue. This is the only network-touching
  piece that should ship without a fresh explicit design conversation, because it's a mechanical
  extension of an already-reviewed, already-scoped contract.
- **Handwriting/degraded-scan OCR fallback** (cloud vision-LLM call): genuinely improves accuracy
  where local OCR fails badly, but this is a **different kind of exception** than anything shipped
  today — it would transmit extracted text or image content, not filenames. This must be a
  separately named, separately gated, separately documented feature with its own explicit UI copy,
  not a silent upgrade path for the OCR module.

**Honest "can't stay local at reasonable accuracy/cost" flag:** face clustering *can* stay fully
local at good accuracy (InsightFace/ArcFace is genuinely strong) — this is not one of the pieces that
fundamentally requires cloud. The one piece where the literature is unambiguous that cloud
meaningfully wins is **handwriting/degraded-scan OCR** (single-digit % error for frontier VLMs vs.
near-total failure for Tesseract). Pet identification is not a "needs cloud" problem so much as a "no
mature turnkey tool exists yet, local or cloud" problem — it requires reference photos either way.

---

## 3b. Network/shared-drive scope — a distinct decision point

This should **not** be assumed as an incremental "phase 2.5" extension of local clustering; it
changes the cost model and the correctness contract enough to warrant its own explicit scoping
decision before any work starts. Concretely:

1. **Performance**: every per-file operation becomes a round-trip. `survey`/`sort` need to switch
   from per-file `stat` calls to batched listings with caching; `dedupe`'s hash stage needs a cheap
   partial-hash prefilter before full reads, since a full-file hash now means reading the whole file
   over the wire.
2. **File-watching breaks**: FSEvents doesn't reliably cover network volumes — any future "watch and
   auto-sort" feature must explicitly fall back to polling for network roots, not silently degrade.
3. **Spotlight can't be relied on**: `find`/wallet-finder needs its own indexed walk for network
   roots rather than leaning on `mdfind`, which doesn't index network volumes by design.
4. **Cloud-placeholder correctness is a *pre-existing* risk, not a new one**: any current local-only
   scan over `~/Documents` on a machine with iCloud "Optimize Mac Storage" enabled can already
   silently force-download evicted files. This should probably be fixed regardless of whether
   network-drive scope is ever pursued — it's a correctness bug in the shipped tool today, surfaced
   by this research rather than created by it.
5. **Shared-drive semantics change the safety model**: `reclaim`'s "delete regenerable junk" logic
   assumes the machine running the tool is the sole stakeholder. On a shared NAS, what's regenerable
   for one machine may be live for another, and a delete on a cloud-synced folder propagates
   everywhere. This needs an explicit policy (refuse by default on detected network/cloud-synced
   roots, or require extra confirmation), not silent reuse of local-mode defaults.

**Recommendation**: treat network/shared-drive scope as an explicit go/no-go decision for a *future*
epic, separate from the core photo/document clustering work, and fix the iCloud-placeholder
correctness gap in the existing local-only tool independent of that decision.

---

## 4. Open questions and risks

- **Where is the line between "cluster label call" and "content-aware call"?** The research is clear
  that filename-only labeling fits today's trust boundary and OCR-text-to-cloud does not — but the
  exact policy for in-between cases (e.g., sending a short document *title* extracted via local OCR,
  vs. full extracted text) needs an explicit decision, written before code, per the project's own
  convention in `.pHive/CONTEXT.md`.
- **Manual-correction UX is unsolved in this research.** Every reference tool (Apple Photos, digiKam,
  PhotoPrism, Immich) needs a merge/rename/split review flow for face clusters, and BERTopic-style
  noise buckets need a triage flow for documents — neither has a designed UI in this codebase yet,
  and this is likely the single largest scope item in any future epic, larger than the ML pipeline
  itself.
- **Cross-modal identity linking** ("this PDF and this photo are about the same person/event") does
  not exist in any surveyed tool and is explicitly out of scope for a first pass — worth flagging so
  it isn't assumed as an implicit phase-2 requirement.
- **Pet clustering** has no mature zero-shot solution; if pets are in scope, the design needs to
  commit to few-shot reference-photo matching (immich-pet-tagger pattern) as the realistic approach,
  not "the same as faces."
- **Dependency footprint decision**: InsightFace+onnxruntime vs. Apple-native Vision/NaturalLanguage
  frameworks is a real tradeoff between cross-platform parity (Arch support) and
  zero-bundle-cost/best-quality on the primary platform. This mirrors the
  `set_screenshot_save_location` precedent but needs its own explicit statement in a design doc, not
  silent asymmetric feature availability discovered later.
- **Performance/UX for "build the index" as a first-run cost**: indexing a large personal library
  (tens of thousands of photos/documents) is a multi-minute-to-tens-of-minutes background job; needs
  the existing `jobs.py` background-job + progress-poll pattern, plus a clear, visible cost estimate
  before the user commits to running it.
- **Offline-mode reliability of any bundled ONNX/HF-ecosystem model** is not guaranteed by the
  library's stated flag alone (`HF_HUB_OFFLINE=1` has a documented failure case) — needs the same
  grep-and-test audit discipline already applied to the Anthropic SDK, as its own concrete
  engineering task, not assumed done.
- **Cloud-placeholder correctness bug in the existing local-only tool** (§3b point 4) should probably
  be triaged and possibly fixed independent of any phase-2 decision, since it's a present-day risk,
  not a future one.

---

## 5. Phasing recommendation

**Recommended order: documents-by-topic before photos-by-person**, with local-only primitives (no AI
call at all) preceding any AI-provider extension in both cases. Reasoning:

1. **Documents-by-topic has a more mature, lower-risk hybrid pattern with a working reference
   implementation already in production** (paperless-ngx + paperless-ai) and clearer accuracy
   tradeoffs (BTZSC benchmark directly quantifies embeddings-vs-LLM for this exact task).
   Photo/face clustering, while technically mature, carries a *harder, more consequential*
   review-UX problem — misclustered people are more emotionally sensitive to get wrong than a
   misclustered PDF, and every reference tool (Apple Photos especially) is called out in this
   research for exactly this failure mode (parent/child merges, no clean undo at scale).
2. **Documents reuse more of what's already been built.** This codebase already has content-hash
   infrastructure (`queue.py`), an approval-queue pattern proven for exactly this "propose, don't
   auto-act" shape, and the AI-provider exception is already scoped around filenames+metadata for
   buckets — extending it to `propose_cluster_label` for document clusters is a smaller conceptual
   leap than introducing an entirely new face-detection/embedding subsystem.
3. **Documents avoid the pet problem entirely** — no analogous "no mature tool exists" gap for
   documents; OCR+embeddings+HDBSCAN is comfortably in "usable today" territory across the board,
   with only the handwriting tail needing special handling (and that tail can simply be
   deferred/flagged rather than blocking the whole feature).
4. **Within documents-by-topic**, the natural build order (mirroring the local-first-architecture-
   constraints report's suggested sequence) is: (a) local OCR + embedding + indexing as a standalone
   capability with its own value (semantic search, useful for `find`/wallet-finder too) — zero
   AI-provider involvement, zero new trust-boundary questions; (b) local clustering surfaced as new
   queue-staged proposals with `source="local:cluster"` — still no AI call; (c) only then, optionally,
   the narrow `propose_cluster_label` extension for users who want cloud-assisted cluster naming.
5. **Photo/face clustering should follow as a second phase**, reusing the same
   `index.py`/`cluster.py` infrastructure (different embedder, same clustering shape), once the
   review/merge/split UI pattern has been designed and validated once already on the lower-stakes
   document case.
6. **Network/shared-drive scope and pet clustering are explicitly out of the first two phases** —
   both are separable decisions (§3b) or immature techniques (pets) that shouldn't gate or complicate
   the initial local person/topic clustering work.

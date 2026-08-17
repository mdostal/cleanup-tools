# Design Discussion: document-topic-clustering

## 0. Prelude

No prior KG decisions or north_star block to cite (this project doesn't run the full Hive
metrics/north_star machinery — see `.pHive/CONTEXT.md`). Builds directly on
`.pHive/research/semantic-desktop-organization.md` and this epic's own
`docs/research-brief.md`.

## 1. Goal

Let a user say, in effect, "group my documents by what they're actually about" and get back
real, reviewable proposals — entirely offline, entirely local, using the exact same
approval-queue mechanism every other pipeline (`sort`/`reclaim`/`corral-screenshots`/the
chat agent's `propose_moves`) already uses. This is phase 1 of the semantic-clustering
vision: documents only (text-bearing files + OCR'able images/scans), no photos/faces, no
network calls anywhere, no new kind of approval flow.

## 2. Proposed approach

### 2.1 The core insight: clustering is just a location-aware bucket, one more time

Every existing pipeline in this app follows the same shape: compute a `bucket` name for a
file, propose `action="move"` to `<location>/_sorted/<bucket>/<filename>`, tag
`group_key=f"sort:{location}:{bucket}"`, stage via `queue.stage_entries()`. `sort.py`'s
bucket comes from extension rules; a semantic cluster's "bucket" is instead a cluster label
discovered from content similarity. Structurally these are identical. So this epic does
**not** invent a new proposal shape or a new review flow — it proposes
`action="move"` to `<location>/_clusters/<cluster-label>/<filename>`, and extends
`queue.parse_group_key`/`queue.group_entries_hierarchical` (already relocated to `queue.py`
this session) to recognize a new `"cluster"` pipeline prefix alongside `"sort"`/`"reclaim"`/
`"corral-screenshots"`, with the identical 3-segment `cluster:<location>:<cluster-slug>`
scheme. A cluster proposal shows up in the **existing** dashboard tree and Review Queue,
approved/rejected/undone through the **existing** bulk and per-entry routes, with zero new
approval code.

This directly answers the "what UI surface" question the planning brief raised: **no new
page**. The dashboard tree and queue view already render anything shaped like a
`QueueEntry` with a `group_key`. What genuinely IS new (see §2.5) is (a) the trigger to run
the pipeline, and (b) a way to see *why* files were grouped, since — unlike a `.pdf`
extension — cluster membership isn't self-evident from a filename alone.

**Filename collisions under a flat `_clusters/<slug>/` dest (grill T1).** Because the scan
is recursive (see §2.2), two files named e.g. `notes.pdf` from different subfolders can
land in the SAME flat `_clusters/<slug>/` destination — a real collision `sort._plan`'s
shallow, single-namespace scan never has to handle. `pipeline.py` disambiguates
deterministically: if a candidate `dest` already exists on disk, or is already claimed by
another entry proposed in this same run, the filename gets a short suffix derived from its
original parent directory appended before the extension (e.g. `notes__Taxes-2023.pdf`)
before it's ever staged. Either way, the existing `dest_exists` signal on the `QueueEntry`
(the same one `sort`/`reclaim` already populate) still tells a reviewer at approval time
whether the computed destination is genuinely free — this never silently overwrites
anything.

### 2.2 New package: `src/cleanup_tools/semantic/`

Structurally parallel to `ai/` and `adapters/` — an ABC + factory pattern for the one part
that's genuinely platform-specific (embedding/OCR), pure Python everywhere else.

| Module | Responsibility |
|---|---|
| `embeddings.py` | `TextEmbedder` ABC + `AppleTextEmbedder` (the only concrete impl this phase) + `get_embedder(adapter)` factory |
| `extract.py` | `extract_text(path) -> str \| None` — PDFKit text layer for `.pdf` (OCR fallback per-page only if the text layer is empty/near-empty), Vision OCR for image files, plain read for `.txt`/`.md`. Never raises; returns `None` on anything it can't handle. |
| `index.py` | Plain SQLite (stdlib `sqlite3`, not `sqlite-vec` — see §3) at `~/.config/cleanup-tools/semantic_index.sqlite3`, keyed by content hash, incremental |
| `cluster.py` | Cosine-similarity threshold + union-find grouping (no HDBSCAN/scikit-learn — see §3), plus a small local (non-AI) keyword labeler |
| `pipeline.py` | Orchestrates extract → embed → index → cluster → stage, mirroring `sort._plan()`/`sort.run()`'s dry-run-then-stage shape |

**Scan scope: recursive, not shallow (grill H1).** `sort._plan`'s existing scan is
deliberately shallow (`adapter.list_dir(target_dir, max_depth=0)`) because it's clearing
loose top-level Downloads clutter. Documents worth topic-clustering overwhelmingly live
NESTED (`~/Documents/Taxes/2023/`, `~/Documents/House Sale/`, ...) — a shallow scan would
make this feature nearly useless for its actual purpose. `semantic/pipeline.py`'s walk is
recursive under each configured location, subject to the same protected-path
(`queue.is_protected_path`) and iCloud-placeholder (§2.6) guards at every depth, not just
the top level.

**Content-hash: cache key only, never a cap on what gets embedded (grill T2).** The
content-hash reused from `queue.build_plan_snapshot`'s contract (capped-prefix hash,
staleness-signal only, "not a duplicate-detection claim") is used PURELY as
`semantic/index.py`'s incremental-reindex cache key — "has this exact content already been
embedded, skip if so." It is NOT applied to the text handed to the embedder:
`extract_text()`'s FULL output (whatever length) is what gets embedded, never truncated to
the hash's 8MiB cap. These are two independent uses of "hash the content," and conflating
them would silently degrade embedding quality for the rare long document for zero benefit.

### 2.3 Dependency-footprint decision: Apple-native only, phase 1

**Decision: this epic ships macOS-only, via PyObjC bindings to already-installed OS
frameworks (`NaturalLanguage`, `Vision`, `Quartz`/PDFKit) — zero bundled model weights, zero
bytes of bundle-size cost. On any non-macOS adapter (Arch), `semantic.embeddings.get_embedder()`
and `semantic.extract.extract_text()` raise `NotImplementedError`, identically to
`OSAdapter.set_screenshot_save_location`'s existing precedent.**

Why, over the cross-platform ONNX+Tesseract alternative the research brief also surveyed:

- This project's actual desktop target (the PyInstaller/Tauri-wrapped app) is macOS. Zero
  bundle-size cost beats ~90MB of bundled model weights for the platform that matters most.
- It sidesteps the research brief's own flagged offline-reliability gotcha entirely —
  `HF_HUB_OFFLINE=1` unreliability is a property of Hugging-Face-ecosystem loaders
  (`transformers`/`sentence-transformers`), which this decision never touches. There is
  nothing to audit-and-pin here the way `ai/anthropic_provider.py` had to for the Anthropic
  SDK, because there's no third-party model-loading code in the request path at all — every
  call goes straight into an Apple framework that ships with the OS.
- It avoids a second new heavy runtime dependency tree (`onnxruntime` + `Pillow`-adjacent
  image preprocessing + a bundled model) on top of the genuinely new one this epic already
  introduces (PyObjC).
- The real cost is honest and stated plainly: **Arch support for this feature does not
  exist in phase 1.** This mirrors `set_screenshot_save_location`'s already-shipped,
  already-accepted precedent — an explicit `NotImplementedError`, documented in the adapter
  and in `README.md`'s platform-support notes, not a silent gap discovered later. If
  cross-platform document clustering becomes a real requirement, it's a separate,
  explicitly-scoped follow-up epic (bundled ONNX path), not a retrofit onto this one.

**Verified before writing this doc**: `pyobjc-framework-NaturalLanguage`,
`pyobjc-framework-Vision`, and `pyobjc-framework-Quartz` are real, actively maintained
packages on PyPI (latest 12.2.2, matching current PyObjC releases) — confirmed via
`pip index versions` during planning. None are installed in this dev environment yet;
installing and confirming they actually import/call successfully on this machine is the
concrete first step of story 1, not an assumption baked into this doc.

New runtime dependencies (macOS only, added as a `semantic` extras group in
`pyproject.toml` — mirrors the existing `build` extras precedent so Arch installs never
pull them): `pyobjc-framework-NaturalLanguage`, `pyobjc-framework-Vision`,
`pyobjc-framework-Quartz`. `numpy` (cosine-similarity math) is already confirmed available
in this environment; it becomes a base runtime dependency since `semantic/cluster.py`'s
math is pure Python+numpy and platform-independent even though the embedding INPUT to it is
Mac-only for now.

### 2.4 Clustering: threshold + union-find, not HDBSCAN

Per the research brief's own explicit steer ("avoid pulling in scikit-learn as a new heavy
dep if it can be avoided") and this being a personal-library, thousands-not-millions scale
(brief §2.6: HDBSCAN over a few thousand vectors is "seconds" either way): build a
cosine-similarity graph over all indexed embeddings (brute-force O(N²) pairwise, numpy
vectorized — fine at this scale per the brief's own performance ballpark; a BK-tree-style
optimization is explicitly out of scope until real usage shows it's needed), connect any
pair above a configurable similarity threshold (default 0.75, a new `Config.semantic_cluster_threshold`
field, persisted like `chat_turn_cap`), then take connected components as clusters via
union-find. Singleton "clusters" (no file similar enough to any other) are simply never
proposed — they stay wherever they already are, exactly like `sort`'s `"other"` bucket
files that don't match a rule.

**Cluster labeling is local, not AI.** Per this epic's explicit no-network-calls
constraint: a cluster's label is the single most common non-stopword token across its
member files' extracted text (a plain word-frequency count against a small hardcoded
English stopword list — no new dependency), falling back to a generic `"cluster-<n>"` slug
if no token clears a minimum-frequency bar. This is a real, if unglamorous, local heuristic
— NOT the research brief's `propose_cluster_label` AI extension, which stays explicitly
out of scope (see epic.yaml). A human renames/corrects the label at review time by simply
editing the proposal's dest path before approving, or rejecting outliers individually —
reusing the existing per-entry edit primitive (`queue.edit_entry`) rather than inventing a
cluster-rename UI.

**Label vs. slug (grill V1).** The human-readable **label** (whatever the word-frequency
heuristic above produces, e.g. `"invoice"`) and the **slug** used as an actual directory
name in `dest` are related but distinct: the slug is a deterministic filesystem-safe
transform of the label (lowercased, non-alphanumeric runs collapsed to a single `-`, capped
to a reasonable length). The label — not the slug — is what's shown in the queue
disclosure (§2.5); the slug only ever appears inside a real path.

### 2.5 What's genuinely new in the UI (small, additive, not a new page)

1. **A new "Plan: Cluster Documents" trigger** — nav link + background job, structurally
   identical to `plan_sort`/`plan_reclaim`/`plan_corral_screenshots` (same `jobs.start_job`
   pattern, same `/status/<job_id>` poll, same redirect-with-`staged=N`). Scans the SAME
   configured locations (`config.configured_locations`) every other pipeline already uses —
   no new location-selection UI.
2. **A "why was this grouped here?" disclosure** on cluster-sourced `QueueEntry`s in
   `queue.html` — a short extracted-text snippet (first ~120 chars, truncated, never the
   full document dumped into the page) behind the SAME `<details>/<summary>` disclosure
   pattern `short_path()` already established this session, so a reviewer can sanity-check
   a grouping before approving without a wall of raw text by default. This is the one
   concrete answer to the research brief's "manual-correction UX... likely the single
   largest scope item" warning: it's not a full merge/split editor (deliberately deferred —
   editing/rejecting individual entries already covers "this file doesn't belong," and
   splitting an over-broad cluster is just rejecting the entries that don't belong while
   approving the rest), but it is real, load-bearing transparency into *why* a proposal
   exists, which is the minimum bar this session's UI-design-review process already set for
   every other AI/algorithm-sourced proposal in this app.
3. **`semantic_cluster_threshold`** as a new Settings field, following the exact
   `chat_turn_cap` precedent from this session (Config field, `load_config`/`save_config`/
   `config_to_dict`, a Settings pane input + POST route).

### 2.6 The iCloud-placeholder correctness risk — a real decision, not a deferral

The research brief (§3b point 4) flags that any macOS content-reading pass over
`~/Documents`-style locations can silently force-download an evicted iCloud placeholder
file (`.name.ext.icloud`) just by trying to read it — a pre-existing risk in `survey.py`/
`dedupe.py` today. **This epic does not retroactively audit or fix those existing
pipelines** (genuinely separate scope, flagged as a follow-up note in this epic's docs, not
silently absorbed). **It DOES guard its own, brand-new content-reading code path**: the file
walk in `semantic/pipeline.py` checks `NSURLUbiquitousItemDownloadingStatusKey` (via PyObjC,
the exact API the research brief names) before ever calling `extract_text()` on a candidate
file, and skips (never force-downloads) anything not already materialized. Rationale for
drawing the line here rather than at "not our problem, it's pre-existing": this epic is
introducing the FIRST pipeline in this codebase that reads file *content* for anything
beyond a capped hash prefix (`build_plan_snapshot`'s existing 8MiB cap is a size/staleness
signal, not semantic content-reading) — writing that pipeline with a KNOWN, NAMED failure
mode already sitting in the research brief in front of us would be a straightforward
regression to ship knowingly, not a reasonable scope boundary.

### 2.7 Packaging

New PyObjC hiddenimports in `packaging/pyinstaller/cleanup_ui.spec` (mirrors the existing
`anthropic`/`httpx` hiddenimports block, since PyObjC frameworks are also not always
statically resolvable by PyInstaller's import scanner). PyObjC-under-PyInstaller is a
well-trodden combination (PyObjC's own docs and numerous community-reported working setups)
but has NOT been verified in THIS project's specific frozen-build pipeline yet — that
verification is this epic's last story, not an assumption.

## 3. Risks

- **PyObjC-under-PyInstaller compatibility, unverified in this project's build.**
  Mitigation: story 1 confirms the frameworks import/call correctly in dev; the final story
  does a real frozen-build manual verification pass before this is considered shipped, with
  explicit sign-off language rather than an assumed pass.
- **Threshold-based clustering is cruder than HDBSCAN** (no density-adaptive behavior, no
  per-point confidence score) — accepted tradeoff per §2.4's reasoning; if real usage shows
  it's too coarse (e.g., merges unrelated files or never fires), a follow-up epic can
  introduce HDBSCAN behind the same `cluster.py` interface without touching anything else.
- **OCR/PDFKit text quality on real messy documents** — the research brief is explicit that
  local OCR has a real accuracy ceiling (handwriting especially). This epic accepts that
  ceiling; poor extraction just means a cluster gets a generic `cluster-<n>` label or gets
  correctly left ungrouped (similarity too low to cluster) rather than mis-clustered with
  confidence — a fail-safe direction, not a fail-open one.
- **First content-reading pipeline in this codebase** — see §2.6's iCloud guard as the
  concrete mitigation for the one specifically-known failure mode; more generally, this is
  why `extract_text()` is designed to never raise (mirrors `build_plan_snapshot`'s existing
  contract) — a file that can't be read/extracted just doesn't contribute to clustering,
  never crashes the whole pipeline run.

## 4. Dependencies

Builds on: `queue.stage_entries`/`parse_group_key`/`group_entries_hierarchical` (this
session, now in `queue.py`), `config.py`'s load/save/config_to_dict pattern, `jobs.py`'s
background-job pattern, `adapters.OSAdapter`'s ABC+factory precedent and
`set_screenshot_save_location`'s NotImplementedError-on-unsupported-platform precedent, this
session's UI design-review conventions (`short_path`/disclosure pattern, `data-intent`
buttons, shared type scale). Does NOT depend on `chat/` — this epic is fully independent of
the chat agent (though a LATER epic could plausibly add clustering-aware tools to the chat
agent's tool set; explicitly not this one).

## 5. Open questions (resolved above, restated for traceability)

1. Dependency footprint: Apple-native only, phase 1 — §2.3.
2. Clustering algorithm: threshold + union-find, not HDBSCAN — §2.4.
3. UI surface: extend existing dashboard/queue, one new trigger + one new disclosure, no
   new page — §2.5.
4. iCloud placeholder guard: fixed in this epic's OWN new code path only, not retroactively
   applied to `survey.py`/`dedupe.py` — §2.6.
5. Cluster labeling: local word-frequency heuristic, explicitly not the AI-provider
   extension — §2.4.

## 6. Scale assessment

**Large.** New subsystem (`semantic/` package), new runtime dependency group, new persisted
local index, first content-reading pipeline in the codebase, extensions to two existing
rendering surfaces (dashboard tree, queue view), a new settings field, and real packaging
risk (PyObjC-under-PyInstaller unverified). Proceeding to H/V decomposition.

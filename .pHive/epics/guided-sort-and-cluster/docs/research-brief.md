# Research Brief: guided-sort-and-cluster

## Scope

Four related asks from the product owner, bundled into one planning pass:

1. Dashboard tree view for bulk approval (Downloads > bucket, Desktop > bucket, "approve all" per branch).
2. User-controlled AI-sort scope (today: fixed "other pile only, capped" — wanted: other-only / everything / within-an-already-bucketed-category, user-adjustable cap).
3. An in-app chat/agent interface to interactively build a sort plan and approve it section-by-section.
4. Semantic/entity clustering (photos of a specific person together, house-sale docs together) — a deep-research brief already exists at `.pHive/research/semantic-desktop-organization.md`.

## 1. The approval queue is the landing surface for all four pieces

`src/cleanup_tools/queue.py`'s `QueueEntry` (34 fields: `action`, `src`, `id`, `dest`, `status`, `source`, `status_history`, `group_key`, `created_at`, `plan_snapshot`, `executed_at`, `execution_error`) plus `stage_entries`/`set_status`/`undo`, all under `adapter.file_lock`'s advisory-flock-on-a-sibling-file locking, is the single store every existing pipeline (sort, reclaim, corral-screenshots, AI proposals) already writes through. **`group_key` is a flat string, never hierarchical** — `sort:<bucket>`, `reclaim:<category>`, `corral-screenshots` (one flat literal), or `None`→"ungrouped". There is no concept of *source root directory* (Downloads vs. Desktop) anywhere in the schema — sort only ever runs against one target dir per invocation.

**This is the epic's central schema decision**: a tree view needs a way to group by source-root AND bucket; AI sub-sorting within an already-bucketed category needs a way to distinguish "top-level heuristic sort:photos" from "AI re-split of sort:photos"; semantic clustering wants its own `semantic:<cluster-id>` namespace. All three want *hierarchy*, and today's schema has none. Whatever `group_key` scheme is chosen needs to work for a **real 6,800+-entry existing queue file** — a migration story, not a green-field schema.

## 2. Bulk-approve already has a working backend; the gap is entirely on the query/UI side

`_bulk_target_ids` (routes.py) does exact `group_key` equality only (deliberately — a passing test guarantees no substring/prefix matching today). `_bulk_transition` calls `queue.set_status` once per resolved id — this execution path needs **no change** for a tree view. What's missing is purely resolution logic: "which ids fall under this tree branch" needs prefix/hierarchical matching that doesn't exist yet, plus the tree UI itself (no hierarchy rendering exists anywhere; `dashboard.html`'s `.group-grid` is a flat card list).

## 3. AI-sort scope: the cap mechanism is sound and reusable; the candidate-selection is not

`ai/wiring.py`'s `propose_for_other_bucket` enforces its cap as a **pre-call slice** (`candidates[:cap]`, never post-call truncation) — "the single most important correctness property" per its own docstring, and the pattern any new mode must preserve. But candidate selection is hardcoded to "today's fresh `other`-bucket dry-run plan" — there's no parameter for scope. Two of the three requested new modes are not parameter tweaks:
- "Other only" (today) and "everything" are both variations of *pre-move dry-run candidates* — a scope parameter is enough.
- "Within an already-bucketed category" (the product owner's actual described workflow: split real family photos from screenshots-that-slipped-through *inside* the photos bucket) means scanning **post-move or post-approval** state, not a fresh pre-sort dry-run — this is new plumbing, not a flag.

The CLI (`cleanup propose-ai --cap N`) already proves cap-as-parameter works end-to-end; the UI route hardcodes `DEFAULT_AI_CAP` with zero exposed control — that gap alone is small.

## 4. No chat/streaming infrastructure exists; the closest precedent is the background-job poll pattern

Zero existing chat, conversation-history, SSE, or WebSocket code anywhere in the repo. The Anthropic SDK (`0.121.0`, unpinned in `pyproject.toml`) supports both streaming and plain multi-turn `messages=[...]` lists natively — extending is additive, not a rework, but the existing `AnthropicProvider.propose_bucket` is deliberately single-shot (`max_retries=0`, hand-rolled 2-attempt retry scoped to one call) and isn't reusable as-is for a chat loop. Two viable substrates on top of Flask's threaded dev server: (a) reuse `ui/jobs.py`'s existing `start_job`/poll pattern (already proven at scale for `/plan/*`), or (b) a new SSE streaming response. (a) is more consistent with the existing codebase idiom and was flagged in research as the likelier fit against the corrected network-policy rule's "doesn't poll aggressively" bar.

## 5. Frontend has zero bundler — confirmed, not assumed

`package.json` has exactly one dependency (`@tauri-apps/cli`). No webpack/vite/rollup/tsconfig anywhere. Every existing static JS file is a bundler-free IIFE loaded via a plain `<script>` tag, gated on `window.__TAURI__` presence when a feature needs the desktop shell. `plan-trigger.js`'s `pollJob()` panel is the closest structural precedent for both the tree-approval UI and a chat panel: vanilla `fetch`, `data-*` attributes carrying server-rendered URLs, no framework. Any new UI in this epic should match this shape or the design needs to explicitly justify a bundler — a real cost this codebase has avoided everywhere else.

## 6. Semantic clustering: architecture already designed, zero new dependencies installed yet

The existing research brief (`.pHive/research/semantic-desktop-organization.md`) proposes a new `src/cleanup_tools/semantic/` package (`embeddings.py`, `ocr.py`, `faces.py`, `index.py` via sqlite-vec, `cluster.py` via HDBSCAN) and an additive `propose_cluster_label` AI-provider method. **None** of its proposed dependencies (sqlite-vec, an embedding runtime, a face-recognition library, OCR, HDBSCAN) exist in `pyproject.toml` today — this is genuinely new subsystem work, not an extension of existing code. Its own phasing recommendation (documents-by-topic before photos-by-person; local OCR+embedding+clustering before any AI-provider call) is sound and should carry forward unchanged into this epic's decomposition, since it was already scoped independently and doesn't need to be re-litigated here.

## 7. Hard conventions this epic must not violate

- **Propose → queue → separate deliberate execution.** No new surface (tree bulk-approve, chat-approved plan sections, semantic clusters) may execute directly; everything lands in the queue exactly like every existing pipeline.
- **Cap-as-pre-call-slice**, not post-call truncation — the one correctness property `ai/wiring.py`'s docstring calls "the single most important."
- **Network policy (corrected same day as this research, commit `f2086a7`)**: not a blanket ban — opt-in/visibly-triggered features are fine; ambient/telemetry-style or aggressively-polling ones are not. This directly permits the chat feature but constrains its infrastructure choice (poll-on-demand over aggressive background polling).
- **Design against the real 6,800+-entry queue**, not a toy example — pagination and background-job infrastructure already exist specifically because naive full-table rendering/scanning broke at this scale once already.

## Test precedent

`tests/test_queue.py` (concurrency/locking races), `tests/test_ui_routes.py` (~80 tests, including the exact-match-not-substring bulk-approve guarantee), `tests/test_ai_wiring.py` (cap-enforcement-by-call-count, not result-count), `tests/test_jobs.py` (background-job semantics) are the direct precedent suites for this epic's stories.

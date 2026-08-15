# Design Discussion: guided-sort-and-cluster

## 0. Prelude

No prior KG decisions found for this topic (clean slate). North star (from `.pHive/project-profile.yaml`): ship a local-first desktop app anyone can download to clean up their Mac, with a pluggable AI layer, prioritizing the author's own real use first. This epic is squarely in that lane — it's the author's own stated next workflow.

**Revision note (round 2, post product-owner review):** the product owner corrected three things in the first draft: (1) root locations must be arbitrary/any-and-all, not a fixed enum, and the workflow needs to let the user pick a *subset* of locations and run a scoped pass against just that subset; (2) the real queue will be reset before this ships, which substantially de-risks the backward-compatibility work the first draft was built around; (3) reject/undo/edit/re-run all need to be first-class, because the chat-agent piece (originally epic #3/#4, furthest out) needs "full skills" to actually do those things on the user's behalf — it's being promoted to the very next epic, not deferred. This revision reflects all three.

## 1. Goal

Let the product owner triage a large, messy queue at the speed the tool's scale demands: run sort/reclaim against any location (not just the two or three hardcoded defaults), select a subset of locations and act on just that subset, approve/reject/undo/edit broad swaths at once via a real tree view, and — very soon after — hand a BYOK LLM real tool-access to the same primitives so it can help drive the triage conversationally, not just propose a one-shot batch.

## 2. Recommendation: still split sequentially, but re-sequenced — chat agent moves up to be next, not last

The four original pieces still have different sizes and infrastructure profiles, and splitting them (rather than one mega-epic) is still right — but the product owner's answers change the order: the chat-agent-with-tools piece depends directly on the primitives in this epic (reject/undo/edit/re-run/select-and-run), and the owner wants it soon, not after AI-sort-scope-modes and semantic clustering. Re-sequenced:

| # | Piece | Size | New infra? | Depends on |
|---|---|---|---|---|
| **1 — this epic** | Arbitrary-location queue hierarchy + dashboard tree + full entry-level control (approve/reject/undo/edit/re-run) + subset-select-and-run | Medium | No | Nothing |
| **2 — next** | In-app chat/agent plan-builder, BYOK, with tool-access to this epic's primitives | Large | **Yes** — first chat/streaming surface in the codebase | #1 (the agent's "tools" ARE #1's primitives) |
| 3 | User-controlled AI-sort scope (other/everything/within-bucket, adjustable cap) | Medium | No | #1's hierarchy scheme; likely absorbed as one of #2's "skills" rather than staying a separate one-shot batch feature — re-evaluate when #2 is planned |
| 4 | Semantic/entity clustering | Large | **Yes** — first ML/embedding subsystem, zero deps installed yet | Independent of #1–3 technically; still last because it needs its own deep design pass (embedding/OCR/face library choices) that the other three don't touch |

**Why chat-agent (#2) still isn't bundled into this epic**, even though it's next: it's a genuinely different kind of work — no chat/conversation/streaming code exists anywhere in this codebase today (confirmed by grep), the Anthropic SDK call pattern here is deliberately single-shot, and a real cost-control design is needed for open-ended conversation (very different shape than a capped batch call). Building it well means it should be planned against THIS epic's primitives once they're real and shipped, not designed against a paper API that might shift during implementation. This epic is the fast, mechanical foundation; #2 starts immediately after, informed by what actually landed.

## 3. Proposed approach for THIS epic

**Locations: arbitrary, not a fixed enum — reuse `Config.search_roots`, which already exists for exactly this.** Research found `reclaim.py` and `corral_screenshots.py` already support scanning multiple arbitrary user-configured locations via `Config.search_roots` (CLI-supplied dirs win, else configured `search_roots`, else a small hardcoded default). `sort.py` is the outlier — it takes exactly one `target_dir`. This epic:

1. Extends `sort.py` to accept multiple roots the same way its siblings already do (`search_roots`-aware, CLI/UI-supplied roots win).
2. Represents a queue entry's location as *whichever of the user's actual configured/selected roots its `src` falls under* — open-ended by design (any path the user has configured or picked), with a single `"other"` fallback for anything outside all of them. No fixed enum.
3. Adds a **root/location picker to the kickoff bar and `/plan/*` routes**: today those routes always run against a single hardcoded default; this epic adds the ability to select a subset of locations (from configured `search_roots`, or an ad-hoc path) and kick off a scoped sort/reclaim pass against just that subset — directly answering "I should be able to select a subset and say just do this, go."

**Parsing existing `group_key`s — simplified, since the real queue resets before this ships.** New writes use a location-aware format (e.g. `sort:<location>:<bucket>`, location being the actual configured root, not an enum member). Reads must not crash on an unexpected/old format (defensive fallback to `"other"`), but — per the product owner — no longer need a proven migration path against real historical data; a reset queue means this can be validated against synthetic fixtures covering the *shape* of old formats, not a byte-for-byte real snapshot.

**Full entry-level control, not just approve.** Alongside per-branch approve (existing `_bulk_transition`, unchanged) and per-branch reject/undo (same execution path, already symmetric for individual entries — extend to bulk), this epic adds an **edit** primitive that doesn't exist today: `queue.py` currently only has `set_status` (change status) and `undo` (revert to prior status) — no way to change a *pending* entry's proposed `dest`/bucket before approving it. New `queue.edit_entry(adapter, entry_id, new_dest, new_group_key, queue_path)`-shaped function, same locking discipline as `set_status`, appends to `status_history` so edits are auditable the same way status changes are. This is the concrete primitive both the tree UI ("move this whole branch to a different bucket before approving") and the future chat agent ("the agent proposes changing X, you can approve that edit") will call.

**"Re-running"**: the kickoff bar already re-runs a plan idempotently (existing `stage_entries` dedup-by-pending-src). What's new is scoping a re-run to a specific location subset (via the picker above) rather than always the full default set.

**UI**: a collapsible tree on the dashboard, grouped by actual configured/selected location then bucket — e.g. `~/Downloads > screenshots (42, 1.2GB) [Approve all] [Reject all] [Undo all]`, with an "Other" branch always present so nothing silently disappears. Vanilla JS matching `plan-trigger.js`'s existing fetch/data-attribute pattern, no bundler. Large branches never render per-entry inline — the tree only ever shows aggregate counts/sizes per branch, consistent with why pagination exists on the flat queue view today. The location/subset picker for kicking off new plans lives on the existing kickoff bar, extending its current checkbox-per-pipeline UI with a location multi-select.

**Cost control**: N/A for this epic — no AI calls involved.

## 4. Risks

- **`sort.py`'s single-target-dir assumption runs deeper than the public `run()` signature.** `_plan`, `_run_from_queue`, and the CLI arg parsing all thread a single `target_dir` today; extending to multi-root needs auditing all three, not just the entry point — mirror `reclaim.py`'s already-working multi-root shape rather than inventing a new one.
- **Location-picker UX at real scale.** If `search_roots` grows large (many configured locations) or a chosen ad-hoc path is enormous, the picker and the resulting tree still need to only ever show aggregates, never eagerly enumerate — same "don't repeat the pre-pagination mistake" risk as the original draft, now applying to two UI surfaces (kickoff picker + tree) instead of one.
- **New `edit_entry` primitive needs the same concurrency discipline as `set_status`/`undo`.** It's new surface area on the queue's locking contract — needs its own concurrency tests (`test_queue.py` already has a real-race test template to extend, not invent).
- **Scope discipline for the chat-agent hand-off.** This epic's job is to make reject/undo/edit/re-run/select-and-run real, tested, and usable by a human first. It's tempting to start shaping them around "what an LLM tool call would need" prematurely — resist that; #2 should adapt to what actually shipped here, not the reverse.

## 5. Dependencies

None outside this repo. No new third-party packages. Builds on `queue.py` (extended with `edit_entry`), `sort.py` (extended to multi-root matching `reclaim.py`/`corral_screenshots.py`'s existing pattern), `routes.py`'s bulk-transition path, and `dashboard.html`'s rendering.

## 6. Open questions

Two of the original three are resolved by the product owner's answers (reject/undo: yes, full parity; migration: de-scoped by the queue reset). One remains:

1. **`search_roots` UI**: today `search_roots` is config-file-only (`~/.config/cleanup-tools/config.yaml`, hand-edited or defaulted). Should this epic also add a way to manage it from the Settings page (add/remove a location), or is CLI/config-file management sufficient for now, with only the *picker* (select from what's already configured, for a given run) living in the UI? Recommend: picker only for this epic — managing the underlying `search_roots` list is a small, separable follow-up, not blocking.

## 7. Scale assessment

**Medium**, three vertical slices, each landing in a working state on its own:

1. **Multi-root + hierarchy schema slice**: `sort.py` extended to `search_roots`-aware multi-root scanning (matching `reclaim`/`corral_screenshots`'s existing pattern), the new location-aware `group_key` scheme, defensive (not migration-proven) backward-compat parsing. No UI change yet.
2. **Entry-control slice**: `queue.edit_entry`, bulk reject/undo extended to match bulk-approve's existing prefix-aware resolution, full concurrency test coverage.
3. **UI slice**: the dashboard tree (built on slices 1+2) and the kickoff-bar location/subset picker.

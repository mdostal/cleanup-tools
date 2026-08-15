# Design Discussion: guided-sort-and-cluster

## 0. Prelude

No prior KG decisions found for this topic (clean slate). North star (from `.pHive/project-profile.yaml`): ship a local-first desktop app anyone can download to clean up their Mac, with a pluggable AI layer, prioritizing the author's own real use first. This epic is squarely in that lane — it's the author's own stated next workflow.

## 1. Goal

Let the product owner triage their real ~6,800-entry queue at the speed the tool's scale demands: approve broad swaths at once (whole Downloads/Desktop sort results), progressively hand harder-to-classify piles to AI (with real control over cost/scope), and — eventually — place things by what they're *about*, not just what type they are.

## 2. Recommendation: split into four sequential epics, not one

The research brief surfaces one finding that should drive the whole shape of this plan: **the four pieces have genuinely different risk, dependency, and infrastructure profiles**, and three of them share one foundational schema decision that should land once, first, rather than being redesigned three times.

| # | Piece | Size | New infra? | Depends on |
|---|---|---|---|---|
| 1 | Hierarchical `group_key` + dashboard tree view | Small–Medium | No — reuses `_bulk_transition` as-is | Nothing |
| 2 | User-controlled AI-sort scope (other/everything/within-bucket, adjustable cap) | Medium | No — extends `ai/wiring.py`'s existing shape | #1's `group_key` scheme (for the "within-bucket" mode's own grouping) |
| 3 | In-app chat/agent plan-builder | Large | **Yes** — first chat/streaming surface in the codebase | #1 (approves land as tree branches) + #2 (the agent's "tools" are largely #2's scope modes) |
| 4 | Semantic/entity clustering | Large | **Yes** — first ML/embedding subsystem, zero deps installed yet | Independent of #1–3 technically, but its outputs (semantic clusters) want the same tree UI from #1 to be reviewable at scale |

Recommendation: plan and ship **#1 as this epic**, sized to two vertical slices (schema first, tree UI second — see §7) rather than "days," since the schema slice is genuinely correctness-critical against a real 6,800-entry queue and deserves to be validated on its own before the UI is built on top of it. It's still immediately valuable on its own (you can start bulk-approving Downloads/Desktop the moment it ships) and still much smaller than #2–4. Once #1's `group_key` scheme is real and battle-tested against the actual queue, run `/plan` again for #2 (now a much smaller, better-informed epic), then #3, then #4 — each building on the last epic's real, shipped foundation instead of a paper design. This mirrors exactly how this project has already shipped everything else (harden-cleanup-cli → ai-approvals-ui → port-remaining-scripts → desktop-app-shell, each a real, separately-shipped epic).

**Why not one big epic with 15+ stories covering all four:** the chat agent (#3) and semantic clustering (#4) are each large enough to be their own multi-week efforts with real open design questions (what tools does the agent have; which embedding/OCR/face libraries to actually vendor). Bundling them with the much smaller, much more mechanical #1/#2 would either slow #1/#2 down waiting for #3/#4's harder questions to resolve, or force #3/#4's design to be rushed to keep pace with #1/#2. Sequential epics let each piece move at its own real pace and let you start using #1 while #2–4 are still being designed.

## 3. Proposed approach for THIS epic (#1: hierarchical group_key + dashboard tree)

**Schema — `root-slug` is a fixed enum, not a derived free-form value.** `root-slug ∈ {"downloads", "desktop", "documents", "other"}` — decided at design time, not inferred per-path at runtime with open-ended output. Resolution algorithm (to confirm against `adapters/base.py` during implementation — `resolve_standard_dir` is confirmed to exist and be called for `"downloads"` today; the implementing story's first task is confirming it also resolves `"desktop"`/`"documents"` symmetrically, or adding thin wrappers if not):

1. Resolve each of the three standard dirs to an absolute path once per request.
2. For a given entry, check whether `Path(entry.src).resolve()` is equal to or a descendant of each standard dir's resolved path, in that fixed order (downloads → desktop → documents).
3. First match wins; no match → `"other"`.

**Parsing existing (pre-epic) `group_key`s — explicit table, not a general parser.** New writes always produce the 3-segment form. Reads must handle every format the research brief confirmed exists today:

| Existing format | Segments | Parsed as |
|---|---|---|
| `sort:<bucket>` | 2 | `root="other"`, `bucket=<bucket>` |
| `reclaim:<category>` | 2 | `root="other"`, `category=<category>` |
| `corral-screenshots` | 0 colons | `root="other"`, no sub-bucket |
| `None` | — | displayed as "ungrouped", not part of the tree at all (see open question 2) |

Split on `:` with `maxsplit`, branch on segment count (0, 2, or 3) rather than a general-purpose parser — a 4th unexpected format should raise loudly in a test, not be silently swallowed. **Verification step before implementation**: grep the whole repo for every `.group_key` read/write site and confirm each one treats it as either an opaque exact-match key or a display string (the research pass checked the obvious consumers — `_bulk_target_ids`, `_group_entries`, `queue.html`'s badges — but wasn't scoped as an exhaustive audit; do that audit as part of this story, not after).

**Backend**: `_bulk_target_ids` gains prefix-aware resolution (a tree-branch identifier like `sort:downloads:` matches every `group_key` starting with that prefix) alongside the existing exact-match mode — additive, not a replacement, so today's flat "Approve group" buttons keep working unchanged. `_bulk_transition`'s execution path (loop calling `set_status`) needs no change at all.

**UI**: a collapsible tree on the dashboard, `Downloads > screenshots (42, 1.2GB) [Approve all]`, `Desktop > screenshots (8, 90MB) [Approve all]`, `Other > ... ` (the fallback bucket, always present so nothing silently disappears), etc. — vanilla JS matching `plan-trigger.js`'s existing fetch/data-attribute pattern, no bundler. Reject/undo-all get the same per-branch treatment. Large branches (thousands of entries) never render per-entry — the tree only ever shows counts/sizes per branch, consistent with why pagination exists on the flat queue view today.

**Cost control**: N/A for this epic — no AI calls involved, purely a queue-schema + bulk-approval UI change.

## 4. Risks

- **Migration risk on the real 6,800-entry queue.** The new `group_key` format must be backward-compatible at read time per the parsing table in §3 (old flat keys still parse, just as `root="other"`) — never a one-time rewrite migration on the user's real data. Needs a test built from a representative sample of the real flat-key formats confirmed in the research brief, not just a handful of synthetic entries.
- **Root-dir inference ambiguity.** Not every `src` path cleanly maps to "Downloads" or "Desktop" (e.g. a nested subfolder, or a file already moved into `_sorted/`). Needs an explicit "other/unknown root" bucket in the tree rather than silently mis-grouping or crashing.
- **Tree UI at scale.** A naive tree that eagerly renders every leaf would repeat the exact performance mistake pagination/background-jobs were built to fix elsewhere in this app. The tree must only ever show aggregate counts per branch, never a flat entry list inline.

## 5. Dependencies

None outside this repo. No new third-party packages. Builds entirely on `queue.py`, `routes.py`'s existing bulk-transition path, and `dashboard.html`'s existing rendering.

## 6. Open questions

1. **Root-dir set**: Downloads + Desktop only (as literally requested), or also Documents (already a `reclaim`/`corral-screenshots` root today)? Recommend: include Documents too, since the schema change is the same effort either way and excluding it would just mean redoing this story again soon.
2. **Existing flat-key entries**: should the tree show a fourth "ungrouped / pre-existing" branch for the real queue's current entries (staged before this schema existed), or should those simply not appear in the tree view at all (still visible in the flat `/queue` page)? Recommend: a visible "ungrouped" branch, so nothing silently becomes harder to find.
3. **Reject/undo-all per branch**: requested piece only mentioned "approve," but reject/undo exist symmetrically today for individual entries and for exact-match groups. Recommend: ship all three per-branch actions together, not approve-only, since the backend cost is identical.

## 7. Scale assessment

**Medium.** Multi-file (queue.py, routes.py, dashboard.html, a new/extended static JS file, tests across `test_queue.py`/`test_ui_routes.py`), single layer (Flask + vanilla JS, no new infrastructure), no new dependencies, no new architecture pattern — but real-scale correctness (the 6,800-entry queue, backward-compatible parsing) makes it more than a "small" mechanical change. Two vertical slices, each landing in a working state on its own:

1. **Schema slice**: the `root-slug` enum + parsing table from §3, backend prefix-matching in `_bulk_target_ids`, and the group_key-consumer audit — validated against a real queue snapshot. No UI change yet; existing flat "Approve group" buttons keep working unchanged throughout.
2. **Tree UI slice**: the dashboard tree view, built on top of slice 1's now-real, now-tested schema.

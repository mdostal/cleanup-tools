# Research Brief: settings-and-transparency

## Scope

Grow Settings from one flat page (app-icon picker only) into a real settings surface: every `Config` field editable from the UI, a proper entry point (gear icon, not a peer-level text nav link), progressive disclosure for advanced options, and a genuine transparency/history view — the product owner's explicit asks: "all settings should be changeable," "we need more transparency," "a big gear icon in the top right so you hit settings and can delve into lots of stuff or open advanced views."

## 1. Current state (confirmed by direct code read)

`Config` (`src/cleanup_tools/config.py`) has four fields: `bucket_rules` (extension/pattern → bucket mappings), `search_roots` (list of paths reclaim/corral-screenshots already scan — see `guided-sort-and-cluster`'s research), `master_paths` (irreplaceable-file protection list, each with a `backed_up` flag), and `icon_choice` (added this session). **Only `icon_choice` has a UI** (`GET /settings`, `POST /settings/icon`, `settings.html`) — the other three are config.yaml-only, hand-edited or defaulted.

`base.html`'s nav is a flat list: `Dashboard | Review Queue | Settings | Plan: Sort | Plan: Reclaim | Plan: Corral Screenshots` — Settings sits as a peer-level text link, competing visually with actual page destinations, no gear icon anywhere, no sub-navigation, no history/audit view anywhere in the app.

## 2. What "all settings changeable" actually means, concretely

- **Bucket rules**: currently a Python list of `BucketRule(extensions, bucket, filename_pattern)` dataclasses, order-sensitive (first match wins). A UI needs to expose add/edit/remove/reorder for a list where order is semantically meaningful — this is real UI complexity, not a flat form.
- **Search roots**: a plain list of paths. Simpler — add/remove, maybe validate existence.
- **Master paths**: path + `backed_up` boolean. The `backed_up` flag is safety-critical (reclaim refuses to delete under an un-backed-up master path even with `--go`) — the UI must make it very clear what flipping this flag actually does, not just render it as a generic checkbox.
- **Icon choice**: already done this session, becomes one section among several rather than the whole page.

## 3. Prior-art synthesis (full detail in `.pHive/research/prior-art-cleaning-and-settings-ux.md`)

Three parallel research passes (competitor Mac-cleaner UX, settings/preferences patterns in well-regarded dev tools, transparency/audit-trail UX) converged on:

- **Sidebar-of-sections** (Raycast/Obsidian), not one long page — General / Bucket Rules / Search Roots / Master Paths / AI Provider / Advanced, each config *list* getting its own drill-in sub-view.
- **Gear icon, pulled out of the peer-level nav**, bound to `Cmd+,`.
- **Progressive disclosure**: one `Advanced` section, never more than two levels deep (raw config-as-JSON view/edit, log verbosity) — mainstream settings (bucket rules, search roots, master paths, AI provider) stay on the main surface since they're things the user configures with intent and needs to trust, not "advanced."
- **Transparency = a real History view**: reverse-chronological action log (what moved/deleted/renamed, when, which rule/AI proposal triggered it) with **per-row undo** (Hazel 6's model — undo travels with the specific action, not one global stack) — the single most direct answer to "we need more transparency," and a genuine differentiator since the closest commercial competitor (CleanMyMac) has no exportable log or scan history at all.
- **Trust signal**: since this app is local-first by design, surfacing that fact in the UI itself (last network-check timestamp for the one opt-in AI feature, an explicit "0 ambient network calls" statement) is cheap and directly answers "more transparency" — the 2024 Bartender trust collapse (undisclosed ownership change + silently added telemetry) is a concrete cautionary tale for this exact class of deep-filesystem-access tool.

## 4. What already exists that a History view can build on

`QueueEntry.status_history` (`queue.py`) already records every status transition with a timestamp — `[{"status": "approved", "timestamp": "..."}, ...]`. A History view doesn't need new storage for the "what happened to entry X" question; it needs a new *read* path that flattens `status_history` across all entries into one reverse-chronological feed, plus enough context per row (which rule/pipeline staged it, its group_key) to be useful without re-deriving it. `guided-sort-and-cluster`'s planned `edit_entry` primitive (also appends to `status_history`) means edits will already be covered by the same feed once that epic ships — worth sequencing awareness, not a hard dependency in either direction.

## 5. Local-first constraint

Per `.pHive/CONTEXT.md`'s corrected network-policy rule (commit `f2086a7`): a settings/history UI is pure local read/write, no network involved at all — squarely fine, no policy question here. The "trust signal" (surfacing that the app makes no ambient calls) is itself just a static UI statement plus, for the one real opt-in feature (AI provider), a last-used timestamp already implicitly available from `status_history`'s `ai:<provider>`-sourced entries.

## Test precedent

`tests/test_ui_routes.py`'s existing settings tests (icon picker) are the direct template for new settings-CRUD route tests. No existing test file covers a history/audit view since it doesn't exist yet.

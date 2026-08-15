# Design Discussion: settings-and-transparency

## 0. Prelude

No prior KG decisions found for this topic. North star (project-profile.yaml): local-first desktop app, author's own real use first. Directly requested by the product owner in this session: "all settings should be changeable... we need more transparency, and a big gear icon in the top right."

## 1. Goal

Turn Settings from a single icon-picker page into a real settings surface — every `Config` field editable, a proper entry point that doesn't compete with primary nav, progressive disclosure so it doesn't overwhelm, and a genuine history/transparency view that out-does the closest commercial competitor's total lack of one.

## 2. Proposed approach

**Nav**: replace the flat-text "Settings" nav link with a gear icon, right-aligned in the nav bar (matches the explicit ask), bound to `Cmd+,` when running inside the Tauri shell (a new, small Rust-side keyboard-shortcut registration — check whether Tauri's global-shortcut plugin is warranted or whether a plain webview `keydown` listener suffices given this is an in-page shortcut, not a system-wide one; lean toward the simpler in-page listener unless a real need for system-wide surfaces).

**Settings page restructure**: sidebar-of-sections (matching Raycast/Obsidian's pattern from research), replacing the current single `<section>`:
- **General** — nothing here yet beyond maybe a version/about block; a placeholder section is fine, don't force content that doesn't exist.
- **App Icon** — today's icon picker, unchanged, becomes one section instead of the whole page.
- **Bucket Rules** — list view (order matters — first match wins), each rule drills into its own edit pane (extensions, bucket, optional filename pattern), add/remove/reorder (drag or up/down buttons).
- **Search Roots & Locations** — simpler list, add/remove paths; this is the natural home for `guided-sort-and-cluster`'s deferred "manage `search_roots` from the UI" open question, picked up here instead.
- **Master Paths** — path + `backed_up` toggle, with explicit, unambiguous copy on what flipping `backed_up` actually changes (reclaim's refusal behavior) — this is safety-critical config, the UI must not present it as a generic checkbox.
- **AI Provider** — currently env-var/credentials-file only; at minimum surface *whether* a key is configured and from where (env var vs. credentials file), without ever displaying the key itself.
- **Advanced** — one section, one level deep: raw config-as-JSON view (read-only initially; edit-in-place is a fast-follow, not required for this epic — see open questions), log verbosity if/when that exists.
- **History** — new. Reverse-chronological feed built from `QueueEntry.status_history` across all entries (already-recorded data, no new storage), each row showing what happened, when, and to what. **Undo semantics must be gated on `entry.executed_at`, not offered uniformly** (self-caught during review, not by the earlier grill pass — see §3): `queue.undo()` only reverts *queue status* (e.g. un-reject something back to approved); it never touches the filesystem. For an entry with `executed_at` still `None`, "Undo" is exactly that safe, already-tested operation. For an entry whose `--from-queue` execution already ran (`executed_at` set — the move/delete actually happened on disk), a plain "Undo" button next to it would be actively misleading: clicking it flips a status field while the file has already moved. Executed entries must either hide the Undo action entirely or visibly relabel it (e.g. "Executed — reverting the record does not move the file back") — this epic does **not** add real post-execution filesystem-undo (restoring a moved/deleted file), that's out of scope and would be its own epic if ever wanted.

**Cost control**: N/A — no AI calls, no network calls, pure local read/write.

## 3. Risks

- **Bucket-rules order-sensitivity is easy to get wrong in a UI.** First-match-wins means a reordering bug silently changes sort behavior for every file, not just a cosmetic glitch. Needs explicit reorder-preview ("here's what this file would now match, before/after") — direct application of Hazel's rule-preview pattern from the research, and worth a dedicated story rather than treating reordering as a trivial drag-and-drop.
- **`master_paths.backed_up` is a safety-critical toggle rendered in a generic settings list.** Risk of it reading as "just another checkbox" when it's actually the thing standing between reclaim and deleting an irreplaceable file. Needs a confirmation step or explicit warning copy when flipping true→false in particular (true→false *removes* protection).
- **"Undo" must never imply a filesystem operation it doesn't perform.** See §2's `executed_at` gating — the biggest single trust risk in this epic is a History-view Undo button that looks like it reverses a real file move/delete when it only reverts a status field. Get the executed-vs-not distinction right in the very first version of this UI, not as a follow-up fix.
- **History view performance at real scale.** The user's queue can run into the thousands of entries; a naive "flatten every status_history across every entry" query on every page load repeats the exact mistake pagination/background-jobs already exist to avoid elsewhere in this app. Needs pagination from day one, not retrofitted later.
- **Advanced/raw-JSON edit-in-place is a bigger scope bite than it looks.** Read-only JSON view is cheap; making it *editable* safely (validate before write, don't corrupt config.yaml on a bad paste) is real work. Recommend read-only for this epic's first slice, editable as an explicit fast-follow story if wanted.

## 4. Dependencies

None outside this repo. No new third-party packages for the settings CRUD/History view. The `Cmd+,` shortcut may or may not need a small Tauri capability addition depending on implementation choice (see §2) — resolve during H/V planning, not here.

## 5. Open questions

1. **JSON edit-in-place**: read-only Advanced JSON view for this epic (recommended, per risk above), with editable-in-place as a clearly-scoped fast-follow? Or is editable-from-day-one a hard requirement?
2. **Bucket-rule reorder UX**: drag-and-drop vs. explicit up/down buttons — drag feels nicer but is more implementation risk (no existing drag-and-drop anywhere in this codebase); up/down buttons are more mechanical but trivial to get right. Recommend up/down buttons for this epic, drag as a possible later polish pass.
3. **History retention**: `status_history` already lives forever on each `QueueEntry` (no separate TTL/pruning exists anywhere today) — should the History view just reflect that as-is, or does this epic also want a retention/archival policy? Recommend: no new policy for this epic, just render what's already kept; revisit only if real usage shows it's a problem.

## 6. Scale assessment

**Medium.** Multi-file (config.py already supports all four fields — no schema change needed, purely new routes/templates/JS), single layer (Flask + vanilla JS, matching the icon-picker's already-proven pattern), no new dependencies. Three vertical slices:

1. **Settings restructure slice**: gear-icon nav entry, sidebar-of-sections shell, bucket-rules/search-roots/master-paths CRUD routes + UI (the icon picker slots in as an existing section, minimal change to it).
2. **Advanced slice**: read-only config-as-JSON view.
3. **History slice**: the aggregated, paginated `status_history` feed + per-row undo wiring.

Slice 1 is the bulk of the value and the explicit "big gear icon" ask; 2 and 3 can ship independently after it.

# Vertical Slice Plan: Port find-wallets, dedupe, corral-screenshots

**Input:** horizontal-plan.md + design-discussion.md.

## 1. Slicing Strategy

```
STRATEGY:
  Total horizontal items: 3 new command modules, 1 new adapter capability, 3 CLI subparsers, 1 UI
                          route + 1 template link, 1 additive config read, ~6 test files.
  Planned slices: 3 — one per script, one per story.
  Slicing rationale: unlike harden-cleanup-cli (OS Adapter/Config had to exist before ANY command
  could be built) or ai-approvals-ui (the queue store had to exist before the UI or --from-queue
  could consume it), this epic invents no new shared foundation. Every extension point three
  scripts need (OSAdapter ABC, Config.search_roots, QueueEntry/stage_entries, the /plan/* route
  pattern, the dry-run/--go/--from-queue convention) already exists, built by the two prior
  epics. Slicing here is purely "one script per slice" — there is no foundation slice to sequence
  first.

  EXPLICIT INDEPENDENCE: the three slices below have NO dependency on each other. Each is
  independently shippable in any order, or in parallel — confirmed by design-discussion §6
  ("No story depends on the AI-provider layer... [and] Story A/B/C depend on nothing new" beyond
  already-existing infrastructure, with zero cross-references between the three). This is a real
  divergence from both prior epics' vertical plans, where every slice after the first had a
  "BUILDS ON: Slice N" line — none of these three slices has one. All three depends_on: [] in
  their story specs reflects this directly, not an oversight.
```

## 2. Vertical Slice Plan

```
## Slice A: `cleanup find-wallets` (read-only)

WHAT WORKS AFTER THIS SLICE:
  Running `cleanup find-wallets [root]` prints filename + content wallet-artifact candidates as
  JSON, behavior-equivalent to scripts/find-wallets.sh, with the same structural guarantee the
  bash script has today: no matched secret/key content can ever appear in the output, only paths
  and which pattern/alternative matched.

BUILDS ON: nothing new — adapter.list_dir/resolve_home (both pre-existing, from
harden-cleanup-cli) cover the filename scan entirely; the content scan is new regex/file-read
logic but needs no new adapter method.

LAYERS TOUCHED:
  Commands: find_wallets.py (new) — _by_filename, _by_content, run()
  CLI: find-wallets subparser + COMMANDS entry
  Tests: test_find_wallets.py

NOT YET: dedupe, corral-screenshots. No queue/UI wiring (decision 5, permanent for this command,
not deferred).

VERIFIED BY:
  - pytest: every one of the 13 filename patterns and all 4 content-regex alternatives tested
    against literal fixture strings that should and should NOT match, preserving the bash
    version's exact patterns (Risk 1) rather than reconstructed approximations.
  - pytest: an explicit assertion that no fixture secret substring (a fake seed phrase, a fake
    xprv string, a fake PEM header) ever appears anywhere in run()'s returned dict, across every
    key at every nesting level — not just "the code doesn't currently print it" (Risk 2).
  - Manual: one run against a synthetic fixture tree with both true-positive and true-negative
    files, eyeballed once before merge.

COMMIT REPRESENTS: find-wallets working end-to-end, read-only, CLI-only.

---

## Slice B: `cleanup dedupe` (read-only)

WHAT WORKS AFTER THIS SLICE:
  Running `cleanup dedupe [dir]` prints structured duplicate-file groups as JSON
  ({"duplicate_groups": [{"hash", "size_bytes", "paths"}]}), behavior-equivalent in spirit to
  scripts/dedupe.sh's two-stage size-then-hash filter, but with full uncapped SHA-256 instead of
  bash's SHA-1-truncated-to-48-bits, and structured groups instead of raw line pairs.

BUILDS ON: nothing new — adapter.list_dir/resolve_standard_dir (pre-existing) + stdlib hashlib
(already used by queue.py's _content_hash, though dedupe deliberately does NOT reuse that
capped-at-8-MiB helper — see design_decisions in the story spec) cover everything.

LAYERS TOUCHED:
  Commands: dedupe.py (new) — _group_by_size, _group_by_hash, run()
  CLI: dedupe subparser + COMMANDS entry
  Tests: test_dedupe.py

NOT YET: find-wallets, corral-screenshots. No queue/UI wiring (decision 5/6, permanent).

VERIFIED BY:
  - pytest: size-grouping drops singleton-size files before any hashing occurs (the two-stage
    filter is the one behavior most worth preserving from bash, per the research brief).
  - pytest: full-file SHA-256 correctness, including a same-size, same-prefix,
    different-tail-content negative case that an 8-MiB-capped hash would have falsely called a
    duplicate — the exact scenario decision 2 exists to avoid.
  - pytest: structured group output shape, not raw pairs.
  - Manual/benchmark: a fixture tree sized to approximate this project's own north_star clutter
    scale (hundreds of files, several genuine size-collision groups), confirming the two-stage
    filter keeps hashing bounded to the size-matched subset only (Risk 4/8's performance note).

COMMIT REPRESENTS: dedupe working end-to-end, read-only, CLI-only.

---

## Slice C: `cleanup corral-screenshots` (+ queue/UI integration)

WHAT WORKS AFTER THIS SLICE:
  Running `cleanup corral-screenshots [dir...] [--go] [--from-queue]` moves screenshots into
  ~/Pictures/Screenshots with an exact (not approximate) moved-count, dry-run by default; the
  approvals UI gets a third "Plan: Corral Screenshots" trigger so screenshot moves go through the
  same human-review path sort/reclaim moves already get; and a separate, explicitly-flagged
  command path changes where macOS saves future screenshots (with a matching graceful-skip on
  Arch Linux).

BUILDS ON: nothing new from Slices A/B (no shared code with either) — but does build on
already-existing infrastructure from the two prior epics: sort.py's dry-run/--go/--from-queue
shape, reclaim.py's config.search_roots-reading pattern, queue.py's QueueEntry/stage_entries,
routes.py's _stage_sort_plan/plan_sort pair, and find_installed_app's
abstract-method-with-one-real-implementation OSAdapter precedent.

LAYERS TOUCHED:
  OS Adapter: set_screenshot_save_location(path) — new abstract method (base.py), MacOSAdapter
    real implementation (the `defaults write` + `killall SystemUIServer` sequence), ArchLinuxAdapter
    NotImplementedError stub.
  Commands: corral_screenshots.py (new) — _plan, run(), _run_from_queue
  CLI: corral-screenshots subparser (dir/--go/--from-queue + its own independently-gated
    screenshot-location flag)
  Config: reads the existing search_roots field as its default roots (decision 3) — no schema
    change.
  UI: ui/routes.py gets _stage_corral_screenshots_plan + /plan/corral-screenshots;
    dashboard.html gets one new link.
  Tests: test_corral_screenshots.py, plus additive cases in test_adapters.py, test_cli.py,
    test_ui_routes.py.

NOT YET: find-wallets, dedupe (separate slices, no dependency either direction).

VERIFIED BY:
  - pytest: dry-run leaves the filesystem unchanged; --go moves screenshots with an exact
    per-file moved count (not bash's pre-move approximation); dest_exists entries are skipped,
    never overwritten (mirrors sort.py's dest_exists guard).
  - pytest: --from-queue executes only approved move entries under the resolved roots, with
    staleness re-checked immediately before acting (mirrors sort.py's _run_from_queue).
  - pytest: default roots resolve from config.search_roots when configured, else fall back to
    bash-parity Desktop/Downloads/Documents; CLI-supplied dirs win over both.
  - pytest: the screenshot-location flag is fully independent of --go — asserting the adapter
    call happens ONLY when its own flag is passed, in every combination with/without --go.
  - pytest: ArchLinuxAdapter.set_screenshot_save_location raises NotImplementedError, caught by
    corral_screenshots.py and reported as {"skipped": True, "reason": "not supported on this
    OS"}, never crashing the rest of the command.
  - pytest (UI): /plan/corral-screenshots stages move entries into the queue idempotently (hitting
    it twice creates no duplicate pending entries), mirroring plan_sort/plan_reclaim's existing
    dedup test coverage.
  - Manual: one dry-run pass on the real macOS machine; the screenshot-location flag exercised
    once deliberately (visible Dock/menu-bar restart expected) before any unattended use.

COMMIT REPRESENTS: corral-screenshots working end-to-end — move logic, queue/UI staging, and the
screenshot-location OS capability — epic-complete.
```

## 3. Overlay Diagram

```
VERTICAL SLICE OVERLAY
─────────────────────────────────────────────────────────────────────────
              │  Slice A            │  Slice B            │  Slice C              │
              │  (find-wallets)     │  (dedupe)           │  (corral-screenshots) │
──────────────┼──────────────────────┼─────────────────────┼───────────────────────┤
OS Adapter    │ (reuses existing)    │ (reuses existing)   │ set_screenshot_       │
              │                      │                     │ save_location (new)   │
──────────────┼──────────────────────┼─────────────────────┼───────────────────────┤
Config        │ (none)               │ (none)              │ reuses search_roots   │
──────────────┼──────────────────────┼─────────────────────┼───────────────────────┤
CLI           │ find-wallets subcmd  │ dedupe subcmd       │ corral-screenshots    │
              │                      │                     │ subcmd + flags        │
──────────────┼──────────────────────┼─────────────────────┼───────────────────────┤
UI            │ (none)               │ (none)              │ /plan/corral-         │
              │                      │                     │ screenshots + link    │
──────────────┼──────────────────────┼─────────────────────┼───────────────────────┤
Commands      │ find_wallets (JSON)  │ dedupe (JSON)       │ corral_screenshots    │
─────────────────────────────────────────────────────────────────────────

No column depends on another. Each is independently commit-worthy and independently shippable.
```

## 4. Deferred Items

```
DEFERRED (not in current slice plan):
  - Any UI surface for find-wallets/dedupe (decision 5 — explicit, not a scope gap).
  - Duplicate-file deletion/reclaim flow built on dedupe's output (design-discussion Open
    Question 3 — no consumer epic yet).
  - master-path awareness for corral-screenshots' moves (Risk 6/design-discussion §4 — matches
    sort.py's existing gap, explicitly out of scope per decision 7, not a new gap this epic
    introduces).
  - Per-desktop-environment screenshot-location support on Linux (GNOME/KDE/etc.) — Arch gets
    NotImplementedError, not a partial implementation.

RATIONALE: each is either an explicit decision already made (5, 6, 7) or has zero dependency from
Slices A/B/C as scoped (Open Question 3) — safe to defer because nothing in this epic assumes any
of them exist.
```

## 5. Risk by Slice

```
RISK PER SLICE:
  Slice A: Medium — the wallet-detection patterns carry real semantic weight (Risk 1), and the
           content-match leakage risk is structural, not just conventional (Risk 2); both are
           the kind of risk that a passing test suite genuinely proves absent, though, not one
           that lingers as residual uncertainty after the story's acceptance criteria are met.
  Slice B: Low-Medium — pure logic + hashing, no destructive operations, no leakage risk; the one
           real risk is performance at scale (Risk 3/Open-Question-adjacent), bounded by the
           two-stage filter and covered by a benchmark-style fixture test.
  Slice C: Medium-High — the only slice with real filesystem side effects (moves) and the single
           most invasive OS-level side effect in the whole codebase (killall SystemUIServer),
           gated behind its own explicit flag specifically because of that invasiveness (decision
           1, Risk 7). Also the only slice touching four layers (Adapter/CLI/Config/UI)
           simultaneously.
```

## 6. Moldability Notes

- No slice can move before another because none depends on another — order is a free choice
  (developer/scheduling convenience only), not a correctness constraint.
- If Slice C's screenshot-location adapter capability turns out to need more than one flag (e.g.
  a future per-DE Linux implementation), the fix is additive — extend `set_screenshot_save_location`
  callers/implementations, not redesign the CLI surface these three stories establish.
- No slice can be dropped without dropping its command entirely — three slices for three
  independently-valuable commands is the minimum for this epic's scope.


# Horizontal Planning Scan: Port find-wallets, dedupe, corral-screenshots

**Input:** design-discussion.md + research-brief.md. Both already did the detailed layer-mapping
work (design-discussion §2's per-story breakdowns, §5's Scale Assessment) — this doc restates it
per-layer rather than re-deriving it, per this epic's Medium scope.

## 1. Layer Inventory

- **Commands** — 3 new modules (`find_wallets.py`, `dedupe.py`, `corral_screenshots.py`), no
  existing command modified. Two are read-only (`survey.py`-shaped); one moves files and gets one
  new OS-side-effect capability (`sort.py`-shaped, plus a screenshot-location OS call).
- **OS Adapter** — 1 new abstract capability (`set_screenshot_save_location`), used by
  `corral_screenshots.py` only. `find_wallets.py`/`dedupe.py` need no new adapter surface —
  `adapter.list_dir`/`resolve_standard_dir`/stdlib `hashlib`/`re` cover everything.
- **CLI Entry Point** — 3 new subparsers + 3 new `COMMANDS` entries in `cli.py`. `find-wallets`
  and `dedupe` are flagless (a positional root/dir only, mirroring `sort_parser`'s `dir` shape).
  `corral-screenshots` gets the fuller `dir`/`--go`/`--from-queue` set (mirroring
  `reclaim_parser`'s multi-root shape) plus its own explicit, independently-gated flag for the
  screenshot-save-location change (decision 1 — never folded into `--go`).
- **UI Routes/Templates** — touched by `corral-screenshots` only, per decision 5/6 (`find-wallets`
  and `dedupe` get no UI surface at all — CLI-only). `ui/routes.py` gets a
  `_stage_corral_screenshots_plan` helper + `/plan/corral-screenshots` route, structurally
  identical to `_stage_sort_plan`/`plan_sort` (`routes.py:194-256`).
  `ui/templates/dashboard.html` gets one new link alongside the existing "Plan: Sort / Plan:
  Reclaim" pair (`dashboard.html:38`). No new template file, no thumbnail-route change — the
  existing `is_image_entry`/`thumbnail` machinery (`routes.py:95-97,559-588`) already renders
  previews for any image-extension queue entry, screenshots included, for free.
- **Config** — no schema change. `corral-screenshots`' default search roots reuse the existing
  `Config.search_roots` field `reclaim.py:418-431` (`_resolve_roots`) already reads (decision 3) —
  an additive *read*, not a new key. `find-wallets`/`dedupe` read no config at all, matching their
  bash originals (root/dir default straight from `adapter.resolve_home()`/
  `resolve_standard_dir("downloads")`, no config lookup).
- **Tests** — 3 new test files (`test_find_wallets.py`, `test_dedupe.py`,
  `test_corral_screenshots.py`), plus additive cases in `test_adapters.py` (new capability, both
  implementations), `test_cli.py` (3 new subparsers), and `test_ui_routes.py` (new plan-staging
  route) for `corral-screenshots` only.

## 2. Per-Layer Requirements

```
## Layer: Commands

find_wallets.py:
  - run(adapter, args=None) -> dict, survey.py-shaped, no filesystem mutation.
  - _by_filename: adapter.list_dir looped over the 13 literal -iname patterns (ported as literal
    constants, not reconstructed — Risk 1), excluding node_modules/Library-Caches, capped at 200.
  - _by_content: walk Documents/Desktop/Downloads for the 5 allowed extensions, run the 4-way
    regex, return path + which-alternative-fired ONLY — never match.group() anywhere in the
    return value (Risk 2, structural not conventional).
  - No --go, no --from-queue, no queue/UI wiring (decision 5).

dedupe.py:
  - run(adapter, args=None) -> dict, survey.py-shaped, no filesystem mutation.
  - _group_by_size: Path.stat().st_size grouping (cheap, no du shell-out needed — per-file stat,
    not the recursive-tree case dir_size_bytes exists for). Groups of 2+ only.
  - _group_by_hash: full, uncapped SHA-256 (decision 2) over every size-matched candidate only.
  - Structured output: {"dir", "duplicate_groups": [{"hash", "size_bytes", "paths": [...]}]}.
  - No --go, no --from-queue, no queue/UI wiring (decision 5/6).

corral_screenshots.py:
  - _plan(adapter, config, target_dirs) -> list[dict]: adapter.list_dir(d, max_depth=2,
    pattern="screenshot*") per root (default: config.search_roots, else Desktop/Downloads/
    Documents — decision 3), building {src, dest, dest_exists}, sort.py:39-51-shaped.
  - run(adapter, args=None): always computes the plan; --go gates adapter.move() per entry
    (dest_exists entries skipped, exact count vs. bash's approximate ~n); --from-queue executes
    approved move entries via a sort.py:65-149-shaped _run_from_queue helper.
  - Screenshot-save-location change: gated behind its own explicit flag (decision 1), calls the
    new adapter.set_screenshot_save_location(pictures_screenshots_dir) capability. On Arch, the
    call raises NotImplementedError, caught and reported {"skipped": True, "reason": "not
    supported on this OS"} — same fallback shape as reclaim.py's _orphaned_installers_category
    (reclaim.py:262-299).

---

## Layer: OS Adapter

INTERFACE OPERATIONS NEEDED:
  - set_screenshot_save_location(path) -> None — new abstract method on OSAdapter
    (adapters/base.py), following find_installed_app's precedent exactly (base.py:254-264):
    declared abstract-with-docstring here, no default implementation.

IMPLEMENTATIONS NEEDED:
  - MacOSAdapter (adapters/macos.py): the real two-command sequence — `defaults write
    com.apple.screencapture location <path>` then `killall SystemUIServer` (subprocess, mirroring
    reclaim.py's existing `docker`/`du` subprocess.run precedent).
  - ArchLinuxAdapter (adapters/arch_linux.py): raises NotImplementedError with a clear message —
    screenshot save location on Linux is desktop-environment-specific, no single system-wide
    preference key exists, matching find_installed_app's ArchLinuxAdapter stub (arch_linux.py:19-35).

NOT NEEDED:
  - Any adapter change for find-wallets/dedupe — existing list_dir/resolve_standard_dir/stdlib
    cover both entirely.

---

## Layer: CLI Entry Point

SUBPARSERS NEEDED:
  - find-wallets: positional `root` (nargs="?", default None -> adapter.resolve_home()).
  - dedupe: positional `dir` (nargs="?", default None -> resolve_standard_dir("downloads")).
  - corral-screenshots: positional `dir` (nargs="*", mirroring reclaim_parser's multi-root shape,
    cli.py:84-92), `--go`, `--from-queue`, plus a new independently-gated flag for the
    screenshot-location change (e.g. `--set-default-location`) — never implied by `--go` alone
    (decision 1).

COMMANDS DICT:
  - Add "find-wallets": find_wallets.run, "dedupe": dedupe.run, "corral-screenshots":
    corral_screenshots.run to cli.py:163-167's COMMANDS dispatch dict — all three follow the
    existing plain-dispatch shape (no special-casing like "approve"/"propose-ai" need).

---

## Layer: UI Routes/Templates

ROUTE NEEDED:
  - @bp.route("/plan/corral-screenshots") — identical shape to plan_sort/plan_reclaim
    (routes.py:246-265): dry-run corral_screenshots.run(), convert each plan entry into
    QueueEntry(action="move", ..., source="ui-plan-corral-screenshots",
    group_key="corral-screenshots", plan_snapshot=queue_module.build_plan_snapshot(item["src"])),
    stage_entries().

TEMPLATE NEEDED:
  - dashboard.html:38's "Plan: Sort / Plan: Reclaim" line gets a third
    `{{ url_for('ui.plan_corral_screenshots') }}` link. No new template file — the existing
    review-card (queue.html) and thumbnail route already handle any image-extension entry.

NOT NEEDED:
  - No UI surface at all for find-wallets/dedupe (decision 5) — no route, no template change.

---

## Layer: Config

SCHEMA NEEDED: none — no new keys, no format change.

USAGE NEEDED:
  - corral_screenshots.py reads config.search_roots (already loaded via
    config_module.load_config(adapter), the same call sort.py/reclaim.py already make) as its
    default roots when no dirs are passed on the CLI, before falling back to bash-parity
    Desktop/Downloads/Documents (decision 3).

---

## Layer: Tests

  - test_find_wallets.py: filename-pattern matches/non-matches against literal fixture strings
    (Risk 1), content-regex matches/non-matches against literal fixture strings including
    negative cases, and an explicit assertion that no fixture secret substring ever appears
    anywhere in run()'s returned dict (Risk 2).
  - test_dedupe.py: size-grouping filters out singletons, hash-grouping only runs on size-matched
    candidates, full-file SHA-256 correctness (including a same-size/different-tail-content
    negative case the old capped-hash approach would have gotten wrong), structured group output
    shape.
  - test_corral_screenshots.py: dry-run vs --go vs --from-queue (mirroring test_sort.py's
    structure), config.search_roots default vs CLI-supplied dirs vs bash-parity fallback,
    set_screenshot_save_location only called when its own flag is passed (never implied by --go
    alone), NotImplementedError on the Arch adapter caught and reported as skipped not crashing.
  - test_adapters.py: additive cases for set_screenshot_save_location on both implementations
    (MacOSAdapter's subprocess calls mocked/verified; ArchLinuxAdapter's NotImplementedError
    asserted directly, mirroring find_installed_app's existing test precedent).
  - test_cli.py: additive cases for the 3 new subparsers' argument shapes.
  - test_ui_routes.py: additive case for /plan/corral-screenshots' staging/dedup behavior,
    mirroring the existing plan_sort/plan_reclaim test coverage.
```

## 3. Cross-Layer Dependencies

```
Commands (find_wallets) -> OS Adapter (list_dir, resolve_home/resolve_standard_dir only, both
                            pre-existing)
Commands (dedupe) -> OS Adapter (list_dir, resolve_standard_dir, both pre-existing) + stdlib
                      hashlib
Commands (corral_screenshots) -> OS Adapter (list_dir, move, the new
                                  set_screenshot_save_location) + Config (search_roots, read-only,
                                  pre-existing field) + Queue (QueueEntry/stage_entries/
                                  check_staleness, all pre-existing) via --from-queue
UI Routes (plan_corral_screenshots) -> Commands (corral_screenshots.run) + Queue (stage_entries) —
                                        identical shape to the existing plan_sort/plan_reclaim
                                        dependency, no new kind of coupling
CLI Entry Point -> all three Commands modules (plain COMMANDS dispatch, no new dispatch machinery)
Tests -> every layer above already existing/complete before its own test file is meaningful

No layer here is a new shared foundation the way OS Adapter/Config were in harden-cleanup-cli —
every extension point (OSAdapter ABC, QueueEntry/stage_entries, /plan/* route pattern, dry-run/
--go/--from-queue convention) already exists and is being followed, not invented (design-discussion
§5).
```

## 4. Layer Map Diagram

```
HORIZONTAL LAYER MAP
─────────────────────────────────────────────────────────────────────────
Commands       │ find_wallets      │ dedupe            │ corral_screenshots │
               │ (read-only)       │ (read-only)       │ (move + OS pref)   │
───────────────┼───────────────────┼───────────────────┼────────────────────┤
OS Adapter     │ (reuses list_dir/ │ (reuses list_dir/ │ set_screenshot_    │
               │ resolve_*)        │ resolve_*)        │ save_location (new)│
───────────────┼───────────────────┼───────────────────┼────────────────────┤
CLI            │ find-wallets      │ dedupe subparser   │ corral-screenshots │
               │ subparser         │                    │ subparser + flag   │
───────────────┼───────────────────┼───────────────────┼────────────────────┤
UI             │ (none)            │ (none)             │ /plan/corral-      │
               │                   │                    │ screenshots + link │
───────────────┼───────────────────┼───────────────────┼────────────────────┤
Config         │ (none)            │ (none)             │ reuses search_roots│
───────────────┼───────────────────┼───────────────────┼────────────────────┤
Tests          │ test_find_wallets │ test_dedupe        │ test_corral_       │
               │                   │                    │ screenshots (+adapter/cli/ui additions)│
─────────────────────────────────────────────────────────────────────────
```

## 5. Scope Summary

```
HORIZONTAL SCOPE:
  Layers affected: 6 (Commands, OS Adapter, CLI, UI Routes/Templates, Config, Tests) — only
    Commands and Tests are touched by all three ports; OS Adapter/UI/Config are touched by
    corral-screenshots alone.
  Total items: 3 new command modules, 1 new adapter capability (2 implementations), 3 CLI
    subparsers + COMMANDS entries, 1 UI route + 1 template link, 0 config schema changes (1
    additive read), ~6 new/extended test files.
  New vs modified: everything at the Commands/OS-Adapter/UI-route layer is new (no in-place edit
    of existing command modules); cli.py, dashboard.html, adapters/base.py|macos.py|arch_linux.py,
    and the existing test files are additively modified, never restructured.
  Estimated total effort: Medium (per design-discussion §5's Scale Assessment — reproduced there,
    not re-derived here).

  LARGEST LAYER: Commands (corral_screenshots specifically — the only one touching OS Adapter,
    CLI flags, UI, and Config simultaneously).
  RISKIEST LAYER: Commands again, but for a different reason per port — find_wallets carries the
    structural secret-leakage risk (Risk 2), dedupe carries a real correctness/performance
    tradeoff (full uncapped SHA-256), corral_screenshots carries the most invasive single side
    effect in the whole codebase (killall SystemUIServer) — see vertical-plan.md's risk-by-slice
    section for how each is specifically mitigated.
```


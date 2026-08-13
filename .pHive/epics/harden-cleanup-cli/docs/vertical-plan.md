# Vertical Slice Plan: Harden cleanup-tools into a packaged local CLI

**Input:** horizontal-plan.md + design-discussion.md + user feedback.

## 1. Slicing Strategy

```
STRATEGY:
  Total horizontal items: ~10 adapter operations, 3 config pieces, 1 entry point + 3 subcommands,
                          3 ported commands, tests throughout.
  Planned slices: 4
  First slice goal: OS adapter + CLI skeleton exist, `cleanup survey` runs end-to-end on both
                     macOS and Arch Linux, producing real JSON output.
  Final slice goal: `cleanup sort` and `cleanup reclaim` both work end-to-end, config-driven,
                     tested, on both platforms — matching design-discussion §1's "done."

  Slicing rationale: the OS adapter and config loader are the shared foundation every command
  depends on (per horizontal-plan §3). Slicing by command, with survey first (cheapest, no
  config dependency) proves the adapter works before sort/reclaim add config on top of it. This
  mirrors the design discussion's own step ordering (§3), just made verifiable at each boundary.
```

## 2. Vertical Slice Plan

```
## Slice 1: OS adapter + CLI skeleton + `survey`

WHAT WORKS AFTER THIS SLICE:
  Running `cleanup survey` on either macOS or Arch Linux prints real disk/dir-size/screenshot-count
  JSON for that machine — the first real end-to-end proof the adapter, the packaging, and the
  test harness all work together.

LAYERS TOUCHED:
  OS Adapter:
    - file read/write, resolve_home/resolve_standard_dir, disk_usage, list_dir — both macOS and
      Arch implementations (survey needs no move/delete/is_docker_installed/find_installed_app
      yet)
  CLI Framework:
    - `cleanup` entry point, `survey` subcommand (no shared dry-run/--go convention needed yet —
      survey has no flags)
  Commands:
    - survey — JSON output, ported from scripts/survey.sh's disk/dir/histogram/screenshot logic

NOT YET:
  - Config loading (survey doesn't need it)
  - move/delete/is_installed adapter operations
  - sort, reclaim

VERIFIED BY:
  - pytest: OS-adapter unit tests for disk_usage/resolve_home on both platforms (run in CI-less
    local test runs on each machine, or mocked per-OS in a shared test suite — implementation
    detail for the story, not pinned here)
  - pytest: survey's JSON output shape (schema/field assertions, not scraped text)
  - Manual: run `cleanup survey` on the real macOS machine AND the real Arch Linux machine,
    confirm both produce sensible output (this is survey — read-only, safe against real machines)

COMMIT REPRESENTS: OS adapter + packaged CLI skeleton + survey, working on both real platforms

---

## Slice 2: Config loader + bucket-rule mechanism

WHAT WORKS AFTER THIS SLICE:
  A config file at ~/.config/cleanup-tools/ (or built-in defaults if absent) loads and resolves
  bucket rules — this slice has no user-facing command output yet (sort isn't wired to it until
  Slice 3), but the rule mechanism itself is built and tested standalone.

BUILDS ON: Slice 1 (uses the OS adapter's file read + resolve_home — Slice 2 cannot start before
Slice 1 exists; see the corrected moldability note in §6)

LAYERS TOUCHED:
  OS Adapter:
    - (no new operations — reuses read_file/resolve_home from Slice 1)
  Config:
    - bucket_rules schema + matching logic (ext-set + optional filename-pattern → bucket),
      search_roots, master_paths (schema only — reclaim doesn't consume master_paths until
      Slice 4)
    - Built-in defaults so commands work with zero config file present

NOT YET:
  - sort/reclaim wired to consume this config
  - master_paths' backed_up flag actually gating anything (that's Slice 4)

VERIFIED BY:
  - pytest: every extension → bucket mapping from today's bucket() case statement, expressed as
    default rule data, plus the screenshot filename-pattern case
  - pytest: config-loading with no file present (defaults), with a present file (overrides),
    and with a malformed file (clear error, not a silent fallback)

COMMIT REPRESENTS: config loader + bucket-rule mechanism, tested standalone

---

## Slice 3: `sort`, wired to config

WHAT WORKS AFTER THIS SLICE:
  Running `cleanup sort [dir]` stages files into `_sorted/<type>/` using the config's bucket
  rules, dry-run by default, `--go` to act — matching scripts/sort-downloads.sh's behavior
  exactly except for the flag-polarity fix.

BUILDS ON: Slice 1 (OS adapter's move) + Slice 2 (config's bucket_rules)

LAYERS TOUCHED:
  OS Adapter:
    - move(src, dst) — both platforms
  CLI Framework:
    - `sort` subcommand, shared dry-run/--go flag convention (first command to need it)
  Commands:
    - sort — ported logic, consuming Slice 2's bucket_rules instead of a hardcoded case
      statement

NOT YET:
  - reclaim

VERIFIED BY:
  - pytest: dry-run vs --go behavior (files unchanged in dry-run, moved under --go)
  - pytest: every bucket-rule mapping resolves through sort end-to-end, not just at the config
    layer in isolation
  - Manual: one full run-through against a synthetic fixture directory mirroring
    docs/CLEANUP-PLAN.md's shapes (screenshots, installers, pdfs, zips) — not the real
    ~/Downloads, per design-discussion §7

COMMIT REPRESENTS: sort working end-to-end, config-driven, on both platforms

---

## Slice 4: `reclaim`, with its three net-new behaviors

WHAT WORKS AFTER THIS SLICE:
  Running `cleanup reclaim [--go] [--docker]` does everything scripts/safe-reclaim.sh does today,
  PLUS refuses to delete configured master paths until marked backed-up, detects orphaned
  installers, reports GB reclaimed, and only touches Docker when `--docker` is explicitly passed.
  This is the last slice — after this, design-discussion §1's "done" is met.

BUILDS ON: Slice 1 (OS adapter's delete/is_installed) + Slice 2 (config's master_paths)

LAYERS TOUCHED:
  OS Adapter:
    - delete(path), is_docker_installed() — both platforms
    - find_installed_app(installer_path) — macOS only for v1 (matches `.dmg`/`.pkg` against
      `/Applications`); Arch has no direct analog (its package manager already tracks installed
      state, so "orphaned installer file" isn't a meaningful concept there the same way). Team
      review (architect lens) caught that testing this "on both platforms" as originally written
      wasn't meaningful — corrected below.
  CLI Framework:
    - `reclaim` subcommand, `--docker` flag alongside the existing dry-run/--go convention
  Commands:
    - reclaim — ported junk/regenerable detection (unchanged categories from today's bash) +
      master-paths refusal (checked against Slice 2's master_paths[].backed_up) + orphaned-
      installer detection (new) + GB-reclaimed reporting (new) + Docker gated behind --docker

NOT YET:
  - find, dedupe, corral-screenshots (next epics)
  - AI-driven rules, screenshot annotation (later epic)
  - Cross-location tax-folder merge/consolidate (later epic)

VERIFIED BY:
  - pytest: dry-run vs --go (unchanged categories still work exactly as today's bash)
  - pytest: master-paths refusal — attempting to delete a configured master with backed_up=false
    is refused; backed_up=true allows it
  - pytest: orphaned-installer detection against fixture "app already installed" vs "not
    installed" cases — macOS only (see OS Adapter note above); this specific test does not run
    on Arch, unlike every other test in this slice
  - pytest: GB-reclaimed reporting sums correctly across categories
  - pytest: --docker flag gating — docker prune runs only when the flag is passed, never as a
    side effect of --go alone
  - Manual: one dry-run pass on each real machine (macOS + Arch) confirming reported categories
    match what's actually present, before ever running --go against real data

COMMIT REPRESENTS: reclaim working end-to-end with all REQUIREMENTS.md-specified behavior, on
both platforms — epic-complete
```

## 3. Overlay Diagram

```
VERTICAL SLICE OVERLAY
─────────────────────────────────────────────────────────────────────────

              │  Slice 1        │  Slice 2         │  Slice 3        │  Slice 4          │
              │  (survey)       │  (config)        │  (sort)         │  (reclaim)        │
──────────────┼─────────────────┼──────────────────┼─────────────────┼───────────────────┤
OS Adapter    │ read/write,     │ (reuses S1)      │ move            │ delete,           │
              │ resolve_*,      │                  │                 │ is_installed/     │
              │ disk_usage      │                  │                 │ install_hint      │
──────────────┼─────────────────┼──────────────────┼─────────────────┼───────────────────┤
Config        │                 │ bucket_rules,    │ (consumes S2's  │ (consumes S2's    │
              │                 │ search_roots,    │ bucket_rules)   │ master_paths)     │
              │                 │ master_paths     │                 │                   │
──────────────┼─────────────────┼──────────────────┼─────────────────┼───────────────────┤
CLI Framework │ entry point,    │                  │ sort subcmd,    │ reclaim subcmd,   │
              │ survey subcmd   │                  │ dry-run/--go    │ --docker flag     │
──────────────┼─────────────────┼──────────────────┼─────────────────┼───────────────────┤
Commands      │ survey (JSON)   │                  │ sort            │ reclaim (+3 new   │
              │                 │                  │                 │ behaviors)        │
─────────────────────────────────────────────────────────────────────────

Each column is a commit-worthy, working state, verified on both macOS and Arch Linux.
```

## 4. Deferred Items

```
DEFERRED (not in current slice plan):
  - find-wallets, dedupe, corral-screenshots ports (next CLI-hardening epics)
  - Windows OS-adapter implementation (aspirational, not a real target yet)
  - AI-driven screenshot crawl/annotate/index (its own later epic, per user confirmation)
  - Cross-location tax-folder merge/consolidate with approval flow (later epic, per user's
    forward-looking note)
  - Config file format finalization beyond "exists at ~/.config/cleanup-tools/" (implementation
    detail, not a slicing concern)

RATIONALE: each of these either has zero current-epic dependency (find/dedupe/corral-screenshots
are separate commands entirely) or was explicitly deferred by the user during design review
(AI/annotation, cross-location merge) — safe to defer because nothing in Slices 1-4 assumes
they exist.
```

## 5. Risk by Slice

```
RISK PER SLICE:
  Slice 1: Medium — first real use of the OS adapter on both platforms; if the adapter's
           interface is wrong, this is where it surfaces first (cheaply, since survey is
           read-only).
  Slice 2: Low — pure logic + file loading, no destructive operations, easy to test in isolation.
  Slice 3: Medium — first command with real filesystem side effects (moves); flag-polarity
           change is a real behavior change for the user, not just internal.
  Slice 4: Medium-high — most net-new behavior of any slice (3 new behaviors beyond the port),
           and the only slice with actually destructive operations against potentially real data.
```

## 6. Moldability Notes

- Slice 2 cannot move before Slice 1 — team review (researcher lens) caught that an earlier draft
  of this note claimed slices 1/2 were order-independent, which contradicted both Slice 2's own
  "BUILDS ON: Slice 1" and horizontal-plan §3's stated Config→OS-Adapter dependency
  (config's default-location resolution needs resolve_home). What IS true: Slice 2's *content*
  (the bucket-rule schema and matching logic itself) has no logical dependency on survey — it's
  sequenced after Slice 1 only because it needs the adapter's read_file/resolve_home to exist,
  not because survey's own logic matters to it.
- Slice 3 cannot move before Slice 2 (needs bucket_rules) or Slice 1 (needs move()).
- Slice 4 cannot move before Slice 2 (needs master_paths) or Slice 1 (needs delete/is_installed).
- If the OS-adapter interface turns out too narrow once Slice 3 or 4 hits a real need Slice 1
  didn't anticipate (the one open question left in design-discussion §6), the fix is additive —
  extend the interface, not redesign it — since slices 2/3/4 depend on the adapter's existence,
  not its exact final shape.
- No slice can be dropped without dropping its command entirely — this is intentionally the
  minimum slice count (4) for the three in-scope commands plus their shared foundation.

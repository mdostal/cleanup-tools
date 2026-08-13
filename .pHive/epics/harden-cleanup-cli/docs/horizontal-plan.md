# Horizontal Planning Scan: Harden cleanup-tools into a packaged local CLI

**Input:** design-discussion.md + research-brief.md + user feedback (Python runtime, real
cross-platform OS adapter for macOS + Arch Linux, general bucket-rule mechanism, docker-prune
gated behind its own flag, AI/screenshot-annotation deferred to a later epic).

## 1. Layer Inventory

- **OS Adapter** — new. File read/write/move, path/home-dir resolution, and OS-command/
  package-manager discovery (brew on macOS, apt/pacman-family on Arch). Nothing like this exists
  today; every script calls raw shell commands directly.
- **Config** — new. Ext→bucket rule mechanism, search roots, master paths + their backed-up flag.
  No config file exists anywhere in the repo today.
- **CLI Framework** — new. Single entry point + subcommand dispatch + arg parsing. Today there
  are six independent scripts with their own positional-arg parsing, no shared entry point.
- **Commands (survey / sort / reclaim)** — ported from bash, each with a distinct safety profile
  (survey: read-only; sort: non-destructive move; reclaim: destructive, dry-run gated).
- **Tests** — new. Zero tests exist today on any of the six scripts.

`find-wallets`, `dedupe`, `corral-screenshots`, any UI, and any AI-provider plumbing are explicitly
out of this epic's layer inventory — noted here only to make the boundary explicit, not expanded
on below.

## 2. Per-Layer Requirements

```
## Layer: OS Adapter

INTERFACE OPERATIONS NEEDED:
  - read_file(path), write_file(path, content) — used by config loading, reclaim's GB-report
  - list_dir(path, max_depth, pattern) — used by survey (extension histogram, screenshot count,
    node_modules discovery), sort (maxdepth-1 file walk), reclaim (junk-file/build-cache
    discovery). Team review (researcher lens) caught this was missing entirely from the original
    interface list despite being the single most-used operation across all three scripts today
    (every one of them calls `find`).
  - move(src, dst) — used by sort (staging into _sorted/<type>/)
  - delete(path) — used by reclaim, gated by the caller's own dry-run/--go logic (adapter itself
    doesn't know about dry-run; it just performs or doesn't get called)
  - resolve_home() / resolve_standard_dir(kind) — used by survey/sort/reclaim's default paths
    (~/Downloads, ~/Desktop, ~/Documents equivalents)
  - disk_usage(path) — used by survey (replaces macOS-only `df`/`du` parsing)
  - is_docker_installed() — used by reclaim's Docker-related step (docker prune needs Docker
    installed; adapter reports whether it is on this OS, does not auto-install it)
  - find_installed_app(installer_path) — used by reclaim's orphaned-installer detection ("is the
    app this installer would install already present"). Team review (architect lens) caught this
    was originally conflated with `is_installed`/`install_hint` above, but it's a different job:
    exact package-manager lookup (Docker) vs. fuzzy installer→app matching. **This operation is
    macOS-specific in its current form** (matches `.dmg`/`.pkg` against `/Applications`) — Arch
    has no direct analog (its package manager already tracks installed state, so there's no
    "orphaned installer file" concept the same way). Scoped as macOS-only for v1; see vertical-
    plan §5/Slice 4 for how this affects verification.

IMPLEMENTATIONS NEEDED (v1):
  - macOS adapter (BSD-flavored `df`/`stat`, `brew` for install hints)
  - Arch Linux adapter (GNU-flavored `df`/`stat`, `pacman`/`apt`-family for install hints —
    confirm which one is actually on the Arch boxes; Arch itself is pacman-native)

NOT NEEDED (v1):
  - Windows implementation (aspirational only)
  - Any operation `find-wallets`/`dedupe`/`corral-screenshots` need that survey/sort/reclaim don't

---

## Layer: Config

SCHEMA NEEDED:
  - bucket_rules: list of {match: {extensions: [...], filename_pattern: optional}, bucket: name}
    — generalizes bucket()'s case statement AND the screenshot filename override as one rule
    shape, not two special cases
  - search_roots: list of default directories sort/reclaim operate on when no path is passed
  - master_paths: list of {path, backed_up: bool} — backed_up is set manually by the user

FILE LOCATION:
  - ~/.config/cleanup-tools/config.{yaml|toml} (format TBD at implementation; not a design-level
    decision this epic needs to pin further)

LOADING BEHAVIOR NEEDED:
  - Load on every command invocation; sensible built-in defaults if the file doesn't exist yet
    (so `cleanup survey` works with zero setup)

---

## Layer: CLI Framework

ENTRY POINT NEEDED:
  - `cleanup` — single installed command
  - Subcommands: `cleanup survey`, `cleanup sort [dir] [--go]`, `cleanup reclaim [--go] [--docker]`

ARG PARSING NEEDED:
  - Shared flag conventions across subcommands (dry-run-by-default polarity, `--go` to act)
  - Per-command positional args (target dir for sort/reclaim, defaulting to config's search_roots)

---

## Layer: Commands

survey:
  - Disk usage, home-dir top-level sizes, Documents/work biggest-repo sizes, node_modules
    regenerable-space total, Downloads extension histogram, screenshot count — all six sections
    from today's bash (team review caught the first draft here only named four), via the OS
    adapter's disk_usage/list_dir instead of raw `df`/`du`/`find`
  - JSON output (primary), pretty-printed to stdout by default

sort:
  - Walk target dir, apply config's bucket_rules via the OS adapter's move(), stage into
    `_sorted/<type>/`
  - Dry-run by default (flag-polarity fix from today's bash), `--go` to act

reclaim:
  - Junk/regenerable detection (node_modules, build caches, OS junk) — same categories as today
  - NEW: orphaned-installer detection (.dmg/.pkg-equivalent whose app is already installed —
    what "already installed" means per-OS is an OS-adapter question, not a reclaim question)
  - NEW: master-paths refusal-to-delete, checked against config's master_paths[].backed_up
  - NEW: GB-reclaimed reporting
  - Docker pruning behind its own explicit flag (`--docker`), separate from `--go`
  - Dry-run by default, `--go` to act (already the pattern in today's bash — preserved, not fixed)

---

## Layer: Tests

  - OS-adapter unit tests, run against both macOS and Arch Linux implementations
  - Config-loading tests (defaults, malformed file, bucket-rule matching including the
    screenshot-pattern case)
  - Per-command tests: survey's JSON shape; sort's dry-run vs --go and every bucket-rule mapping;
    reclaim's dry-run vs --go, master-paths refusal, docker-flag gating, GB-reporting
  - One manual verification pass: sort against a synthetic fixture directory (not real ~/Downloads)
```

## 3. Cross-Layer Dependencies

```
DEPENDENCIES:

CLI Framework → OS Adapter (entry point needs disk_usage/resolve_home before survey can run)
Commands (survey) → OS Adapter (disk_usage, resolve_standard_dir, list_dir)
Commands (sort) → OS Adapter (list_dir, move) + Config (bucket_rules)
Commands (reclaim) → OS Adapter (list_dir, delete, is_docker_installed, find_installed_app) +
                      Config (master_paths)
Config → OS Adapter (resolve_home, for the default config-file location)
Tests (OS Adapter) → both platform implementations existing
Tests (Commands) → Config + OS Adapter both existing (commands can't be tested in isolation from
                    their two foundations)
```

The OS Adapter and Config are the two layers everything else depends on — this is the same
"shared foundation" dependency the design discussion's Scale Assessment flagged, just made
explicit per-layer here.

## 4. Layer Map Diagram

```
HORIZONTAL LAYER MAP
─────────────────────────────────────────────────────────────────────────

OS Adapter    │ file read/write/  │ path/home-dir  │ command/pkg-mgr    │
              │ move/delete       │ resolution     │ discovery          │
              │ (macOS + Arch)    │ (macOS + Arch) │ (brew / pacman)    │
──────────────┼───────────────────┼────────────────┼────────────────────┤
Config        │ bucket_rules      │ search_roots   │ master_paths       │
              │ (rule mechanism)  │ (defaults)     │ (+ backed_up flag) │
──────────────┼───────────────────┼────────────────┼────────────────────┤
CLI Framework │ entry point       │ subcommand     │ shared dry-run/    │
              │ (`cleanup`)       │ dispatch       │ --go convention    │
──────────────┼───────────────────┼────────────────┼────────────────────┤
Commands      │ survey (JSON)     │ sort (rules,   │ reclaim (masters,  │
              │                   │ dry-run→--go)  │ installers, GB,    │
              │                   │                │ docker flag)       │
──────────────┼───────────────────┼────────────────┼────────────────────┤
Tests         │ adapter (x2 OS)   │ config loading │ per-command        │
─────────────────────────────────────────────────────────────────────────
```

## 5. Scope Summary

```
HORIZONTAL SCOPE:
  Layers affected: 5 (OS Adapter, Config, CLI Framework, Commands, Tests) — 2 of these
    (OS Adapter, Config) are entirely new with no in-repo precedent.
  Total items: ~10 adapter operations, 3 config schema pieces, 1 entry point + 3 subcommands,
    3 ported commands (with reclaim carrying 3 net-new behaviors beyond its current code),
    tests across all of the above.
  New vs modified: everything is new in the ported CLI's code (nothing is "modified" — this is a
    port, not an in-place edit of the bash), but every command's *behavior* is modified relative
    to today's script (flag-polarity fix in sort, 3 new behaviors in reclaim, JSON output in
    survey).
  Estimated total effort: medium.

  LARGEST LAYER: Commands (reclaim specifically — 3 net-new behaviors on top of the port).
  RISKIEST LAYER: OS Adapter (new architecture, only 2 of its eventual platforms are real targets
    today, easy to over- or under-scope its interface).
```

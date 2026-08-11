# Research Brief: Harden cleanup-tools into a packaged local CLI

**Validation note:** codebase-only. context7 and web research were not escalated —
this epic ports existing bash to a new runtime rather than adopting an unfamiliar
third-party library, so there is no external API surface to validate yet. That
gap reopens once a runtime (Node/TS vs Python) and its CLI-framework/hashing
dependencies are chosen — flagged as an open question below. Confidence: **high**
for the current-state findings (all 6 scripts and both docs read in full);
**medium** for forward-looking recommendations (no packaging precedent in-repo).

## Summary

`cleanup-tools` is six standalone bash scripts (67 lines total) plus two planning
docs. There is no shared code between scripts — every script re-implements its own
argument parsing, its own dry-run gate (where destructive), and its own file
walking. `docs/REQUIREMENTS.md` already specifies the target: harden these into a
packaged, testable Node/TS or Python CLI with a config file for the ext→bucket map,
master-paths, and search-roots, fully local (no network, no telemetry). This epic
is scoped to the CLI hardening only — no UI, no AI-provider plumbing (per the
project's `north_star.avoid`).

## Key files & surfaces

- `scripts/survey.sh` (8 lines) — read-only snapshot: disk, top-level dir sizes,
  node_modules total, Downloads extension histogram, screenshot count. Pure
  `du`/`find`/`awk` pipeline to stdout; no args, no config.
- `scripts/sort-downloads.sh` (16 lines) — the bucket-stager. `bucket()` (lines
  5-10) is a bash `case` mapping extension → bucket name, with one content-aware
  override (`screenshot*` filename check on image extensions, line 6). This is the
  exact logic `docs/REQUIREMENTS.md` says to make config-driven.
  `DIR="${1:-$HOME/Downloads}"; DRY=""; [ "$2" = "--dry" ]` (line 4) is positional,
  order-sensitive arg parsing — no flag parser.
- `scripts/corral-screenshots.sh` (8 lines) — move-only + one macOS-specific side
  effect: `defaults write com.apple.screencapture location ...` (line 7). This is
  the only script that mutates system state outside the filesystem tree it scans.
- `scripts/safe-reclaim.sh` (12 lines) — the **reference dry-run pattern**: `GO="";
  [ "$1" = "--go" ] && GO=1` (line 3) then every destructive action is gated
  through a `say()` helper (line 4) that either prints or prints-and-executes.
  Covers node_modules/`.next`/`.turbo`/`dist`, OS junk, `$RECYCLE.BIN`, and shells
  out to `docker system prune -af --volumes` (line 11) unconditionally when `--go`
  — no per-category opt-out.
- `scripts/find-wallets.sh` (17 lines) — two independent passes: filename `find`
  (lines 6-11) and content `grep -rlIE` with a combined regex (line 13) covering
  BIP39-shaped phrases, `xprv`, PEM private keys, and Ethereum keystore JSON. Both
  passes print **paths only** — the content pass uses `-l` (files-with-matches),
  never printing the matched text. This paths-only invariant is the single
  highest-stakes behavior to preserve when porting.
- `scripts/dedupe.sh` (6 lines) — single dense pipeline: `stat` for size → group by
  size → `shasum` only within same-size groups → group by hash. Read-only, no
  flags. The one-liner-per-stage style makes this the hardest script to port
  behavior-for-behavior without an actual test harness.
- `docs/REQUIREMENTS.md` — the authoritative target spec: components to build
  (survey/sort/corral/reclaim/find/dedupe + a "keep-clean" recurring trigger not
  yet built), the four hard rules, and `## Stack` naming Node/TS or Python as the
  candidates with an optional local UI later.
- `docs/CLEANUP-PLAN.md` — the grounded survey this repo was built to execute
  against (real dir sizes, categories, phased manual plan). Useful as the
  acceptance-test scenario source (e.g. "857 Downloads items → near zero"), not
  as a spec for the tool itself.
- `.pHive/project-profile.yaml` → `north_star` — states the priority ordering
  explicitly: local-first CLI first, packaging/distribution and AI-provider
  plumbing later. `.pHive/CONTEXT.md` carries the domain glossary (masters,
  buckets, `_REVIEW`, dry-run/`--go`, wallet finder) this epic's docs should reuse
  rather than re-defining.

## Patterns & conventions

- **Dry-run-by-default, explicit `--go`/`--dry` to change the default direction**
  — but the two existing scripts that need this invert the flag polarity
  (`sort-downloads.sh` defaults to *acting* unless `--dry` is passed;
  `safe-reclaim.sh` defaults to *not acting* unless `--go` is passed). A ported
  CLI should pick one polarity and apply it uniformly — `docs/REQUIREMENTS.md`'s
  hard rule ("dry-run by default... `--go` to act") implies `safe-reclaim.sh`'s
  polarity is the target, so `sort-downloads.sh`'s default needs to flip.
- **Move, don't copy, for staging** — `sort-downloads.sh` and
  `corral-screenshots.sh` both use `mv`, so the original location is empty after
  staging (reversible by moving back, not by re-running).
- **Paths-only output for anything sensitive** — `find-wallets.sh`'s two-pass
  design (filename + content-via-`-l`) is the concrete precedent for "never print
  matched material," which the requirements doc also states as a hard rule for
  any future secrets handling.
- **No shared code today** — every script is self-contained; there is no existing
  "extract the common bits" pattern in-repo to follow. The port is greenfield with
  respect to internal architecture.

## Constraints

- **No network, no telemetry** (`docs/REQUIREMENTS.md`, `north_star.avoid`) — the
  packaged CLI must not phone home, not even for update checks or crash reporting,
  since it walks personal file trees.
- **Never delete without a backup of masters** — `safe-reclaim.sh` today has no
  concept of "masters" at all; it only handles regenerable junk. The master-paths
  refusal-to-delete behavior described in `docs/REQUIREMENTS.md` does not exist
  yet anywhere in the current scripts — it's new behavior, not a port.
  `docs/CLEANUP-PLAN.md`'s guardrails section names the concrete masters (SMS
  backup, family footage, tax docs, patent app, `desktop back`). REQUIREMENTS.md
  also implies persisted "backed up" state (masters are blocked "until they're
  marked backed-up") that no current script or doc specifies the mechanism for.
- **`reclaim`'s spec is bigger than `safe-reclaim.sh`'s current behavior** —
  `docs/REQUIREMENTS.md`'s `reclaim` component also calls for orphaned-installer
  detection (`.dmg`/`.pkg` whose app is already in `/Applications`) and reporting
  GB reclaimed, neither of which exists in `safe-reclaim.sh` today. Both are gaps
  against the current script, same as the master-paths behavior above — not
  optional extras.
- **macOS-only assumptions are not confined to the out-of-scope scripts** —
  `stat -f%z` (BSD stat, `dedupe.sh` line 4), `defaults write
  com.apple.screencapture` (`corral-screenshots.sh` line 7), and `killall
  SystemUIServer` are macOS-specific, but so is `survey.sh` line 3: `df -h / |
  sed -n '1p;/disk/p'` filters on the literal string "disk," which matches macOS
  device names (`disk3s1s1`) but prints nothing on Linux (`sda1`, `nvme0n1`).
  `survey` is in this epic's scope, so this isn't just a future-porting note.
  `docs/REQUIREMENTS.md` doesn't state a cross-platform goal, so this is worth
  confirming rather than assuming — see open questions.
- **`docker system prune` is unconditional and destructive** — no filter, no
  per-category confirmation; porting this as-is into a "safe reclaim" story needs
  explicit scoping (all Docker layers/volumes, not just this project's).

## Risks

- **Medium — behavior drift during the port.** None of the six scripts have
  tests. Porting `bucket()`'s case statement, the wallet regex, or dedupe's
  size→hash grouping into a new language risks silent behavior changes with
  nothing to catch them. Mitigate by writing tests *from the current bash
  behavior* before/alongside the port (this is also why `classic` methodology,
  not `tdd`, was the kickoff default — there's no existing test-first signal to
  build on, but tests are still explicitly in scope per `docs/REQUIREMENTS.md`).
- **Medium — flag-polarity inconsistency (`--go` vs `--dry`) could get ported
  as-is** and become a permanent footgun in the packaged CLI. Should be resolved
  as a design decision (see design discussion), not silently carried forward.
- **Low-medium — `docker system prune -af --volumes` is destructive beyond this
  project's scope** (removes ALL unused Docker resources on the machine, not just
  ones related to cleanup-tools). Worth a hard rule or explicit confirmation in
  the packaged version.
- **Low — macOS-specific commands break silently on other platforms** if
  cross-platform support is ever assumed without being addressed.

## Open questions

1. **Runtime choice: Node/TS or Python?** `docs/REQUIREMENTS.md` leaves this open.
   Node/TS gives a natural path to the planned local desktop UI (Electron/Tauri
   commonly pair with a TS core); Python has stronger BSD-`stat`/hashing ergonomics
   and no packaging-runtime story of its own (needs PyInstaller or similar for a
   "download and run" experience). This is a real architectural decision, not a
   detail — recommend resolving it in the design discussion with architect input.
2. **Cross-platform or macOS-only?** Every current script assumes macOS
   (`stat -f%z`, `defaults write`, `com.apple.screencapture`). Confirm whether v1
   targets macOS only (matches "the author's own Mac" from `north_star.audience`)
   or should abstract the OS-specific calls now to avoid a second port later.
3. **What does "packaged" mean for v1?** A single global npm/pip install the
   author runs from a terminal, or a signed/notarized downloadable binary? This
   affects Phase 1 scope significantly and should be pinned down before H/V
   planning, if this epic reaches Medium scope.
4. **Config file location/format** — `docs/REQUIREMENTS.md` calls for "a config
   file for the ext→bucket + master-paths + search-roots" but doesn't specify
   where it lives (`~/.config/cleanup-tools/`, a repo-relative default, etc.) or
   its format (YAML/JSON/TOML). Needed before the `sort`/`reclaim`/`find` stories
   can be written concretely.

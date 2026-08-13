# Design Discussion: Harden cleanup-tools into a packaged local CLI

## 1. What Are We Doing?

Right now `cleanup-tools` is six bash scripts you run by typing `bash scripts/whatever.sh`.
They work, but they're reference-quality — no tests, no shared code, no packaging, and (as I
found reading them closely) two scripts that disagree with each other about which way the
dry-run flag defaults, plus platform-specific commands baked directly into the logic. The goal
of this epic is to turn that reference code into a real local CLI: one thing you install/run
(Python, packaged via pip), with a config file instead of hardcoded buckets and paths, a
cross-platform file/command abstraction instead of raw macOS-only shell calls, tests that lock
down behavior that currently only exists as bash one-liners, and a consistent safety model
across every command.

"Done" for this epic specifically means: `survey`, `sort`, `reclaim` exist as commands in a
packaged Python CLI (not raw scripts), running on both macOS and Arch Linux (your two real
machines today), behavior-equivalent to today's bash plus the fixes below, with tests. I'm
deliberately NOT touching `find-wallets`, `dedupe`, or `corral-screenshots` in this epic, and
I'm deliberately NOT touching UI or AI-provider plumbing — you confirmed the AI-driven
screenshot crawl/annotate/index vision is its own later epic; this epic sorts by type and
filename pattern only, same as the bash today. Trying to do all six commands plus packaging plus
AI in one epic would blow past "thin first slice."

## 2. What I Found

- `scripts/survey.sh` — 8 lines, pure read-only reporting (`du`/`find`/`awk` to stdout). No
  flags, no config. The simplest of the six, and a good "does the harness even work" first
  story. Line 3's `df -h / | sed -n '1p;/disk/p'` filters on the literal string "disk," which
  matches macOS device names (`disk3s1s1`) but prints nothing on Arch (`sda1`, `nvme0n1`) — this
  needs the OS adapter (see below), not a platform-specific hack.
- `scripts/sort-downloads.sh` — 16 lines. `bucket()` (lines 5-10) is a bash `case` statement
  mapping extension → bucket name, with one content-aware rule: an image extension named
  `screenshot*` goes to `screenshots`, not `photos` (line 6). This is exactly the "ext→bucket
  map, plus content-aware rules" `docs/REQUIREMENTS.md` asks for. You confirmed you want a real
  general rule mechanism here, not a hardcoded special case, partly because it's the natural
  future plug point for the AI-driven rules from the later epic.
- `scripts/safe-reclaim.sh` — 12 lines. This is the *reference* dry-run pattern: `GO=""; [ "$1" =
  "--go" ] && GO=1` then a `say()` helper that either prints or prints-and-deletes. Every
  destructive action funnels through it — I want the packaged CLI's safety model to look like
  this, not like `sort-downloads.sh`. Its spec is bigger than its current code, too:
  `docs/REQUIREMENTS.md` also wants orphaned-installer detection (`.dmg`/`.pkg` whose app is
  already installed) and GB-reclaimed reporting, neither of which exists yet.
- **The two scripts disagree on flag polarity.** `sort-downloads.sh` defaults to *acting* unless
  you pass `--dry`; `safe-reclaim.sh` defaults to *not acting* unless you pass `--go`.
  `docs/REQUIREMENTS.md`'s hard rule ("dry-run by default... explicit `--go` to act") settles this
  in `safe-reclaim.sh`'s favor — `sort`'s default needs to flip.
- `docker system prune -af --volumes` in `safe-reclaim.sh` (line 11) is unconditional whenever
  `--go` is passed — it prunes ALL unused Docker resources on the machine, not just anything this
  tool would otherwise touch. You confirmed this needs its own gate, not a ride-along on `--go`.
- `.pHive/CONTEXT.md` already has the vocabulary (buckets, masters, `_REVIEW`, dry-run/`--go`) —
  reusing those terms rather than inventing new ones.

## 3. My Proposed Approach

Runtime is **Python**, confirmed. Platform target is **macOS + Arch Linux**, both real machines
you use today (Windows is explicitly aspirational/lower-priority, not a v1 target).

1. **Design the OS-adapter interface first** — this now blocks everything else, more than the
   runtime choice did. A small interface covering what `survey`/`sort`/`reclaim` actually need:
   file read/write/move, path conventions (home dir, standard folders), and OS-command/
   package-manager discovery (e.g. "is this installed, and if not, what installs it" —
   `brew` on macOS, `apt`/`pacman`-family on Linux). Two concrete implementations for v1 (macOS,
   Arch Linux), with the interface shaped so a third (Windows) is addable later without
   redesigning it. This is genuinely new architecture — nothing in the current bash gives us a
   pattern to crib from, so I want to keep the interface's surface area small (just what the
   three in-scope commands need) rather than speculatively broad.
2. **Set up the package skeleton** — a single CLI entry point with subcommands (`cleanup survey`,
   `cleanup sort`, `cleanup reclaim`), built against the OS adapter from step 1, not raw
   platform calls.
3. **Port `survey` first** — read-only, zero flags, cheapest way to prove the packaging/test
   harness and the OS adapter both work before touching anything with a safety model attached.
   Output format: **structured JSON**, not just human-readable stdout — `docs/REQUIREMENTS.md`
   says "output JSON so the UI/**other tools** can consume it," and that second half isn't
   UI-gated: JSON is what makes `survey` mechanically testable (assert on parsed fields, not
   scraped text) and the natural fixture format for the not-yet-built keep-clean trigger.
   Pretty-printed to stdout by default, so it stays human-readable too.
4. **Design the config schema** — a small general rule mechanism (ext-set + filename-pattern →
   bucket, confirmed) for the bucket map, replacing `bucket()`'s case statement, plus a place for
   search roots. Master-paths' *location* belongs here too; the *refusal-to-delete* behavior
   itself is a `reclaim`-story concern (see step 6), so it doesn't silently expand this story.
   Config file lives at `~/.config/cleanup-tools/` (my recommendation, proceeding absent
   objection) rather than repo-relative, since the tool is meant to run against your real home
   directory, not this repo.
5. **Port `sort`** against the new config and OS adapter, fixing the flag-polarity issue (default
   dry, explicit `--go`, keep move-based staging into `_sorted/<type>/`), and add tests that lock
   in the rule mechanism against every mapping the current `case` statement encodes, including the
   screenshot override.
6. **Port `reclaim`** against `safe-reclaim.sh`'s existing dry-run pattern and the OS adapter,
   covering all three gaps against `docs/REQUIREMENTS.md`: master-paths refusal-to-delete (new,
   tested behavior — a master is refused until marked backed-up via a manual flag you set in the
   config yourself, my recommendation, proceeding absent objection — the tool doesn't try to
   verify a backup happened, it trusts your say-so, consistent with "nothing gets deleted without
   your say-so" from `docs/CLEANUP-PLAN.md`), orphaned-installer detection, and GB-reclaimed
   reporting. Docker pruning moves behind its own explicit flag, confirmed, separate from `--go`.
7. **Tests throughout, not bolted on at the end** — methodology is `classic` (no existing
   test-first signal at kickoff), but every story ships its own tests, including OS-adapter tests
   for both platforms.

I'm explicitly not designing the `find`/`dedupe` port, Windows support, AI-driven rules, or the
cross-location tax-folder-merge-with-confirmation idea you raised — all noted as future-epic
material, not this pass's scope. "Packaged" for this epic means a terminal-installable CLI (pip
install, run from your shell) — my recommendation, proceeding absent objection; anything like a
signed binary is the later distribution epic's problem.

## 4. What Could Go Wrong

- **Medium-high — the OS adapter is new architecture with no in-repo precedent, built for exactly
  2 platforms today.** Getting its interface shape wrong (too narrow: forces workarounds later;
  too broad: speculative generality for a Windows target that isn't real yet) is the single
  biggest design risk in this epic, bigger than the flag-polarity or config-schema risks below.
  Keeping its surface area pinned to what `survey`/`sort`/`reclaim` actually call is the mitigation.
- **Medium — behavior drift during the port.** Zero tests exist today on any of these six
  scripts. `bucket()`'s case statement and `safe-reclaim.sh`'s find/prune pipeline could shift
  behavior subtly when rewritten, and nothing would catch it except a human noticing later. Every
  story carries its own test step instead of deferring tests.
- **Medium — flag-polarity fix changes muscle memory.** If you've been running
  `sort-downloads.sh` expecting it to act by default, flipping it to dry-by-default is a real
  behavior change for you, not just an internal refactor.
- **Low — `docker system prune` gated behind its own flag is a behavior change from today's
  `safe-reclaim.sh`**, where any `--go` prunes Docker too. Worth a one-line callout in the
  eventual release notes so it's not read as "reclaim stopped cleaning Docker."
- **Low — no config file exists yet anywhere in the repo.** Designing the schema is new work with
  no existing pattern to crib from; keeping it minimal for v1 (rule-mechanism bucket map + search
  roots + master paths) rather than anticipating every future need.

## 5. Dependencies and Constraints

- **Hard constraint: no network, no telemetry.** The pip-installed CLI must not phone home — no
  update pings, no crash reporting, no analytics.
- **Hard constraint: never delete without backing up masters.** Not yet implemented anywhere; new
  behavior this epic introduces for `reclaim`, gated on a manually-set config flag (§3 step 6).
- **No CI, no linters, no pre-commit hooks exist yet** (confirmed at kickoff). Adding a test
  runner is this epic's job; not separately scoping "set up CI" unless you want it folded in.
- **This epic depends on nothing upstream** — first epic for this project. It blocks the eventual
  desktop-UI epic (needs a stable core to wrap), the AI-provider-plumbing/screenshot-annotation
  epic (needs a place to plug in — the bucket rule mechanism from step 4 is one such plug point),
  and a future cross-location merge/consolidate epic (the tax-folders-from-multiple-places idea)
  — all per your stated priority ordering.

## 6. Open Questions

All originally-open questions are now resolved except where noted "proceeding absent objection"
above (packaged-CLI meaning, config file location, master-backup mechanism) — flag any of those
three if you want a different answer. One question remains genuinely open:

1. **How thin should the OS-adapter interface be for v1?** My lean, per §3 step 1: only the
   concrete operations `survey`/`sort`/`reclaim` call today (file read/write/move, home-dir/
   standard-folder resolution, "is X installed / how do I install X" per package manager) — not a
   general-purpose cross-platform toolkit. This is the one design decision in this epic I'd want
   validated during implementation (does the real Arch Linux port actually need anything the
   interface didn't anticipate?) rather than fully nailed down now.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest.
  Platforms: macOS and Arch Linux — both are real machines you use today; the OS adapter is
             tested against both, not just designed for both.
  Automated: the bucket rule mechanism (every mapping the current case statement encodes, plus
             the screenshot override, expressed as data), sort's dry-run vs --go behavior,
             reclaim's dry-run vs --go behavior, reclaim's new master-paths refusal-to-delete,
             reclaim's docker-prune flag gating, survey's JSON output shape, and the OS adapter
             itself on both platforms.
  Manual: one full run-through of `sort` against a synthetic fixture directory built to mirror
          the shapes in `docs/CLEANUP-PLAN.md` (screenshots, installers, pdfs, zips, mixed
          extensions) — NOT your actual ~/Downloads. Running new, untested code against real
          personal files for its first verification pass is exactly the risk this project's own
          hard rules exist to avoid; running against the real ~/Downloads is a reasonable
          follow-up once the fixture-based tests and this manual pass both pass clean, on both
          machines.
  Not verifying: Windows (aspirational only, not a v1 target), the not-yet-built find/dedupe
                 ports, anything UI- or AI-related.
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~14-18 (OS-adapter interface + 2 platform implementations + package skeleton +
                  config schema/rule mechanism + 3 ported commands + their tests) — larger than
                  the original ~10-14 estimate now that a real cross-platform adapter is in scope.
  Subsystems: OS-adapter layer (new), CLI entry point/arg parsing, config loading, survey, sort,
              reclaim — two layers now (adapter + CLI core) rather than one, still no cross-stack
              work (no UI, no backend service, no AI plumbing).
  Migration required: no data migration; behavioral migration only (flag-polarity fix in sort,
                       docker-prune flag gating in reclaim).
  Cross-team coordination: no — solo project.
  Unknowns: 1 genuinely open question (OS-adapter interface thinness, §6); 3 answered by default
            pending objection (packaged-CLI meaning, config location, master-backup mechanism).

  RECOMMENDATION: Still needs a short horizontal/vertical slice pass before story-writing — now
  more because of the two-layer split (adapter vs. CLI core) than because of open questions, most
  of which resolved during this review. The adapter has to exist before survey/sort/reclaim can
  be ported against it; getting that sequencing explicit is the point of the H/V pass, kept
  lightweight per the earlier team review (this is confirming a build order across two layers,
  not mapping cross-team coordination).
  RATIONALE: Still single project, no coordination, no migration — but two real subsystems
  (adapter + CLI core) instead of one, and the adapter is genuinely new architecture with no
  in-repo precedent. That combination is why this stays Medium rather than dropping to Small now
  that most open questions are resolved.
```

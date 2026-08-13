# Design Discussion: Port the remaining bash scripts (find-wallets, dedupe, corral-screenshots)

## 1. Goal

`north_star.pain_points` (`.pHive/project-profile.yaml:152`) names two things this epic exists to
close out: **"473+ loose screenshots"** and **"the lost-wallet recovery need"**. Two epics have
already turned `survey`/`sort`/`reclaim` and the approval-queue/AI/UI layer into a packaged,
tested, cross-platform CLI. `find-wallets.sh` (wallet recovery) and `corral-screenshots.sh`
(the screenshot problem) are the two scripts that address those named pain points most directly,
and — together with `dedupe.sh` — the only ones still hard-coded, untested, macOS-only bash
outside that packaged CLI (`README.md:26`).

"Done": `cleanup find-wallets`, `cleanup dedupe`, and `cleanup corral-screenshots` exist as real
subcommands, behavior-equivalent to (and, where the research brief flagged a bug, deliberately
better than) today's bash, running on macOS with graceful degradation on Arch Linux where a
capability is genuinely macOS-only (`find_installed_app` already sets this precedent for
`reclaim`). `find-wallets`/`dedupe` stay read-only, matching their existing safety properties.
`corral-screenshots` moves files, so it graduates into `sort`/`reclaim`'s
dry-run/`--go`/`--from-queue`/queue-staging pattern, including a "Plan: Corral Screenshots"
trigger in the approvals UI, so screenshot moves get the same human-review path file moves
already get instead of a CLI-only escape hatch.

## 2. Proposed Approach

Three stories, one per script:

### Story A — `cleanup find-wallets`

New `commands/find_wallets.py`, `run(adapter, args=None) -> dict`, following `survey.py`'s
read-only shape. Two sub-scans matching the bash script's two sections:

- `_by_filename`: loop the 13 filename patterns over `adapter.list_dir(root, pattern=...)`
  (mirroring `reclaim.py`'s `_os_junk_category`, which already loops multiple patterns per
  adapter call — no new adapter surface needed), excluding `node_modules`/`Library/Caches`,
  capped at 200 like the bash `head -200`.
- `_by_content`: walk `Documents`/`Desktop`/`Downloads` for the 5 allowed extensions, run the
  4-alternative regex, return **paths only** — never a match/snippet, never `match.group()`
  anywhere in the return value (see Risk 2 — the return *shape* must make leaking structurally
  impossible, not just avoided by convention).
- Each hit is tagged with match type (`"filename"`/`"content"`) and which specific
  pattern/alternative fired — satisfying the task brief's "match-type + confidence" ask via the
  matched-pattern identity itself, not a fabricated numeric score.
- `cli.py`: new subparser, positional `root` (`nargs="?"`, default `None` →
  `adapter.resolve_home()`, matching bash's `${1:-$HOME}`) — no `--go`, no `--from-queue`.
- No queue/UI integration (Open Question 1).

### Story B — `cleanup dedupe`

New `commands/dedupe.py`, `run(adapter, args=None) -> dict`, also read-only, `survey.py`-shaped:

- `_group_by_size`: `adapter.list_dir(target_dir, pattern=None)` excluding `node_modules`, then
  group by `Path.stat().st_size` in Python (a cheap per-file stat, not the recursive-tree problem
  that justifies `dir_size_bytes`'s `du` shell-out). Keep only groups with 2+ members — this *is*
  the bash script's fast pre-filter, expressed directly instead of via sort/awk/sort-u.
- `_group_by_hash`: hash every size-matched candidate, regroup by hash. Strength/cap is Open
  Question 2 — land whichever it resolves to (full SHA-256, most likely), not bash's
  SHA-1-truncated-to-48-bits.
- Return shape: `{"dir": ..., "duplicate_groups": [{"hash", "size_bytes", "paths": [...]}]}` —
  structured groups, not raw line pairs.
- `cli.py`: new subparser, positional `dir` (`nargs="?"`, default → downloads dir, matching
  `sort_parser`'s convention) — no `--go`, no `--from-queue`.
- No queue/UI integration this pass (Open Question 3).

### Story C — `cleanup corral-screenshots` (+ queue/UI integration)

New `commands/corral_screenshots.py`, structured as `sort.py`'s closer sibling (`reclaim.py`'s
master-path-refusal machinery doesn't apply — no such check exists for moves today; Risk 6):

- `_plan(adapter, target_dirs)`: for each root (default Desktop/Downloads/Documents, matching
  bash, but see Open Question 4), `adapter.list_dir(d, max_depth=2, pattern="screenshot*")` —
  literally `survey.py`'s existing `_screenshot_count` call (`survey.py:90-99`), minus Pictures
  (the destination). Each match → `{"src", "dest": pictures_screenshots_dir / name,
  "dest_exists"}`, mirroring `sort.py:39-51` field-for-field.
- `run(adapter, args=None)`: always computes the full plan; `--go` gates `adapter.move()` calls,
  each isolated via `try/except OSError` like `sort.py:197-210`; `dest_exists` entries skipped
  (this *is* bash's `mv -n`, made exact instead of the pre-move-count approximation flagged in
  the research brief). `--from-queue` executes approved `move` entries via a `_run_from_queue`
  helper structurally identical to `sort.py:65-149`.
- The `defaults write com.apple.screencapture location ...` / `killall SystemUIServer` side
  effect becomes a new `OSAdapter.set_screenshot_save_location(path)` abstract method,
  `MacOSAdapter` implementing the two-command sequence, `ArchLinuxAdapter` raising
  `NotImplementedError` — mirroring `find_installed_app` exactly (`adapters/base.py:254-264`).
  Not wired into `--go` by default in this proposal, given it's categorically more invasive
  ("restarts a running system process") than anything else this tool does (Open Question 5).
- `cli.py`: new subparser mirroring `reclaim_parser`'s multi-root shape (`cli.py:84-92`) rather
  than `sort_parser`'s single-dir one — `dir` (`nargs="*"`), `--go`, `--from-queue`.
- **UI integration** (what makes Story C bigger than A/B):
  - `ui/routes.py`: `_stage_corral_screenshots_plan(adapter)`, structurally identical to
    `_stage_sort_plan` (`routes.py:194-214`) — dry-run `corral_screenshots.run()`, convert each
    plan entry into
    ```python
    queue_module.QueueEntry(
        action="move", src=str(item["src"]), dest=str(item["dest"]),
        source="ui-plan-corral-screenshots", group_key="corral-screenshots",
        plan_snapshot=queue_module.build_plan_snapshot(item["src"]),
    )
    ```
    then `stage_entries()`. A single flat `group_key="corral-screenshots"` (vs. `sort`'s
    per-bucket key) is proposed since there's only one kind of action here — open to revisiting
    if bulk-approving all N moves at once turns out too coarse.
  - `@bp.route("/plan/corral-screenshots")` — identical shape to `plan_sort`/`plan_reclaim`
    (`routes.py:246-265`).
  - `dashboard.html:38`'s "Plan: Sort / Plan: Reclaim" line gets a third link.
  - No new thumbnail/approval-route code needed — the existing `is_image_entry`/`thumbnail`
    machinery already renders previews for any image-extension queue entry, for free.

## 3. Open Questions

1. **Do `find-wallets`/`dedupe` need any UI surface, even read-only?** Brief scopes them
   CLI-only; I agree — neither produces an approve/reject decision, and `find-wallets`' safety
   model rests on being run and read deliberately (thumbnailing a *matched wallet file* would be
   a much higher-stakes surface than anything the UI renders today). `dedupe`'s structured
   output, though, is exactly the shape a future "approve which duplicates to delete" epic would
   want — confirm you're fine with CLI-only for now rather than half-building UI plumbing no
   epic yet consumes.
2. **Dedupe hash: full uncapped SHA-256, or `queue.py`'s existing prefix-capped SHA-256
   (`CONTENT_HASH_MAX_BYTES` = 8 MiB)?** Reusing the cap unexamined risks false-positive
   duplicate claims on same-size large files that diverge only past 8 MiB — worse here than for
   `queue.py`'s staleness check, since dedupe's entire output is an identity claim. Lean: full
   SHA-256, uncapped, accepting the slower worst case since it's already gated behind the cheap
   size pre-filter — real tradeoff, needs sign-off.
3. **Should dedupe's output feed the approval queue at all, even as a non-actionable marker?**
   Lean: no — `action` is currently `"move"`/`"delete"` only, both meaning "will actually
   happen"; a third pseudo-action with no executor is scope creep with no consumer yet. Flagging
   in case there's a near-term use I'm not weighing correctly.
4. **`corral-screenshots`' default roots: hardcode (bash parity), or config-driven like
   `reclaim`'s `search_roots`** (`reclaim.py:418-431`)? Lean: reuse `search_roots` rather than
   invent a screenshot-specific key — but that means one config edit now silently affects two
   commands, worth explicit sign-off.
5. **Should the screenshot-save-location change ship in this epic, and under what gate?** Three
   options: (a) unconditional under `--go`, matching bash exactly; (b) its own explicit flag
   (e.g. `--set-default-location`), independent of `--go`; (c) drop it, leave it manual. Lean:
   (b) — dropping it (c) leaves "stop new screenshots recurring" half-solved, but bundling a
   Dock/menu-bar restart silently into every `--go` (a) is a bigger behavior change than either
   prior epic ever made. **This is the single most consequential open question here** — it
   changes both the CLI and adapter surface, so I'd like it resolved before Story C's
   implementation starts.

## 4. Risks

- **Wallet-detection false negatives/positives from an imprecise regex/glob port** (research
  brief Risk 1) — the 13 globs and 4 regex alternatives carry real semantic weight
  (`xprv[0-9A-Za-z]{20,}` is a specific BIP32 key-prefix check). Mitigation: port as literal
  constants with tests asserting each still matches/doesn't-match bash's exact fixture strings.
- **Content-match leakage risk is structural, not conventional** (Risk 2) — `re.search` hands
  back a `Match` with `.group()` available; nothing stops a future edit from logging it.
  Mitigation: the scanning function returns only path + which-alternative-fired, never touches
  `.group()` anywhere, and a test asserts no fixture-secret substring ever appears in output.
- **Content-search performance at this project's own clutter scale** (Risk 3) — a naive
  Python read-and-regex loop could be materially slower than `grep`'s C implementation.
  Mitigation: benchmark against a realistic fixture tree; shelling to `grep -rlIE` (mirroring
  `dir_size_bytes`'s existing `du` shell-out for the same reason) is a legitimate fallback.
- **`corral-screenshots` has no master-path awareness, because `sort.py` (its closest
  precedent) has none either** (Risk 6) — a screenshot inside a configured, not-yet-backed-up
  master path would move with zero refusal check as proposed. Pre-existing gap in `sort` too,
  not new to this epic, but worth a deliberate "same as sort today" sign-off rather than
  inheriting it unexamined for brand-new code.
- **`killall SystemUIServer` is a new category of invasiveness** — see Open Question 5. Wrong
  gate here has the most direct blast radius (a live Dock/menu-bar restart on the real machine).
- **Dedupe hash weakening risk** if bash's SHA-1-truncated approach ports forward unexamined
  (Risk 4 / Open Question 2) — collision probability is low either way, but a materially
  different promise than full SHA-256 for output people may eventually delete files based on.
- **Dedupe hashing performance at scale** — bounded by the size pre-filter, but a large
  duplicate-heavy Downloads folder (repeated installer re-downloads, video exports) could still
  mean hashing a non-trivial number of large files; worth the same fixture benchmark as above.

## 5. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~14-18 — 3 new command modules + cli.py subparser/COMMANDS additions +
                  ui/routes.py (staging helper + route) + dashboard.html (one link) +
                  adapters/base.py (one new abstract method) + adapters/macos.py (its impl) +
                  adapters/arch_linux.py (NotImplementedError stub) + config.py (only if Q4
                  reuses search_roots) + ~6-8 new test files, one per command (matching
                  test_sort.py/test_reclaim.py/test_survey.py's existing pattern).
  Subsystems touched: commands/ (3 new modules), cli.py, adapters/ (1 new capability across 3
                      files), ui/ (routes + 1 template edit) — no new subsystem, unlike
                      harden-cleanup-cli (built the adapter layer) or ai-approvals-ui (built
                      queue/AI/Flask). Every extension point needed here (OSAdapter ABC,
                      QueueEntry/stage_entries, /plan/* route pattern, dry-run/--go/--from-queue)
                      already exists and is being followed, not invented.
  Migration required: none — no schema change to config.py/queue.py's persisted shapes (unless
                      Q4 adds an additive config read).
  Cross-team coordination: none — solo project, same as both prior epics.
  Unknowns: 5 open questions; none block starting Stories A/B (self-contained read-only ports).
            2 of 5 (Q4, Q5) block finalizing Story C, since they change its CLI/adapter surface.

  RECOMMENDATION: Medium.
  RATIONALE: Smaller in kind than either prior epic — no new subsystem, three individually modest
  scripts (17/6/8 lines of bash), every pattern needed has a direct cited precedent (survey.py,
  sort.py, find_installed_app, _stage_sort_plan). That alone argues for Small. What keeps it at
  Medium: (a) three independent ports bundled together — A/B are near-Small alone, but C carries
  adapter + CLI + UI + template changes comparable in shape to a full harden-cleanup-cli command
  port; (b) two of five open questions (screenshot-location gating, default-roots source) are
  real judgment calls with behavior-surface consequences, not naming choices; (c) two genuinely
  new pieces of logic — leak-proof content-regex scanning, and a two-stage size/hash dedupe with
  a real correctness/performance tradeoff — that don't reduce to "copy the existing pattern" the
  way most of the original sort/reclaim port work did. Not Large: no cross-subsystem architecture
  to design, no multi-platform testing burden beyond one new adapter method, no external
  dependency or migration risk.
```

## 6. Dependencies

- **Story A** depends on nothing new — `adapter.list_dir` covers the filename scan; the content
  scan needs new logic (regex + file read) but no new adapter method, unless the Risk 3
  performance investigation concludes a `grep` shell-out is warranted (then it follows
  `dir_size_bytes`'s existing precedent).
- **Story B** depends on nothing new — `adapter.list_dir` + stdlib `hashlib` (already used by
  `queue.py`) covers everything once Open Question 2 is resolved.
- **Story C** depends on:
  - Nothing new for the move/plan/`--go`/`--from-queue`/queue-staging core — `queue.py` and
    `ui/routes.py`'s staging pattern already exist and need no changes to support a new caller.
  - One new `OSAdapter` abstract method (+ macOS impl + Arch stub) for the screenshot-location
    change — new surface, but shaped identically to `find_installed_app`, gated on Open
    Question 5.
  - Optionally `config.py`'s `search_roots`, if Open Question 4 resolves to reusing it.
  - If Risk 6 (master-path awareness for moves) is resolved as "yes, add it," extracting
    `reclaim.py`'s currently module-private `_master_path_refusal`/`_is_relative_to_ci`/
    `_resolve_loose`/`_same_path_ci` helpers into a shared location first — a small refactor,
    worth sequencing before Story C's move-planning logic is written.
- **No story depends on the AI-provider layer** — none of these three commands proposes
  ambiguous-file decisions the way `sort`'s "other" bucket does.

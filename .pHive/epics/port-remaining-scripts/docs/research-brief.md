# Research Brief: Port the remaining bash scripts (find-wallets, dedupe, corral-screenshots)

**Validation note:** codebase-only (fully read the three target scripts plus every Python module
they need to follow the conventions of). No context7/web research needed — nothing here touches a
third-party SDK; it's a straight bash→Python port against patterns this codebase already
established in `harden-cleanup-cli` and `ai-approvals-ui`. Confidence: **high**.

## Summary

Three scripts remain bash-only, confirmed by `README.md:26` ("`find-wallets`, `dedupe`, and
`corral-screenshots` remain bash-only for now, pending future epics") and
`cli.py`'s `COMMANDS` dict, which only wires up `survey`/`sort`/`reclaim`. Two of the three
(`find-wallets.sh`, `dedupe.sh`) are pure read-only reporting scripts with no destructive
behavior at all — they slot in next to `survey.py` as a fourth/fifth "computes a dict, prints
JSON" command. The third (`corral-screenshots.sh`) **moves files** and **changes a macOS system
preference** — it needs the full dry-run/`--go`/`--from-queue`/queue-staging/UI-route treatment
that `sort.py`/`reclaim.py` and `ui/routes.py` already established.

## What each script currently does

### `scripts/find-wallets.sh` (17 lines)

- `ROOT="${1:-$HOME}"` — defaults to the whole home directory, unlike `sort`/`reclaim` which
  default to Downloads/Documents+Desktop.
- **By filename**: `find "$ROOT" -type f` with 13 case-insensitive `-iname` patterns ORed
  together: `wallet.dat`, `*.wallet`, `keystore*`, `UTC--*`, `*.kdbx`, `electrum*`,
  `*mnemonic*`, `*seed*phrase*`, `*recovery*phrase*`, `*.keychain`, `default_wallet`,
  `metamask*`, `exodus*`, `atomic*wallet*`. Excludes `*/node_modules/*` and
  `*/Library/Caches/*`. Output capped at `head -200`.
- **By content** (paths only): `grep -rlIE` over `$ROOT/Documents`, `$ROOT/Desktop`,
  `$ROOT/Downloads` with the regex
  `(\b[a-z]+ ){11,23}[a-z]+\b|xprv[0-9A-Za-z]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"crypto"\s*:\s*\{[^}]*"cipher"`
  — i.e. a 12-24 space-separated lowercase word run (BIP39 seed phrase shape), a BIP32 extended
  private key prefix, a PEM private-key header, or an Ethereum keystore JSON's `"crypto": {
  ..."cipher"` signature. Restricted to `--include='*.txt' *.json *.md *.rtf *.csv'`,
  `--exclude-dir=node_modules --exclude-dir=.git`. `-l` (list filenames only, never match text)
  and `-I` (skip binary files) are load-bearing safety flags. Output capped at `head -100`.
- **Safety property, verified by close reading**: the script *never* reads a match back out —
  `grep -l` only ever emits filenames, never the matched line/content. There is no code path in
  this script that could print key material; the closing `echo` line makes this explicit
  ("this script never prints or sends key material"). This exact behavior (never resolve a
  content match to its matched substring, never `cat`/print file contents) is the one
  non-negotiable safety property the port must preserve byte-for-byte.
- No dry-run/`--go` concept applies — it never writes anything. Pure read.

### `scripts/dedupe.sh` (6 lines, dense)

- `DIR="${1:-$HOME/Downloads}"`.
- **Stage 1 — group by size** (cheap, no hashing): `find "$DIR" -type f ! -path
  '*/node_modules/*'` → for each file, `stat -f%z` (**BSD/macOS-only** `stat` flag syntax — GNU
  `stat` on Linux needs `-c%s`; this is exactly the kind of platform difference `OSAdapter`
  exists to hide) to get its byte size, emit `"<size> <path>"`. Sort numerically by size, then an
  `awk` pass that compares each line's size to the *previous* line's size and prints both when
  they match. Because it only ever compares to the immediately-prior line, a same-sized group of
  3+ files does still get every member printed (each adjacent pair triggers a print), but with
  duplicated lines for the middle elements — hence the following `sort -u`, which dedupes those
  down to a clean, size-matched candidate list.
- **Stage 2 — hash only the size-matched candidates**: for each surviving path, `shasum` (SHA-1,
  full file content, no size cap) truncated to its first 12 hex characters, emitted as `"<hash>
  <path>"`. Sort by hash, then the identical previous/adjacent-line `awk` trick prints pairs
  whose hash matches.
- **This two-stage "cheap filter (size) before expensive filter (hash)" shape is the one
  behavior most worth preserving** — hashing every file in a directory tree up front would be far
  slower than hashing only the (usually much smaller) subset that already shares an exact size.
- **Two real weaknesses worth flagging, not silently inheriting**:
  - `shasum`'s default is SHA-1, truncated to 12 hex characters (48 bits) here. That's a
    meaningfully higher collision risk than a full SHA-256 (which `queue.py`'s
    `_content_hash` already uses elsewhere in this codebase, see below) — worth reconsidering for
    the port even though this pass stays read-only/informational.
  - The hash is computed over the **entire** file with no size cap, unlike `queue.py`'s
    `_content_hash`, which deliberately caps at `CONTENT_HASH_MAX_BYTES` (8 MiB, prefix-only for
    anything larger) specifically because full-file hashing is "prohibitively slow ... for very
    large files" (`queue.py:275-284`). Dedupe's use case is different from staleness-checking,
    though: a prefix-hash "duplicate" claim on two large, same-size files that differ only past
    the first 8 MiB would be a **false positive** — two non-identical files reported as
    duplicates. That's an acceptable narrow gap for `queue.py`'s staleness check (per its own
    docstring) but a much worse one for a tool whose entire output is "these files are
    duplicates" — worth deciding deliberately rather than reusing `queue.py`'s cap unexamined.
- Output today is raw `"<hash>  <path>"` line pairs, not structured groups — the Python port
  should return a structured `{hash: [paths]}` (or list-of-groups) shape instead, matching every
  other command's dict-of-JSON-serializable-data convention.
- Purely read-only: no move, no delete, nothing written anywhere. Reclaiming space from the
  duplicates it finds is explicitly out of scope for this epic per the task brief.

### `scripts/corral-screenshots.sh` (8 lines)

- `mkdir -p ~/Pictures/Screenshots`.
- Counts matches first: `find ~/Desktop ~/Downloads ~/Documents -maxdepth 2 -iname
  'screenshot*'` (case-insensitive, depth-limited to 2, hardcoded three roots — no
  `node_modules`/`Library/Caches` exclusion, but `-maxdepth 2` makes that largely moot) piped to
  `wc -l` for a pre-move count `n`.
- Moves: the *same* `find` re-run with `-exec mv -n {} ~/Pictures/Screenshots/ \;`. `-n` is
  `mv`'s no-clobber flag — if a same-named file already exists at the destination, that one move
  is silently skipped, every other match still proceeds. **This is exactly the `dest_exists`
  skip-don't-overwrite behavior `sort.py`'s `_plan`/`run` already implement explicitly** (see
  `sort.py:44-51,197-202`) — a directly reusable precedent, not new design space.
  - **Bug worth flagging, not porting forward**: because `n` is counted *before* the move, and
    `mv -n` can silently skip destination-collision files during the move, the printed `"moved
    ~$n screenshots"` is only an upper bound, not the actual count — the script's own `~` prefix
    on `$n` is a tacit admission of this. `sort.py`'s plan (`entry["moved"]` per-file) already
    has the right shape to report an *exact* post-move count; the port should use it and drop the
    approximation.
- Prints `"moved ~$n screenshots -> ~/Pictures/Screenshots"`.
- **"Stop new ones hitting the Desktop" — read closely, this is exactly two shell commands, no
  LaunchAgent, no config file, no symlink**:
  1. `defaults write com.apple.screencapture location ~/Pictures/Screenshots` — writes directly
     to the macOS `com.apple.screencapture` preference domain (backed by a `.plist` under
     `~/Library/Preferences/`), which is the same mechanism System Settings' own "where screenshots
     save" control uses. This changes where macOS's *built-in* screen-capture (Cmd+Shift+3/4/5, and
     the `screencapture` CLI) saves future screenshots — it has zero effect on any third-party
     screenshot tool.
  2. `killall SystemUIServer` — restarts the process that renders the menu bar/notification
     center/etc. so the new preference takes effect immediately without a full logout. This is a
     genuine, visible system-level side effect (menu bar and Dock briefly disappear/relaunch) —
     categorically different from anything `sort`/`reclaim` do today, which only ever touch files.
  3. Prints a confirmation line.
- **This entire mechanism is macOS-only and has no Arch Linux equivalent** — screenshot save
  location on Linux is desktop-environment-specific (GNOME Screenshot, KDE Spectacle, a WM
  keybinding calling `grim`/`scrot`/etc.), with no single system-wide preference key. This is
  the exact shape `find_installed_app` already establishes a precedent for: an
  `OSAdapter`-abstract, macOS-only capability, with `ArchLinuxAdapter` raising
  `NotImplementedError` (see `reclaim.py`'s `_orphaned_installers_category`, which catches that
  `NotImplementedError` and reports `{"skipped": True, "reason": "not supported on this OS"}`
  rather than crashing — the exact fallback shape a ported corral-screenshots should reuse for
  this side effect on non-macOS).
- No config is read at all — roots and pattern are hardcoded. `config.py`'s
  `DEFAULT_BUCKET_RULES` already encodes the identical `"screenshot*"` filename-pattern
  convention for the `screenshots` bucket (`config.py:53-57`), and `survey.py`'s
  `_screenshot_count` (`survey.py:90-99`) already calls `adapter.list_dir(d, max_depth=2,
  pattern="screenshot*")` across Desktop/Downloads/Documents/Pictures — this is the closest
  existing precedent for enumerating screenshot files via the adapter, and the port should reuse
  it verbatim (minus Pictures, which is the destination, not a source, for corral-screenshots).

## Existing Python patterns each port should follow

- **Read-only, dict-returning commands** (`find-wallets`, `dedupe`): follow `survey.py`'s shape
  exactly — `run(adapter, args=None) -> dict`, args accepted-and-mostly-ignored, every "section"
  a private `_helper(adapter, ...)` function, no filesystem mutation anywhere in the module. No
  queue/UI integration needed for either (see design-discussion's open questions on this point).
- **Dry-run-by-default, `--go`-gated, `--from-queue`-capable command** (`corral-screenshots`):
  follow `sort.py`'s shape, which is the closer of the two structural precedents (`reclaim.py`
  additionally has the master-path-refusal machinery, which is delete-specific and — per the risk
  section below — may or may not apply here):
  - A `_plan(adapter, target_dirs) -> list[dict]` building `{src, dest, dest_exists}` entries,
    mirroring `sort.py:23-52`.
  - `run(adapter, args=None)` always computes the full plan; only calls `adapter.move()` per
    entry when `args.go` is true; each move isolated in its own `try/except OSError`
    (`sort.py:197-210`).
  - `args.from_queue` executes previously-*approved* `move` queue entries whose `src` falls under
    the resolved roots, via a `_run_from_queue` helper structurally identical to
    `sort.py:65-149` (load queue once inside `with_queue_lock`, staleness check via
    `queue.check_staleness`, `dest.exists()` skip-guard, broad `except Exception` per entry so
    one bad entry never blocks `save_queue`, save exactly once).
  - `cli.py` needs a new subparser (`dir`/`--go`/`--from-queue`, following the `sort_parser`
    block at `cli.py:50-75` almost verbatim) and a `COMMANDS["corral-screenshots"] =
    corral_screenshots.run` entry.
- **UI plan-staging route**: follow `ui/routes.py`'s `_stage_sort_plan`/`plan_sort` pair
  (`routes.py:194-256`) exactly — dry-run the command's `run()`, convert each plan entry into a
  `QueueEntry(action="move", ...)`, `stage_entries()` them, redirect to the dashboard with a
  `staged=` count. `dashboard.html:38` already has the "Plan: Sort / Plan: Reclaim" link pattern
  (`{{ url_for('ui.plan_sort') }}` / `{{ url_for('ui.plan_reclaim') }}`) to extend with a third
  "Plan: Corral Screenshots" link.
- **QueueEntry construction**: `queue.py`'s `QueueEntry` dataclass (`queue.py:33-49`) needs no
  schema changes — `action="move"` already fits corral-screenshots' semantics exactly the same
  way it fits sort's. `plan_snapshot` should be built via `queue.build_plan_snapshot(item["src"])`
  exactly as `_stage_sort_plan` does, so `check_staleness`/`--from-queue` work identically.
- **OS-adapter extension for the screenshot-location side effect**: needs one new
  `OSAdapter`-abstract method (e.g. `set_screenshot_save_location(path) -> None` or similar),
  implemented in `MacOSAdapter` (the two-command sequence above) and either raising
  `NotImplementedError` in `ArchLinuxAdapter` or implemented per-DE if that's judged worth the
  scope (see design-discussion's open questions) — following `find_installed_app`'s existing
  abstract-method-with-one-real-implementation precedent (`adapters/base.py:254-264`,
  `adapters/macos.py:27-51`).

## Risks / gaps identified

1. **Wallet-detection false negatives from an imprecise port.** The 13 filename `-iname`
   patterns and the 4-alternative content regex are exact strings/regex fragments with real
   semantic weight (e.g. `xprv[0-9A-Za-z]{20,}` is specifically a BIP32 extended-key prefix, not
   a generic "looks like base58" heuristic). A port that "cleans up" or "simplifies" these during
   translation to Python regex/`fnmatch` risks silently narrowing (or widening) what's detected.
   These need to be preserved as literal, tested constants, not reconstructed from memory.
2. **Wallet-detection safety property is currently structural, not just a comment.** The bash
   script is safe because `grep -l` *cannot* emit matched content even if someone tried — there's
   no code path to a leak. A Python port that reads file content into a string to run
   `re.search` (rather than `re.search` + immediately discarding the match without ever storing
   `match.group()` in the returned dict) must preserve that same structural guarantee, not just
   avoid printing the match "by convention." The returned dict shape needs to make it
   *impossible* to accidentally include matched text, not just currently not include it.
3. **Content-search performance.** `grep -rlIE` is a highly optimized C tool; a naive Python
   walk + `open().read()` + `re.search()` over every `.txt`/`.json`/`.md`/`.rtf`/`.csv` file
   under Documents/Desktop/Downloads could be materially slower, especially on a machine with the
   "857 Downloads / 473+ screenshots"-scale clutter this project's `north_star` describes. Worth
   deciding explicitly whether to shell out to `grep` (mirroring `dir_size_bytes`'s existing
   precedent of shelling out to `du` specifically because a Python walk is too slow,
   `adapters/base.py:213-224`) or reimplement in pure Python with size/extension pre-filtering.
4. **Dedupe hash strength/cap tradeoff** (detailed above) — SHA-1-truncated-to-48-bits with no
   size cap (bash) vs. SHA-256-with-8MiB-prefix-cap (`queue.py`'s existing pattern) are two
   different, both-imperfect tradeoffs; neither should be adopted without a deliberate choice,
   since dedupe's whole output is a duplicate-ness claim, not an auxiliary staleness check.
5. **`stat -f%z` is macOS-only bash — not a Python-specific risk**, but confirms `dedupe.sh` was
   never actually run on the project's other real machine (Arch Linux, per
   `project-profile.yaml`'s `tech_stack.planned` note) — `Path.stat().st_size` in Python sidesteps
   this entirely and is one of the more mechanical parts of the port.
6. **`corral-screenshots`' master-path interaction is currently undefined, because `sort.py` has
   no master-path check at all today.** `reclaim.py`'s master-path refusal logic
   (`_master_path_refusal`, `reclaim.py:141-173`) is scoped to *deletion* and is never consulted
   by `sort.py`'s move logic. A screenshot living inside a configured (and not-yet-backed-up)
   master path (e.g. `~/Documents/tax/screenshot-of-receipt.png` if `~/Documents/tax` were
   configured as a master path) would, if `corral-screenshots` is built as a pure `sort.py`
   clone, be moved with zero master-path awareness — silently reorganizing a directory the user
   has flagged as not-yet-backed-up. This gap already exists for `sort` today but is worth
   surfacing explicitly here since it's a *new* command being designed now, not an existing one
   being left alone.
7. **The `killall SystemUIServer` side effect is more invasive than anything `sort`/`reclaim`
   do today.** Every existing destructive action in this codebase is scoped to file
   moves/deletes; restarting a running system process (however routine on macOS) is a new
   category of side effect for this tool and arguably deserves its own explicit opt-in rather
   than being folded silently into `--go`.
8. **`_master_path_refusal`, `_is_relative_to_ci`, `_resolve_loose`, `_same_path_ci`
   currently live as module-private helpers inside `reclaim.py`**, not in a shared location. If
   corral-screenshots' design ends up wanting master-path awareness (see risk 6), those helpers
   would need extracting to a shared module (e.g. `config.py` or a new `paths.py`) rather than
   being duplicated — a genuine small refactor, not a copy-paste.

## Dependencies already available

`pyproject.toml` declares `PyYAML`, `Flask`, `Pillow`, `anthropic` as runtime deps and `pytest`
as dev. Everything this epic needs — `hashlib` (stdlib, already used by `queue.py`), `re`
(stdlib, already used by `reclaim.py`), `subprocess` (stdlib, already used by `reclaim.py` for
`docker` and `adapters/base.py` for `du`) — is either already a dependency or stdlib. No new
third-party packages are anticipated.

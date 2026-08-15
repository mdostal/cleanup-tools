# Project CONTEXT

cleanup-tools: bash scripts (soon a packaged local CLI/desktop app) for sorting, reclaiming space
on, and finding files across a cluttered Mac — including recovering a lost crypto wallet.

## Terminology

- **Survey** — the read-only snapshot step (`scripts/survey.sh`): disk usage, top-level dir sizes,
  node_modules total, Downloads type histogram, screenshot count. Always the first step; never writes.
- **Sort / bucket** — staging files by type into `_sorted/<type>/` (screenshots, installers, pdfs,
  photos, videos, archives, data, docs, other) via `scripts/sort-downloads.sh`. Non-destructive (moves
  within the same folder), never deletes.
- **Reclaim** — deleting *regenerable* junk (node_modules, build caches, Docker layers, OS junk like
  `.DS_Store`/`~$*`/`$RECYCLE.BIN`) via `scripts/safe-reclaim.sh`. Dry-run by default; needs `--go`.
- **Masters** — irreplaceable files (SMS backup, family footage, tax docs, patent app, the
  "desktop back" dump) that must be backed up externally before anything touching them is deleted.
  This is now actually enforced, not just a convention: `cleanup reclaim` (see
  `src/cleanup_tools/commands/reclaim.py`) reads `master_paths` from
  `~/.config/cleanup-tools/config.yaml` and unconditionally refuses to delete any candidate that is,
  contains, or is contained by a master path not marked `backed_up: true` — even under `--go`. Once a
  master path's `backed_up` field is flipped to `true`, it gets no special protection and is treated
  as a normal deletable candidate.
- **`_REVIEW`** — the staging folder for anything uncertain. Nothing goes straight to trash; unclear
  files land here for a human decision.
- **Dry-run / `--go`** — the safety convention for every destructive script: default run only prints
  what *would* change; the literal `--go` flag is required to actually act.
- **Wallet finder** — `scripts/find-wallets.sh`, ported as `cleanup find-wallets [root]`
  (`src/cleanup_tools/commands/find_wallets.py`): scans for crypto-wallet artifacts by filename (14
  patterns: `wallet.dat`, `keystore*`, `UTC--*`, `.kdbx`, seed/mnemonic files, and more) and content
  signatures (BIP39 phrases, `xprv`, PEM private keys, Ethereum keystore JSON). **Paths-only** —
  never prints, logs, or transmits the matched secret/key material; in the Python port this is
  structural (the matched-content object is never bound to a name that outlives its truthiness
  check), not just a convention.
- **Dedupe** — `scripts/dedupe.sh`, ported as `cleanup dedupe [dir]`
  (`src/cleanup_tools/commands/dedupe.py`): lists duplicate files via a two-stage size-then-hash
  filter so the user can decide what to remove; never auto-deletes, never touches the approval
  queue. The Python port deliberately hashes with full, uncapped SHA-256 (not bash's SHA-1
  truncated to 48 bits, and not `queue.py`'s separate 8-MiB-capped hash helper) since dedupe's
  entire output is a duplicate-ness identity claim, not an auxiliary staleness check.
- **Keep-clean** — the not-yet-built recurring triage loop (see `docs/REQUIREMENTS.md`) that re-runs
  survey, flags when Downloads/Desktop cross a clutter threshold, and offers a one-tap sort.

## Key paths

- `scripts/` — the six reference bash tools (survey, sort-downloads, corral-screenshots,
  safe-reclaim, find-wallets, dedupe). Currently the only code in the repo.
- `docs/CLEANUP-PLAN.md` — the grounded, numbers-based survey of the author's actual mess and the
  phased plan for working through it by hand.
- `docs/REQUIREMENTS.md` — the build-out spec: hardening the reference scripts into a packaged,
  testable CLI (Node/TS or Python) with an optional local desktop UI later, plus the hard safety rules.
- `.pHive/project-profile.yaml` — Hive's discovery profile for this project (tech stack, north star,
  integrations, code quality signals).
- `~/.config/cleanup-tools/config.yaml` — optional persisted config (loaded/created by
  `src/cleanup_tools/config.py`) controlling bucket rules, search roots, and master paths; sensible
  defaults apply if absent.
- `~/.config/cleanup-tools/approval_queue.yaml` — the approval queue store (`src/cleanup_tools/queue.py`):
  proposed move/delete actions awaiting human approval, with file locking and atomic writes for safe
  concurrent access. Kept as a separate file from `config.yaml` (not a section within it) so the UI
  and CLI commands can read/write queue entries concurrently without contending over the same file
  lock as unrelated config changes. `cleanup sort --from-queue` / `cleanup reclaim --from-queue` /
  `cleanup corral-screenshots --from-queue` execute approved entries; `cleanup approve`
  (`src/cleanup_tools/ui/`) is the localhost-only Flask UI that stages (`/plan/sort`, `/plan/reclaim`,
  `/plan/corral-screenshots`) and reviews (approve/reject/undo) entries — it never executes,
  execution is always a separate deliberate CLI step. `corral-screenshots` also introduces
  `OSAdapter.set_screenshot_save_location` (macOS-only, `NotImplementedError` on Arch), the most
  invasive OS-level side effect in this codebase (restarts `SystemUIServer`) — gated behind its own
  `--set-default-location` CLI flag, structurally independent of `--go`/`--from-queue` and
  unreachable from the UI.
- `src/cleanup_tools/ui/jobs.py` — the in-memory background-job registry backing all three
  `/plan/*` routes (sort, reclaim, corral-screenshots — each proposed entry gets a real content
  hash, which is slow at real scale) and `GET /status/<job_id>`. Unlike `approval_queue.yaml`, job
  state does **not** persist anywhere — it's a plain `dict[job_id -> JobState]` guarded by a single
  lock, lost entirely on process restart. A job only needs to survive one open browser tab/
  desktop-app window; there is nothing to recover if the server restarts mid-job.
  `static/plan-trigger.js` is the shared client-side counterpart that polls it, used both by the
  individual "Plan: X" nav links and the dashboard's kickoff bar (select multiple plans, launch them
  all as independent parallel jobs with one click).
- `packaging/pyinstaller/` — the PyInstaller spec (`cleanup_ui.spec`) + entrypoint
  (`entrypoint.py`) that freeze `src/cleanup_tools/ui/` into a standalone sidecar binary, later
  wired into a Tauri desktop shell's `externalBin`. Ships `--onedir` (a directory, not a single
  file) — a real measured finding, not a style preference: killing a `--onefile` build with
  `SIGKILL` orphans the real Flask process behind PyInstaller's bootloader (confirmed via
  `lsof`/`netstat` on the actual built binaries), while `--onedir`'s reported PID always is the
  real process. `tests/test_pyinstaller_spec_datas.py` fails loudly if the spec's `datas=` list
  ever drifts out of sync with the real `templates/`/`static/` directories — this already caught
  a real bug once (a new static file landing in a concurrent story, missed by the spec).
- `src-tauri/` — the macOS Tauri v2 desktop shell (first Rust code in this project). Wraps the
  onedir sidecar via a shell-script stub (`src-tauri/binaries/cleanup-ui-sidecar-stub.sh`) that
  `exec`s the real onedir executable, since Tauri's `externalBin` only accepts one file per target
  triple and onedir is a directory — the stub preserves onedir's whole point (the tracked PID is
  always the real Flask process). `frontend/loading.html` is the bundled loading/poll/error screen
  the webview shows before/if the sidecar isn't ready. Unsigned distribution: a quarantined copy
  shows "app is damaged" on current macOS, not a right-click-Open dialog — see README.md's Tauri
  section for the actual working install step (`xattr -d com.apple.quarantine`).
- `packaging/arch/PKGBUILD` — local-only, build-from-source Arch Linux PKGBUILD for the Tauri
  desktop shell, run via `makepkg -si` directly from a local clone (never published to the public
  AUR). Reuses `src-tauri/`'s process-lifecycle Rust code unchanged; this is a packaging-config-only
  addition. **Structurally reviewed only, never build-tested** — nobody working on this project has
  Arch Linux hardware. `bash -n` passes on its `build()`/`package()` functions and its fields were
  checked against the Arch Wiki's PKGBUILD conventions, but `makepkg -si` itself has never been run.
  `src-tauri/binaries/cleanup-ui-sidecar-stub-linux.sh` (copied to the required per-target-triple
  filename `cleanup-ui-sidecar-x86_64-unknown-linux-gnu`) closes the previously-open gap where the
  sidecar stub script only handled macOS `.app`-bundle paths — it's a Linux/`.deb`-bundle-layout
  counterpart of `cleanup-ui-sidecar-stub.sh` (untouched), derived from reading `tauri-bundler`'s
  Debian-bundler source directly (also unverified on real hardware — the stub tries a defensive
  fallback path for exactly that reason). One prerequisite gap remains and cannot be closed without
  real Arch/Linux hardware: no Linux-target PyInstaller onedir sidecar *binary* exists yet (only the
  stub *script* that would locate and exec it) — see README.md's Arch Linux section for the exact
  manual build/verification steps still needed from the project owner.
- **Icon picker** (`GET /settings`, `POST /settings/icon`, `src-tauri/src/lib.rs`'s
  `apply_icon_choice` command) — lets the user swap the app icon at runtime among four bundled
  concepts (`broom-folder` default, `broom-sparkle`, `tidy-folder-check`, `recycle-folder`; full
  icon sets under `src-tauri/icons/alternates/<slug>/`, shipped via `bundle.resources`, thumbnails
  at `src/cleanup_tools/ui/static/icon-choices/*.png`). Persisted in `config.yaml`'s `icon_choice`
  field (`src/cleanup_tools/config.py`) rather than the theme picker's browser-only `localStorage`,
  since the Rust side also needs to read it — same cross-language "must match by hand" list
  (`ICON_CHOICES`) duplicated in both `routes.py` and `lib.rs`, mirroring `SIDECAR_PORT`'s existing
  convention. **Deliberately platform-asymmetric, by explicit decision, not an oversight:** macOS
  gets the full treatment — the *installed* `.app` bundle's Finder icon is actually rewritten (plain
  copy first, falling back to an admin-prompted `osascript`/`do shell script` if the install
  location isn't user-writable). Windows/Linux only get the live taskbar/window icon via Tauri's
  cross-platform `set_icon` — no Start-Menu-shortcut or `.desktop` `Icon=` rewrite, for the same
  "no test hardware" reason the Arch PKGBUILD above is flagged reviewed-never-built. If that ever
  changes, this is the natural next extension point.
- **Update checker** (`tauri-plugin-updater` + `tauri-plugin-process`, `src-tauri/src/lib.rs`'s
  `check_for_update`/`get_pending_update`/`download_and_install_update` commands,
  `static/update-checker.js`) — checks `plugins.updater.endpoints` in `tauri.conf.json` (the
  stable `releases/latest/download/latest.json` GitHub alias, so this URL never needs to change
  per release) on launch and every 6 hours, and ONLY ever shows a dismissible in-app banner; a
  download/install never happens without an explicit "Update now" click. This is the first real
  use of the corrected network-policy rule above (custom `#[tauri::command]`s wrapping the plugin's
  Rust API, not the `@tauri-apps/plugin-updater` JS package — this project's frontend has no JS
  bundler, same reasoning as the icon picker's `apply_icon_choice`). The signing private key lives
  in Portunus (`cleanup-tools-updater-signing-key`), never on disk in this repo; the public key is
  the `pubkey` baked into `tauri.conf.json`. Cutting a signed release needs `tauri build --ci`
  (plain `tauri build` tries to interactively prompt for the signing key's password even when
  `TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""` is set, and hangs forever with no TTY attached — `--ci`
  is what actually suppresses that prompt; confirmed the hard way during v0.1.1's own release) —
  see README.md's "Cutting a signed release" section and `packaging/tauri/make-latest-json.sh` for
  the full sequence.

## Conventions

- Every destructive script defaults to dry-run; `--go` is the only way to make it act. Any new tool
  must follow this pattern (see `scripts/safe-reclaim.sh` for the reference implementation).
- Uncertain files are staged into `_sorted/<type>/` or `_REVIEW/` — never deleted blind.
- The wallet/secret finder outputs `path · match-type · confidence` — never the matched value itself.
- **No ambient/telemetry network calls, and nothing excessive.** This tool touches personal files
  end-to-end, so anything that phones home *silently* or *by default* is out — no analytics, no
  crash-reporting plugin, no background tracking. This is NOT a blanket ban on network code, though
  (an earlier phrasing of this rule as "no network calls, period" overstated the actual policy —
  corrected 2026-08-14, direct from the project owner): a legitimate, user-facing feature is fine as
  long as it's opt-in or visibly-triggered rather than silent, and doesn't poll aggressively or move
  more data than the feature actually needs. `.pHive/project-profile.yaml → north_star.avoid` has
  the same correction applied.
- No CLAUDE.md exists yet — until one is added, `.pHive/project-profile.yaml → claude_md_summary`
  is the authoritative source for build/rule conventions.
- Explicit, user-triggered AI calls
  via `src/cleanup_tools/ai/` (`AIProvider` ABC + `AnthropicProvider`). The SDK itself must never
  phone home on its own (verified against the installed SDK's source, not assumed) — client
  construction always passes `api_key=` explicitly so the SDK can't silently fall back to an
  OAuth-profile/Workload-Identity-Federation credential we don't control. API keys live in
  `ANTHROPIC_API_KEY` or `~/.config/cleanup-tools/credentials` (0600-enforced) — never in
  `config.yaml` or `approval_queue.yaml`. Wired in via `src/cleanup_tools/ai/wiring.py`'s
  `propose_for_other_bucket()`: reads the sort plan's `other`-bucketed files, enforces the call
  cap as a pre-call gate (slices candidates before calling the provider, never filters results
  after), and stages successes into the same queue manual staging uses (`source="ai:<provider>"`)
  — so AI-sourced entries need zero special-casing anywhere downstream. Reachable via the
  `POST /propose-ai` UI route or `cleanup propose-ai [--cap N]`.

## Canonical references

- `README.md` — script table, workflow order, wallet-finding overview.
- `docs/CLEANUP-PLAN.md` — the phased cleanup plan this repo was built from.
- `docs/REQUIREMENTS.md` — the full build-out spec, hard rules, and wallet-finder signatures.
- `.pHive/project-profile.yaml` — Hive's structured discovery profile (north star, integrations,
  code quality, ship target).

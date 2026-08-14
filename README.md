# cleanup-tools

<!-- shared:tagline -->
> Reclaim disk on macOS with dry-run-first cleanup scripts. Free & open source.
<!-- /shared:tagline -->
<!-- shared:byline -->
Built by [Mathew Dostal](https://mdostal.com) — fractional CTO, Dostal Technology.
<!-- /shared:byline -->

Scripts + a plan for going through a cluttered Mac — Documents, Desktop, Downloads, and the sprawl of
screenshots — and getting it **sorted, arranged, and kept clean**. Built from a real survey (see
`docs/CLEANUP-PLAN.md`). A side benefit of the file-finder: it also **locates a lost crypto wallet**.

## Principles
- **Sort is reversible; delete is not.** Destructive scripts default to **dry-run**; you pass `--go` to act.
- **Back up masters before deleting.** Never lose the irreplaceable (SMS/family/tax/patent).
- **Regenerable ≠ precious** — `node_modules`, build caches, Docker layers delete freely.
- Uncertain files get **staged into buckets/_REVIEW**, never trashed blind.

## Status
A packaged Python CLI is being built in `src/cleanup_tools/`, following the build-out plan in
`docs/REQUIREMENTS.md`; `cleanup survey` (installed via `pip install -e .`, run as `cleanup survey`
or `python3 -m cleanup_tools.cli survey`) is now a real, working ported command, on top of the
cross-platform OS-adapter foundation (macOS + Arch Linux). `cleanup sort` is also now a real,
working ported command (see the callout below for a behavior change vs. the old script). `cleanup
reclaim` is now a real, working ported command too, from `scripts/safe-reclaim.sh`, with several
behaviors new vs. the old script: master paths configured in `~/.config/cleanup-tools/config.yaml`
are now **refused from deletion until marked `backed_up: true`** — the "never delete without a
backup of masters" hard rule is now actually enforced in code, not just a convention to remember;
orphaned-installer detection (`.dmg`/`.pkg` files whose app is already installed) is new, macOS-only
for now; and GB-reclaimed reporting is new. See the callout below for a Docker-pruning gate change.
`cleanup survey`, `cleanup sort`, and `cleanup reclaim` are now all real, working commands — the
packaged CLI's first full round. `cleanup find-wallets [root]` is now also a real, working ported
command from `scripts/find-wallets.sh`: same 14 filename patterns and 4 content-signature
alternatives, byte-for-byte, and the same read-only safety property (paths + which pattern
matched only — matched secret content never appears anywhere in its output, by construction, not
just by convention). `cleanup dedupe [dir]` is now also a real, working ported command from
`scripts/dedupe.sh`: same two-stage size-then-hash shape, but with two deliberate corrections —
full, uncapped SHA-256 instead of bash's SHA-1 truncated to 48 bits, and structured
`{hash, size_bytes, paths}` groups instead of raw line pairs (see the callout below).
`cleanup corral-screenshots [dir...] [--go] [--from-queue] [--set-default-location]` is now also
a real, working ported command from `scripts/corral-screenshots.sh` — **this closes out the
port-remaining-scripts epic: every bash script now has a real Python CLI equivalent.** It follows
`sort`'s dry-run/`--go`/`--from-queue` pattern (not `reclaim`'s master-paths refusal, which is
delete-specific and doesn't apply to file moves — this is a pre-existing gap `sort` also has, not
new here), stages its moves into the approvals UI just like `sort`/`reclaim` do (a third
"Plan: Corral Screenshots" link), and reports an exact moved-count instead of bash's pre-move
approximation. The old script's screenshot-save-location system-preference change (which restarts
a live macOS process) is preserved but never runs implicitly — see the callout below. The CLI now
reads (and creates, if absent) an optional
config file at `~/.config/cleanup-tools/config.yaml` for bucket rules, search roots, and master
paths — sensible defaults apply if the file isn't there.

An approval queue store (`src/cleanup_tools/queue.py`) now also exists, persisting proposed
move/delete actions as YAML at `~/.config/cleanup-tools/approval_queue.yaml`, with file locking and
atomic writes so it's safe to touch from multiple processes at once. It's a separate file from
`config.yaml` rather than a section within it, so a future UI process and CLI commands can read/write
queue entries concurrently without contending over the same lock as unrelated config changes.

`cleanup sort` and `cleanup reclaim` both now also support a `--from-queue` flag, driven by that
approval queue store. This is a **second execution path alongside `--go`**, not a replacement for
it — `--go` still drives the existing "preview vs. act on this run" flow. `--from-queue` instead
executes actions that were previously staged into the approval queue and approved there.
Consequently `--from-queue` executes **unconditionally and does not need `--go` alongside it** —
approval status recorded in the queue is itself the safety gate for that path. The master-paths
refusal described above (configured masters refused from deletion until marked `backed_up: true`)
applies to `cleanup reclaim --from-queue` exactly as it does to `cleanup reclaim --go` — queue
approval does not bypass it.

`cleanup approve` is now a real command: it starts a small localhost-only (`127.0.0.1`, never
reachable from another machine — hardcoded, not configurable) Flask review UI and opens your
browser to it. The dashboard gives a DiskDrill-style overview of the approval queue (counts and
total size per bucket/category, plus a per-status breakdown), `/plan/sort` and `/plan/reclaim`
stage new proposed moves/deletes into the queue (idempotent — re-hitting either doesn't create
duplicate pending entries), and each pending entry gets a review card (thumbnail for images,
never the original full-resolution file) with approve/reject/undo. Approving or rejecting here
only changes the entry's status — it does **not** execute anything; run `cleanup sort --from-queue`
or `cleanup reclaim --from-queue` afterward, as a separate deliberate step, to actually act on
approved entries. None of the three "Plan: X" triggers (Sort, Reclaim, Corral Screenshots) block
the UI silently while they scan — each kicks off its work on a background job and reports live
progress while polling, then lands back on the dashboard with the same result a synchronous version
would have shown. The dashboard also has a **kickoff bar**: check which of Sort/Reclaim/Corral
Screenshots you want to run, hit one button, and all selected plans launch as independent background
jobs at once, each with its own progress, instead of clicking and waiting for each one in turn. The
manual (no-AI) approvals UI is now **complete and usable at real scale**:
`/queue` paginates (`?page=`/`?per_page=`), bulk-approve/bulk-reject act on an entire group_key
(or an explicit id list) in one click, and keyboard shortcuts (`y`/`n`/`space`/arrow keys) let you
triage a large plan fast without reaching for the mouse each time. The dashboard also shows a
small canvas-drawn chart of storage used per group, built from the same data the group cards
render from.

The UI now supports **three runtime-switchable visual themes** — Ledger (warm/light, serif
headings), Sonar (dark, monospace-forward), and Tide (cool/light, rounded) — chosen from a
"Theme" control in the nav on every page. The choice is saved in the browser's `localStorage`
(no server-side state, no account), so it persists across page navigations and browser restarts,
and is applied before first paint (no flash of the wrong theme on load).

An AI-provider layer (`src/cleanup_tools/ai/`) now exists — an Anthropic implementation of a
narrow "given a filename, propose a bucket" interface. Set `ANTHROPIC_API_KEY` in your
environment, or put the key in `~/.config/cleanup-tools/credentials` (created with `0600`
permissions — the tool will correct the mode and warn if it finds that file less restrictive).

**The epic is now complete end-to-end**: survey/sort/reclaim CLI, the approvals UI, and
AI-assisted proposals all connect. `cleanup approve`'s "Propose with AI" button (or
`cleanup propose-ai [dir] [--cap N]` from the CLI) reads the current sort plan's `other`-bucketed
(ambiguous) files, asks the AI provider what bucket each belongs in, and stages successful
proposals into the same approval queue manual staging uses — they show up in `/queue`
identically to manually-staged entries (same approve/reject/undo/bulk/keyboard flow), tagged
with an "AI-proposed" badge so you can tell them apart at a glance. The AI call-volume cap
(default 20) is enforced **before** any calls are made, not by filtering results afterward — a
misbehaving/fast provider genuinely cannot be called more than the cap allows. AI never talks
to the network unless you explicitly click "Propose with AI" or run `propose-ai` — this is the
one sanctioned exception to the "no ambient network calls" rule, and it stays that way: nothing
else in this tool makes a network call.

**Desktop app packaging is in progress** (`packaging/pyinstaller/`): the Flask UI now freezes into
a standalone binary via PyInstaller — no Python install required on the end user's machine — as
the first step toward a real double-clickable app (Tauri, macOS + Arch Linux) instead of
`cleanup approve` typed into a terminal. Build it with
`pip install -e '.[build]' && pyinstaller packaging/pyinstaller/cleanup_ui.spec`; smoke-test the
result with `python3 scripts/smoke_test_sidecar.py <path-to-binary>`. Ships as a directory
(`--onedir`, the default), not a single file — a single-file build was measured to leave an
orphaned server process holding the port if the OS ever has to force-kill it (`SIGKILL`), since
that signal can't reach the real process behind PyInstaller's single-file bootloader; the directory
build doesn't have this problem and is worth the extra size (~67MB vs. ~31MB).

**Building/running the macOS Tauri shell** (`src-tauri/`): `npm install`, then `npm run tauri dev`
for a live dev build or `npm run tauri:build` for a real `.app`/`.dmg` (both land under
`src-tauri/target/{debug,release}/bundle/`). `npm run tauri:build` runs `tauri build` and then
`packaging/tauri/post-build.sh`, which ad-hoc-signs the resulting `.app` (`codesign --force --deep
-s -`) — this makes `codesign --verify` pass and is worth doing for free, but **it does not make
Gatekeeper happy**; see the warning below before handing a built copy to anyone (including
yourself, on a second Mac).

> ⚠ **This build is unsigned. A downloaded/AirDropped/copied `.app` or `.dmg` will show "'Cleanup
> Tools' is damaged and can't be opened. You should move it to the Trash" — there is no
> right-click → Open bypass offered.** Older Gatekeeper docs (including this project's own earlier
> design notes) describe an "unidentified developer" dialog with a right-click-Open escape hatch;
> that is NOT what happens on this build/macOS version. It was tested directly: ad-hoc codesigning
> the app (above) produces a technically valid signature but does **not** clear the "damaged"
> dialog for a quarantined copy — only removing the quarantine flag does. The real, working fix for
> a personal install:
> ```
> xattr -d com.apple.quarantine "Cleanup Tools.app"   # or the .dmg you downloaded
> ```
> Run this **before the first time you open the app/dmg** — it must be the first launch attempt.
> If you already double-clicked a quarantined copy once and hit the "damaged" dialog (or it just
> sat there doing nothing), removing the attribute from that same copy afterward does not reliably
> un-stick it: macOS appears to cache a bad Gatekeeper verdict per file location once a quarantined
> launch has been attempted. Delete that copy, get/copy a fresh one, and run `xattr -d
> com.apple.quarantine` on the fresh copy *before* opening it.
>
> **Known side effect of a quarantined open attempt: an orphaned headless process.** If you do
> open a quarantined, unsigned copy (hitting the "damaged" dialog), macOS also launches a
> `cleanup-desktop` process in the background that never finishes starting — `sample <pid> 1`
> shows it permanently parked at `_dyld_start`, i.e. it never reaches this app's Rust `main()` at
> all. This is confirmed to be a macOS/Gatekeeper-level block (dyld/syspolicyd refusing to let a
> quarantined, non-Developer-ID-signed binary finish loading), not something this app's own code
> can detect or clean up — there is no app code running yet for it to run in. The process doesn't
> exit on its own; force-quit it (Activity Monitor, or `kill -9 <pid>`) or restart. Removing
> quarantine *before* first launch (per above) avoids ever triggering this in the first place.

**Arch Linux packaging** (`packaging/arch/PKGBUILD`): a local-only, build-from-source PKGBUILD —
run `makepkg -si` directly from your own clone of this repo to build and install the Tauri desktop
shell. It is **not published to the public AUR** (`aur.archlinux.org`) — no AUR-maintainer
obligations (SSH-key auth, `.SRCINFO` generation, ongoing update responsibility) are taken on; this
is a deliberate, permanent decision (see `.pHive/epics/desktop-app-shell/docs/design-discussion.md`
§e/§f), not a "not yet published" placeholder. `makedepends` covers `cargo`, `nodejs`, `npm`;
`depends` covers `webkit2gtk-4.1`, `gtk3`, `cairo`, and the rest of Tauri's own documented
WebKitGTK runtime dependency list; `options=('!strip')` protects the bundled PyInstaller sidecar
binary from being stripped in a way that breaks it.

> ⚠ **This PKGBUILD has only been structurally reviewed, never build-tested.** Nobody working on
> this project has access to a real Arch Linux machine. Everything in the PKGBUILD has been checked
> by reading it (field-by-field against the Arch Wiki's PKGBUILD conventions) and by running
> `bash -n` against its `build()`/`package()` shell functions (a real syntax check, but not a real
> build) — `makepkg -si` itself has **never been run**.
>
> **The sidecar stub-script portability gap is now closed** (was previously listed here as an open
> item): `src-tauri/binaries/cleanup-ui-sidecar-stub-linux.sh` is a Linux counterpart of the macOS
> `cleanup-ui-sidecar-stub.sh`, adapted to Tauri's Debian bundler layout (main binary + sidecars in
> `/usr/bin/`, `bundle.resources` under `/usr/lib/<productName>/` — read directly from
> `tauri-bundler`'s Debian-bundler source, with a defensive fallback candidate since that reading is
> itself unverified on real hardware), copied byte-for-byte to
> `src-tauri/binaries/cleanup-ui-sidecar-x86_64-unknown-linux-gnu` (the exact per-target-triple
> filename Tauri's `externalBin` resolution requires on Linux x86_64). This is still
> **structurally reviewed only, never build-tested** — same caveat as the rest of this section — but
> the macOS-only-hardcoded-paths gap that would have made the stub itself wrong on Linux no longer
> exists. **Note:** like everything else this PKGBUILD's `git+file://` source pulls in, these new
> files need to be `git commit`-ted (or at least staged) before `makepkg -si` will see them — see the
> PKGBUILD's own `source=` caveat comment.
>
> One thing is still known, not just suspected, to need attention before `makepkg -si` can actually
> succeed:
> 1. **No Linux-target PyInstaller sidecar *binary* exists in this repo yet** (only the stub *script*
>    above, which locates and `exec`s that binary — not the binary itself). `src-tauri/resources/`
>    currently only has the macOS onedir build. You'll need to build a Linux onedir sidecar binary
>    on your own Arch box (`ONEFILE=0 pyinstaller packaging/pyinstaller/cleanup_ui.spec`, per
>    `packaging/pyinstaller/cleanup_ui.spec`'s onedir-vs-onefile rationale) and place the resulting
>    output at `src-tauri/resources/cleanup-ui-onedir/` (mirroring the existing macOS layout) before
>    `tauri build` can produce a working sidecar on Linux. `packaging/arch/PKGBUILD`'s `build()` now
>    checks for this file explicitly and fails with this same explanation if it's missing, rather
>    than failing deeper inside `tauri build` with a more generic error.
> 2. `npm run tauri:build`'s `packaging/tauri/post-build.sh` step is macOS-only (ad-hoc codesign of
>    a `.app` bundle) — the PKGBUILD deliberately calls `npx tauri build -b deb` directly instead,
>    not `npm run tauri:build`, to avoid that script hard-failing on Linux.
>
> **What you need to do**: after building the Linux sidecar binary in (1) above and committing it
> (and the new stub files) into the repo, run `makepkg -si` from `packaging/arch/PKGBUILD` on your
> real Arch machine, then report back:
> - Did `makepkg -si` complete without error?
> - Did the package install cleanly via `pacman`?
> - Does the installed app launch, spawn its sidecar, pass the healthz check, and reach the real
>   Flask UI (the same end-to-end behavior already proven on macOS)? If the sidecar fails to start,
>   check whether `cleanup-ui-sidecar-stub-linux.sh`'s resource-path guess was wrong (its own error
>   message tells you how to check with `dpkg -L`) — that's the one part of this closed gap that
>   couldn't be confirmed without a real install.

> ⚠ **Flag polarity flip: `cleanup sort` defaults to dry-run — the opposite of `sort-downloads.sh`.**
> `sort-downloads.sh` **acted by default** and needed `--dry` to preview. The new `cleanup sort`
> **previews by default** and needs `--go` to actually move files. If you're used to running the bash
> script, running `cleanup sort` with no flags will **not** move anything — pass `--go` to act. This
> matches the CLI's overall dry-run-by-default convention (see Principles above), but it is a real
> reversal from the old script's convention, so don't assume old muscle memory carries over.

> ⚠ **`cleanup dedupe` hashes differently than `dedupe.sh` — a deliberate correctness fix, not
> silent parity.** The old bash script hashes with `shasum`'s default (SHA-1) truncated to 12 hex
> characters (48 bits) — a real collision risk for a tool whose entire output is "these files are
> the same, safe to remove one." `cleanup dedupe` hashes the full file content with uncapped
> SHA-256 instead. This is slower on large duplicate-heavy folders (the two-stage size-then-hash
> filter still bounds what gets hashed at all), but correctness matters more here than for
> `queue.py`'s separate, narrower staleness-check use of a capped hash.

> ⚠ **`cleanup corral-screenshots` never changes your screenshot save location unless you pass
> `--set-default-location` explicitly.** `corral-screenshots.sh` always ran
> `defaults write com.apple.screencapture location ~/Pictures/Screenshots && killall
> SystemUIServer` after moving files — a live system-preference change plus a Dock/menu-bar
> restart, unconditionally. `cleanup corral-screenshots --go` moves your existing screenshots but
> **does not** touch that system preference or restart anything. If you also want new screenshots
> to land in `~/Pictures/Screenshots` going forward (stopping the recurring Desktop clutter, not
> just cleaning up what's already there), pass `--set-default-location` as its own explicit,
> independent flag — on Arch Linux this capability isn't supported and is reported as skipped
> rather than erroring.

> ⚠ **Docker pruning now needs `--go` AND `--docker` — not `--go` alone.** `safe-reclaim.sh` ran
> `docker system prune` automatically any time you passed `--go`. The new `cleanup reclaim` makes
> that a deliberate, separate opt-in: Docker layer/volume pruning only runs when **both** `--go` and
> `--docker` are passed. `cleanup reclaim --go` by itself now reclaims build caches, OS junk, the
> recycle bin, and orphaned installers, but leaves Docker alone — add `--docker` if you also want
> that machine-wide prune to run.

## Scripts (`scripts/`)
| Script | What it does | Safety |
|---|---|---|
| `survey.sh` | Snapshot of what's eating space + where the clutter is | read-only |
| `sort-downloads.sh [dir] [--dry]` | Stages a folder into `_sorted/<type>/` buckets by type (screenshots, installers, pdfs, photos, videos, archives, data, docs, other). Unchanged, still works as before — **acts by default**, pass `--dry` to preview. Ported to the CLI as `cleanup sort` (see Status above): note that `cleanup sort` flips this and **previews by default**, needing `--go` to act. | non-destructive (mv within folder) |
| `corral-screenshots.sh` | Moves every screenshot → `~/Pictures/Screenshots` + stops new ones hitting the Desktop | move-only |
| `cleanup corral-screenshots [dir...] [--go] [--from-queue] [--set-default-location]` | Ported version (see Status above and the callout above for the `--set-default-location` gating change vs. the old script) — dry-run by default, stages into the approvals UI, exact moved-count. | **dry-run default**, needs `--go`; system-preference change needs `--set-default-location` separately |
| `safe-reclaim.sh [--go]` | Deletes node_modules, build caches, Docker layers, junk (`.DS_Store`, `~$*`, `$RECYCLE.BIN`) | **dry-run default**, needs `--go` |
| `cleanup reclaim [--go] [--docker]` | Ported version of `safe-reclaim.sh` (see Status above): same categories plus new orphaned-installer detection (macOS-only) and GB-reclaimed reporting; refuses to delete any configured master path not marked `backed_up: true`; Docker pruning needs `--go` **and** `--docker` — see the callout above. | **dry-run default**, needs `--go` |
| `find-wallets.sh [root]` | Finds crypto-wallet artifacts by filename + content; **prints paths only, never key material**. Ported to the CLI as `cleanup find-wallets [root]` (see Status above) — behavior-identical. | read-only |
| `dedupe.sh [dir]` | Lists duplicate files (size + hash) to reclaim. Ported to the CLI as `cleanup dedupe [dir]` (see the callout above for the hash-strength and output-shape changes vs. the old script). | read-only |

## Workflow
1. `survey.sh` — see the picture.
2. Back up masters (Phase 0 in the plan).
3. `safe-reclaim.sh` (dry-run → `--go`; or `cleanup reclaim --go`, once installed — masters not
   marked `backed_up` are refused, and add `--docker` too if you also want the Docker prune) —
   instant, zero-risk GB.
4. `corral-screenshots.sh` (or `cleanup corral-screenshots --go`, once installed — add
   `--set-default-location` too if you also want new screenshots to stop landing on the Desktop
   going forward, see the callout above) — kill the #1 clutter source.
5. `sort-downloads.sh` (or `cleanup sort --go`, once installed — remember it's dry-run by default, unlike the script) — bucket the 857 Downloads, then bulk-act per bucket.
6. Then the plan's Desktop/Documents phases by hand.

## Wallet finding
`find-wallets.sh` (or `cleanup find-wallets [root]`, now ported) scans your machine for wallet
files (`wallet.dat`, keystores, `UTC--*`, `.kdbx`, seed/mnemonic files, and more — 14 filename
patterns total) and content signatures (BIP39 phrases, `xprv`, PEM private keys, Ethereum keystore
JSON) under `Documents`/`Desktop`/`Downloads`. It **only lists candidate paths** — you open them.
Once `sort-downloads.sh` has bucketed things, the wallet is far easier to spot in the
`data`/`docs`/`archives` buckets.

> ⚠ The wallet finder is for **your own** machine/wallet. It never prints or transmits secrets — paths only.
> In the ported CLI command this is a structural guarantee, not just a convention: the code path
> that finds a content match never extracts or stores the matched text anywhere, so there is no
> code path that *could* leak it, verified by both the implementer and an independent adversarial
> review with real secret-shaped test fixtures.

See `docs/CLEANUP-PLAN.md` for the full grounded plan and `docs/REQUIREMENTS.md` for the build-out spec.

<!-- shared:support -->
## Support this project

Free and open source, always. A few ways to help — or just say hi:

- **Use it, star it, file an issue.** Honestly the best support an open-source project can get. → [this project](https://github.com/mdostal/cleanup-tools)
- **Hire me.** I do fractional-CTO and consulting work — fixing and scaling tech stacks. → [mdostal.com/contact](https://mdostal.com/contact)
- **[Buy me a coffee](https://www.buymeacoffee.com/mdostal)** if it saved you time.
- **More tools like this** → [tools.mdostal.com](https://tools.mdostal.com)
- **Life outside the terminal** → [life.mdostal.com](https://life.mdostal.com)
- **What we're building at Firefly Events** — event discovery, 8,000+ events/day from 7+ sources → [ff.events](https://ff.events)

Always up for a conversation if any of it's useful to you.
<!-- /shared:support -->

# cleanup-tools

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
packaged CLI's first full round — while `find-wallets`, `dedupe`, and `corral-screenshots` remain
bash-only for now, pending future epics. The CLI now reads (and creates, if absent) an optional
config file at `~/.config/cleanup-tools/config.yaml` for bucket rules, search roots, and master
paths — sensible defaults apply if the file isn't there.

An approval queue store (`src/cleanup_tools/queue.py`) now also exists, persisting proposed
move/delete actions as YAML at `~/.config/cleanup-tools/approval_queue.yaml`, with file locking and
atomic writes so it's safe to touch from multiple processes at once. This is foundation only, not
yet usable end-to-end — nothing writes to it, no command reads from it (no `--from-queue` flag
yet), and there's no UI to approve/reject entries. It's a separate file from `config.yaml` rather
than a section within it, so a future UI process and CLI commands can read/write queue entries
concurrently without contending over the same lock as unrelated config changes.

> ⚠ **Flag polarity flip: `cleanup sort` defaults to dry-run — the opposite of `sort-downloads.sh`.**
> `sort-downloads.sh` **acted by default** and needed `--dry` to preview. The new `cleanup sort`
> **previews by default** and needs `--go` to actually move files. If you're used to running the bash
> script, running `cleanup sort` with no flags will **not** move anything — pass `--go` to act. This
> matches the CLI's overall dry-run-by-default convention (see Principles above), but it is a real
> reversal from the old script's convention, so don't assume old muscle memory carries over.

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
| `safe-reclaim.sh [--go]` | Deletes node_modules, build caches, Docker layers, junk (`.DS_Store`, `~$*`, `$RECYCLE.BIN`) | **dry-run default**, needs `--go` |
| `cleanup reclaim [--go] [--docker]` | Ported version of `safe-reclaim.sh` (see Status above): same categories plus new orphaned-installer detection (macOS-only) and GB-reclaimed reporting; refuses to delete any configured master path not marked `backed_up: true`; Docker pruning needs `--go` **and** `--docker` — see the callout above. | **dry-run default**, needs `--go` |
| `find-wallets.sh [root]` | Finds crypto-wallet artifacts by filename + content; **prints paths only, never key material** | read-only |
| `dedupe.sh [dir]` | Lists duplicate files (size + hash) to reclaim | read-only |

## Workflow
1. `survey.sh` — see the picture.
2. Back up masters (Phase 0 in the plan).
3. `safe-reclaim.sh` (dry-run → `--go`; or `cleanup reclaim --go`, once installed — masters not
   marked `backed_up` are refused, and add `--docker` too if you also want the Docker prune) —
   instant, zero-risk GB.
4. `corral-screenshots.sh` — kill the #1 clutter source.
5. `sort-downloads.sh` (or `cleanup sort --go`, once installed — remember it's dry-run by default, unlike the script) — bucket the 857 Downloads, then bulk-act per bucket.
6. Then the plan's Desktop/Documents phases by hand.

## Wallet finding
`find-wallets.sh` scans your machine for wallet files (`wallet.dat`, keystores, `UTC--*`, `.kdbx`,
seed/mnemonic files) and content signatures (BIP39 phrases, `xprv`, PEM private keys, Ethereum keystore
JSON). It **only lists candidate paths** — you open them. Once `sort-downloads.sh` has bucketed things,
the wallet is far easier to spot in the `data`/`docs`/`archives` buckets.

> ⚠ The wallet finder is for **your own** machine/wallet. It never prints or transmits secrets — paths only.

See `docs/CLEANUP-PLAN.md` for the full grounded plan and `docs/REQUIREMENTS.md` for the build-out spec.

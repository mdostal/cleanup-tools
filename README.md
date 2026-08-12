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
The bash scripts in `scripts/` below are still the current, working tool for `sort`/`reclaim` —
nothing about how you use those has changed. A packaged Python CLI is being built in
`src/cleanup_tools/`, following the build-out plan in `docs/REQUIREMENTS.md`; `cleanup survey`
(installed via `pip install -e .`, run as `cleanup survey` or `python3 -m cleanup_tools.cli survey`)
is now a real, working ported command, on top of the cross-platform OS-adapter foundation (macOS +
Arch Linux). Keep using `sort-downloads.sh`/`safe-reclaim.sh` until the CLI has ported those too and
this README says otherwise. The CLI now reads (and creates, if absent) an optional config file at
`~/.config/cleanup-tools/config.yaml` for bucket rules, search roots, and master paths — sensible
defaults apply if the file isn't there.

## Scripts (`scripts/`)
| Script | What it does | Safety |
|---|---|---|
| `survey.sh` | Snapshot of what's eating space + where the clutter is | read-only |
| `sort-downloads.sh [dir] [--dry]` | Stages a folder into `_sorted/<type>/` buckets by type (screenshots, installers, pdfs, photos, videos, archives, data, docs, other) | non-destructive (mv within folder) |
| `corral-screenshots.sh` | Moves every screenshot → `~/Pictures/Screenshots` + stops new ones hitting the Desktop | move-only |
| `safe-reclaim.sh [--go]` | Deletes node_modules, build caches, Docker layers, junk (`.DS_Store`, `~$*`, `$RECYCLE.BIN`) | **dry-run default**, needs `--go` |
| `find-wallets.sh [root]` | Finds crypto-wallet artifacts by filename + content; **prints paths only, never key material** | read-only |
| `dedupe.sh [dir]` | Lists duplicate files (size + hash) to reclaim | read-only |

## Workflow
1. `survey.sh` — see the picture.
2. Back up masters (Phase 0 in the plan).
3. `safe-reclaim.sh` (dry-run → `--go`) — instant, zero-risk GB.
4. `corral-screenshots.sh` — kill the #1 clutter source.
5. `sort-downloads.sh` — bucket the 857 Downloads, then bulk-act per bucket.
6. Then the plan's Desktop/Documents phases by hand.

## Wallet finding
`find-wallets.sh` scans your machine for wallet files (`wallet.dat`, keystores, `UTC--*`, `.kdbx`,
seed/mnemonic files) and content signatures (BIP39 phrases, `xprv`, PEM private keys, Ethereum keystore
JSON). It **only lists candidate paths** — you open them. Once `sort-downloads.sh` has bucketed things,
the wallet is far easier to spot in the `data`/`docs`/`archives` buckets.

> ⚠ The wallet finder is for **your own** machine/wallet. It never prints or transmits secrets — paths only.

See `docs/CLEANUP-PLAN.md` for the full grounded plan and `docs/REQUIREMENTS.md` for the build-out spec.

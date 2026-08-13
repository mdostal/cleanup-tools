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
- **Wallet finder** — `scripts/find-wallets.sh`: scans for crypto-wallet artifacts by filename
  (`wallet.dat`, `keystore*`, `UTC--*`, `.kdbx`, seed/mnemonic files) and content signatures (BIP39
  phrases, `xprv`, PEM private keys, Ethereum keystore JSON). **Paths-only** — never prints, logs, or
  transmits the matched secret/key material.
- **Dedupe** — `scripts/dedupe.sh`: lists duplicate files by size+hash so the user can decide what to
  remove; never auto-deletes.
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
  lock as unrelated config changes. `cleanup sort --from-queue` / `cleanup reclaim --from-queue`
  execute approved entries; `cleanup approve` (`src/cleanup_tools/ui/`) is the localhost-only Flask
  UI that stages (`/plan/sort`, `/plan/reclaim`) and reviews (approve/reject/undo) entries — it never
  executes, execution is always a separate deliberate CLI step.

## Conventions

- Every destructive script defaults to dry-run; `--go` is the only way to make it act. Any new tool
  must follow this pattern (see `scripts/safe-reclaim.sh` for the reference implementation).
- Uncertain files are staged into `_sorted/<type>/` or `_REVIEW/` — never deleted blind.
- The wallet/secret finder outputs `path · match-type · confidence` — never the matched value itself.
- **No network, no telemetry.** This tool touches personal files end-to-end and must stay fully local
  (see `.pHive/project-profile.yaml → north_star.avoid`).
- No CLAUDE.md exists yet — until one is added, `.pHive/project-profile.yaml → claude_md_summary`
  is the authoritative source for build/rule conventions.
- The sole sanctioned exception to "no network, no telemetry": explicit, user-triggered AI calls
  via `src/cleanup_tools/ai/` (`AIProvider` ABC + `AnthropicProvider`). The SDK itself must never
  phone home on its own (verified against the installed SDK's source, not assumed) — client
  construction always passes `api_key=` explicitly so the SDK can't silently fall back to an
  OAuth-profile/Workload-Identity-Federation credential we don't control. API keys live in
  `ANTHROPIC_API_KEY` or `~/.config/cleanup-tools/credentials` (0600-enforced) — never in
  `config.yaml` or `approval_queue.yaml`. As of this writing the provider layer is standalone,
  not wired into the CLI/UI/queue yet.

## Canonical references

- `README.md` — script table, workflow order, wallet-finding overview.
- `docs/CLEANUP-PLAN.md` — the phased cleanup plan this repo was built from.
- `docs/REQUIREMENTS.md` — the full build-out spec, hard rules, and wallet-finder signatures.
- `.pHive/project-profile.yaml` — Hive's structured discovery profile (north star, integrations,
  code quality, ship target).

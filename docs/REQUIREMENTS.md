# cleanup-tools — build-out requirements

The shell scripts are the working reference. This spec is for the hive to harden them into a real,
repeatable local tool (a CLI, and optionally a small local UI) for tidying a Mac and keeping it clean.

## Goals
1. **Go through & sort** Documents / Desktop / Downloads / screenshots into a consistent structure.
2. **Safe reclaim** of regenerable junk (node_modules, caches, Docker, OS junk).
3. **Keep it clean** — recurring triage so Downloads/Desktop return to near-zero.
4. **Find things** — a fast local search, including the **lost-wallet** use case.

## Hard rules (carry into every tool)
- **Dry-run by default on anything destructive; explicit `--go` to act.** Print exactly what will change.
- **Never delete without a backup of masters.** The tool should refuse to delete flagged "master" paths
  (SMS backup, family footage, tax, patent, `desktop back`) until they're marked backed-up.
- **Stage, don't trash.** Uncertain files → `_sorted/<type>/` or `_REVIEW/`, reversible.
- **Secrets are paths-only.** The wallet/secret finder lists locations; it must NEVER print, log, copy,
  or transmit key material.

## Components to build
1. **`survey`** — machine snapshot (disk, top-level sizes, biggest repos, node_modules total, Downloads
   type histogram, screenshot count). Output JSON so the UI/other tools can consume it.
2. **`sort`** — the bucket-stager. Config-driven ext→bucket map (extend the reference `bucket()`), plus
   content-aware rules (a `screenshot*` png → screenshots, not photos). Idempotent; `--dry` preview.
3. **`corral-screenshots`** — move + set macOS `com.apple.screencapture location`.
4. **`reclaim`** — regenerable/junk deletion. Categories: node_modules, `.next`/`.turbo`/`dist`, Docker
   prune, OS junk, orphaned installers (`.dmg/.pkg` whose app is already in `/Applications`). Report GB
   reclaimed. Dry-run default.
5. **`find`** — fast local search by name/type/content. First-class **wallet/secret finder** mode
   (filename signatures + content signatures below). Paths-only output, ranked by confidence.
6. **`dedupe`** — size+hash duplicate detection; propose keep/remove (never auto-delete).
7. **Recurring "keep-clean"** — a scheduled/menu-bar reminder that re-runs `survey` + flags when
   Downloads/Desktop cross a clutter threshold, and offers a one-tap `sort`.

## Wallet-finder signatures (the recovery use case)
- **Filenames:** `wallet.dat`, `*.wallet`, `keystore*`, `UTC--*`, `*.kdbx`, `electrum*`, `default_wallet`,
  `*mnemonic*`, `*seed*phrase*`, `*recovery*phrase*`, `*.keychain`, `metamask*`, `exodus*`, `atomic*wallet*`.
- **Content:** BIP39 phrase (12/18/24 lowercase words), `xprv…`, `-----BEGIN … PRIVATE KEY-----`,
  Ethereum keystore JSON (`{"version":3,"crypto":{…"cipher"…}}`), long `0x`-hex.
- **Also worth scanning:** browser-extension storage (MetaMask), `~/Library/Application Support/<wallet apps>`,
  password-manager exports, `Documents/crypto/`, old `.zip` exports (unzip-and-scan in a temp dir, then wipe).
- **Output:** `path · match-type · confidence` — never the matched value.

## Target structure it sorts into
```
~/Documents/{work, personal, references, archive}
~/Pictures/{Screenshots, Photos-inbox}
~/Media/video   (or external drive)
~/Desktop  → near-empty (+ _REVIEW/ staging)
~/Downloads → transient, triaged to zero
```
(See `CLEANUP-PLAN.md` for the full category→destination table and phased plan.)

## Stack
Shell reference now. Harden as a Node/TS or Python CLI (packaged, testable), config file for the
ext→bucket + master-paths + search-roots. Optional tiny local UI later. No network, no telemetry —
this touches personal files and must stay fully local.

# Computer Cleanup & File-Organization Plan

Survey date: 2026-08-10. Disk: 926 GB, **~186 GB free** — space isn't critical; this is an
**organization** job. Goal: everything in Documents, Desktop, Downloads, and the 473 screenshots
**gone through, sorted, and arranged** into a structure that stays clean.

---

## What's actually here (the mess, by the numbers)

| Location | Size | The problem |
|---|---|---|
| `~/Library` | 201 GB | 62 GB **Caches** + 20 GB Containers — app data, mostly not user-cleanup (leave; caches reclaimable) |
| `~/Desktop` | 46 GB | **87 screenshots** loose + big folders: `drone-jobs` 22 GB (organized ✓), **`desktop back` 17 GB** (old-machine dump — triage), `drone - prado` 7.1 GB (flight-1 footage — merge into drone-jobs), `$RECYCLE.BIN` (Windows junk) |
| `~/Documents` | 43 GB | `work/` (events 19 GB, personal 14 GB, clients 4.4 GB…) + non-work (3d-printing 771 MB, crypto 279 MB, taxes, patent) + **loose junk in root** (Office `~$` lock files, `$RECYCLE.BIN`, stray files) |
| `~/Downloads` | 36 GB | **857 items** — 286 PDFs, 260 photos, 82 zips, **27 installers (.dmg)**, big backups (SMS 2.8 GB, Family Trip zip 2.8 GB, exports) |
| **Screenshots** | — | **473 total across home** — the single biggest clutter source |

---

## Principles (non-negotiable)

1. **Back up the irreplaceable BEFORE deleting anything.** Masters: SMS backup, family-trip footage,
   tax docs, patent app, the `desktop back` dump → external/cloud first.
2. **Sort is reversible; delete is not.** Anything uncertain → move to a `~/Desktop/_REVIEW/` staging
   folder, never straight to trash. Only *obvious* junk gets deleted (installers, `$RECYCLE.BIN`,
   `~$` Office locks, `.DS_Store`).
3. **Nothing gets deleted without your say-so.** The safe-reclaim commands below are ready to run, but
   you greenlight each phase.
4. **Regenerable ≠ precious.** `node_modules`, build caches, Docker layers = delete freely (rebuild on demand).

---

## Target structure (where things land)

```
~/Documents/
  work/            code + active projects (already here — just prune)
  personal/        3d-printing · crypto · patent-app · taxes · obsidian  (consolidate non-work here)
  references/      the keeper PDFs (manuals, receipts, contracts, docs)
  archive/         finished/dead projects, old exports
~/Pictures/
  Screenshots/     ALL 473 screenshots corralled here (+ change macOS default so new ones land here)
  Photos-inbox/    loose jpg/png from Downloads until filed
~/Media/  (or an EXTERNAL drive)
  video/           family trip, pitch videos, drone raws
~/Desktop/         KEPT NEARLY EMPTY — only the truly active thing + _REVIEW/ staging
~/Downloads/       transient only — triaged weekly to zero
```

---

## Category → destination rules

| You find… | It goes to… |
|---|---|
| Screenshot (`Screenshot*.png`) | `~/Pictures/Screenshots/` |
| Installer (`.dmg` / `.pkg`) | **delete** (app's already installed) |
| Zip / export / backup | extract → file the contents, then archive the zip to external; or delete if superseded |
| PDF — manual/receipt/contract/doc | `~/Documents/references/` (sub-foldered by type) |
| Loose photo (`.jpg/.jpeg/.png`) | `~/Pictures/Photos-inbox/` |
| Video (`.mp4/.mov`) | `~/Media/video/` or external |
| Code / project folder | `~/Documents/work/` |
| `~$*`, `.DS_Store`, `$RECYCLE.BIN`, Thumbs.db | **delete** (junk) |
| Office/docx working files | `~/Documents/` sub-foldered by project |

---

## Phased execution

### Phase 0 — Back up the irreplaceable (do first, no deletes yet)
Copy to external/cloud: `Downloads/SMS Backup*.xml`, `Downloads/Family Reunion Trip*.zip`,
`Documents/taxes/`, `Documents/patent app/`, and eyeball `Desktop/desktop back/` for anything unique.

### Phase 1 — Safe reclaim (zero-risk GB, ready to run)
```bash
# regenerable build stuff (rebuild anytime with npm/pnpm install + build)
find ~/Documents -type d -name node_modules -prune -exec rm -rf {} +      # 40 dirs
find ~/Documents -type d \( -name .next -o -name .turbo -o -name dist \) -prune -exec rm -rf {} +
docker system prune -af --volumes                                          # ODM layers etc.
# obvious junk
find ~ -maxdepth 3 -name '.DS_Store' -delete 2>/dev/null
find ~/Documents ~/Desktop -maxdepth 2 -name '~$*' -delete 2>/dev/null      # Office lock files
rm -rf ~/Desktop/\$RECYCLE.BIN ~/Documents/\$RECYCLE.BIN                     # Windows recycle bins
```

### Phase 2 — Corral the 473 screenshots
```bash
mkdir -p ~/Pictures/Screenshots
find ~/Desktop ~/Downloads ~/Documents -maxdepth 2 -iname 'screenshot*' -exec mv {} ~/Pictures/Screenshots/ \;
# stop the bleed — new screenshots land in Pictures/Screenshots, not Desktop:
defaults write com.apple.screencapture location ~/Pictures/Screenshots && killall SystemUIServer
```

### Phase 3 — Triage Downloads (857 → near zero)
- Delete the **27 installers** (`.dmg/.pkg`) after confirming apps are installed.
- Move PDFs → `~/Documents/references/`, photos → `~/Pictures/Photos-inbox/`, videos → `~/Media/video/`.
- The 82 zips: extract keepers, archive the rest to external, delete superseded exports.
- *(A sorter script can stage these by type into `~/Downloads/_sorted/<type>/` for one-pass review — see "Automate" below.)*

### Phase 4 — Desktop big folders
- `drone - prado` (7.1 GB) → merge into `~/Desktop/drone-jobs/` as `2026-XX-XX_prado_flight1/` (per the
  drone-jobs convention already in that folder's README).
- `desktop back` (17 GB) → this is an old-machine dump; **review carefully**, pull anything unique into
  the real structure, then archive/delete the rest.
- Delete `$RECYCLE.BIN`.

### Phase 5 — Documents
- Consolidate non-work (`3d-printing`, `crypto`, `patent app`, `taxes`, `obsidian`) under `~/Documents/personal/`.
- Prune finished repos in `work/` to `work/archive/`; the big ones (`events` 19 GB, `personal` 14 GB) —
  move build artifacts/media out to external, keep the code.
- Clear the loose junk files in `Documents/` root.

---

## Automate (optional, for the 857-file Downloads + 473 screenshots)
A `sort-downloads.sh` can move files into `_sorted/<screenshots|installers|pdfs|photos|videos|zips|docs|other>/`
by extension in one pass — **staging, not deleting** — so you review buckets instead of 857 individual
files, then bulk-act on each bucket. Say the word and I'll write it.

## Guardrails
- Never delete: `desktop back` contents until reviewed · SMS/family/tax/patent masters until backed up ·
  anything in `work/` that isn't clearly `node_modules`/build/dead.
- Everything uncertain → `~/Desktop/_REVIEW/`, decide later.
- The other agent running the cleanup executes; this doc is its plan.

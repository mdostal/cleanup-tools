# Research: Prior Art — Cleanup-Tool UX, Settings/Preferences UX, and AI-Assisted Organization UX

Three parallel deep-research passes (2026-08-15), feeding the `guided-sort-and-cluster` epic and a new `settings-and-transparency` epic. Full per-tool findings and citations live in the three source agent reports; this doc is the synthesized, cross-cutting takeaway set.

## The one pattern every credible tool in all three passes converges on

**Preview before commit, tiered confidence, per-item undo.** Every actively-developed, well-regarded tool researched — commercial (DaisyDisk, Gemini 2, Hazel, DEVONthink) and open-source (Local-File-Organizer, ai-file-sorter, file-organizer, ai-filesense, Paperless-ai) — independently arrived at the same three-part shape:

1. Nothing irreversible happens without an explicit human action on that specific item/batch (never a silent auto-apply).
2. Confidence is expressed as a *bucket* (safe-to-auto-select / needs-review / low-confidence-catch-all), not a raw percentage — raw scores produce alert fatigue and false trust.
3. Undo is a **logged, reversible transaction**, not "restore from Trash and hope."

cleanup-tools' existing approval-queue design already has (1). This research is mainly about sharpening (2) and building (3) explicitly, plus a fourth pattern that recurred everywhere but doesn't exist in cleanup-tools yet: **preview a rule/suggestion against real data before it's ever allowed to run unattended** (Hazel's "Preview Rule," multiple OSS tools' "dry run" toggle).

## Findings that change `guided-sort-and-cluster` (epic #1, already in planning)

- **Hard-block protected paths from ever entering the queue** (DaisyDisk's Collector rejects `/System`, `/Library`, home-root outright) — stronger than a warning dialog. Worth an explicit story: certain path classes should never be stageable, not just flagged.
- **Require expansion of aggregated/grouped items before bulk action** (DaisyDisk) — an opaque "42 items" branch shouldn't be one-click-approvable without at least a summary of what's in it; the tree view should show real counts/categories per branch, not just a number.
- **Named, per-branch bulk-selection strategies, not one blunt "select all"** (Gemini 2: Automatically/Newest/Oldest/Any, applied per group) — worth considering as a light extension to the tree's per-branch actions beyond plain approve/reject/undo-all (e.g., "approve everything older than N days in this branch").
- **Concrete, factual labels — never vague aggregate/marketing language** ("System Junk," inflated numbers) — this is a direct anti-pattern from the CleanMyMac-adjacent scareware category; cleanup-tools should keep showing real paths/sizes/dates, which the tree view design already does, and this is worth stating as an explicit, permanent design principle, not just an accident of the current implementation.

## Findings that justify a new `settings-and-transparency` epic

Today's Settings page is one flat section (icon picker only) behind a plain text nav link — there's no gear icon, no sub-navigation, no history/audit view anywhere in the app. Research strongly supports growing this into its own epic rather than folding it into `guided-sort-and-cluster`:

- **Entry point**: pull Settings out of the peer-level nav (`Dashboard | Review Queue | Settings | Plan: ...`) into a dedicated gear icon, bound to `Cmd+,` (macOS convention) — matches the explicit ask for "a big gear icon in the top right."
- **Structure**: Raycast/Obsidian-style sidebar-of-sections (General, Bucket Rules, Search Roots & Locations, Master-Path Backups, AI Provider, Advanced) rather than one long scrolling page. Each config *list* (bucket rules, search roots) gets its own sub-list where each item drills into its own pane, rather than flattening every rule's fields onto one page.
- **Progressive disclosure**: one `Advanced` section/toggle (CleanShot X / Safari's "Show features for web developers" pattern), never more than two levels deep (NN/g's rule) — houses things like raw-JSON config view/edit (Sublime/VS Code's "GUI and JSON are two views of one source of truth" model), log verbosity, cache paths.
- **Transparency = a real History view, not just Trash-as-safety-net.** A Dropbox-style reverse-chronological action log (what moved/deleted/renamed, when, which rule/AI proposal triggered it) with a **per-row Undo** (Hazel 6's "undo travels with the artifact" model — right-click an item, undo that specific action — not one global undo stack). This is the single most direct, concrete answer to "we need more transparency," and it's a genuine differentiator: research confirms CleanMyMac (the closest commercial competitor) has no exportable log or scan history at all.
- **Trust signal, given local-first is the whole point**: surface "0 network requests made" / last-check timestamps for the (rare, opt-in) network features directly in the UI. The 2024 Bartender trust collapse (quiet ownership change + undisclosed telemetry addition, discovered only via a third-party cert-change alert) is a strong cautionary tale for exactly this category of deep-filesystem-access tool — loud, visible transparency about what does and doesn't phone home is cheap insurance against exactly that failure mode.

## Findings to carry forward for the (not-yet-planned) AI-sort-scope and semantic-clustering epics

Not actionable for the current two epics, but worth preserving so they aren't re-researched later:

- **Confidence bucketing, not scores**: safe/auto-select (exact hash match, well-known cache path) vs. needs-review (heuristic/fuzzy match) vs. low-confidence catch-all bucket (ai-filesense's literal "Review" folder; thebearwithabite's 85% cutoff) — keeps the review queue small enough to actually get reviewed instead of demanding 100% manual triage.
- **Mylio's four-verb batch review** (Approve / Reject-this-one / Ignore / Skip) is the most granular, well-reasoned model found for reviewing a cluster/batch of AI suggestions — directly applicable to semantic-clustering review UX.
- **Hazel's live "Preview Rule"** (green/red per-condition, "Rule matches"/"does not match" against a real file, before the rule is ever armed) is the gold-standard pattern for building trust in an AI-authored or user-authored sort rule before it runs unattended — should inform the AI-sort-scope epic's rule-authoring UX directly.
- **Feed corrections back in** (ai-file-sorter's "consistency layer," thebearwithabite's adaptive learning DB) — a wrong suggestion, corrected once, shouldn't recur. Worth a story once any AI-proposal feature exists, not before.
- **DEVONthink's Option-drag "Replicate"** (provisionally place in a cluster without a destructive commit) is a nice non-destructive middle ground for semantic clustering specifically, where cluster boundaries are genuinely fuzzy.

## Sources

Full citations preserved in the three source research passes (competitor Mac-cleaner UX; settings/preferences patterns; rule-based & semantic organization prior art), run 2026-08-15. Key tools referenced: CleanMyMac X, DaisyDisk, OnyX, AppCleaner, Hazel, Gemini 2 (MacPaw), VS Code, Raycast, Obsidian, Sublime Text/Merge, Bartender, CleanShot X, DEVONthink, Paperless-ngx/paperless-ai/paperless-gpt, Apple Photos, Google Photos, Mylio, and five open-source AI file-organizer projects (Local-File-Organizer, ai-file-sorter, file-organizer, ai-file-organizer, ai-filesense).

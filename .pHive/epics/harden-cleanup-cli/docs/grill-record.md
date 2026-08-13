# Grill Record — harden-cleanup-cli

**Source draft:** .pHive/epics/harden-cleanup-cli/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass — the research-brief skill's standard template has no `inconsistency_risk_signals` field; not populated for this epic)
**round_number:** 1
**unresolved_count:** 3
**Generated:** 2026-08-10

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: 2 findings
- Unresolved tensions: 1 finding
- Convention violations: clean
- Posture mismatches: not applicable

## Vocabulary mismatches

Clean. Draft terminology (buckets, masters, dry-run/`--go`, `_sorted`/`_REVIEW`) matches `.pHive/CONTEXT.md` throughout and is used consistently across sections.

## Hidden assumptions

- **H1** — §3 step 3 asserts REQUIREMENTS.md's "output JSON so the UI/other tools can consume it" is "UI-driven scope" and defers it, without addressing the "other tools" half of that sentence.
  - Draft location: §3, step 3 ("Output format: keep human-readable stdout for now... is UI-driven scope I'm deferring")
  - Why this matters: `docs/REQUIREMENTS.md`'s actual wording names "the UI/**other tools**" — plural, and not obviously UI-only. If `survey`'s JSON output has value independent of the UI (e.g. as a machine-checkable test fixture, or for the not-yet-built "keep-clean" trigger to consume), deferring it here could mean re-doing survey's output contract in a later epic instead of designing it once, now, while survey is the cheapest of the three commands to get right.
  - Question for planner: is JSON output for `survey` genuinely UI-gated, or is there a non-UI consumer (tests, the future keep-clean trigger) that means it belongs in this epic's story for `survey`?

- **H2** — §7's manual verification step is ambiguously worded about what it runs against.
  - Draft location: §7, Verification Strategy → Manual ("one full run-through of `sort` against a real messy Downloads-shaped fixture directory")
  - Why this matters: "a real messy Downloads-shaped fixture directory" parses two ways — (a) a synthetic fixture directory built to *look like* a messy real Downloads, or (b) the author's actual `~/Downloads`. Those have very different risk profiles: this project's own hard rules (no network/no telemetry is unrelated, but "stage, don't trash" and "never delete without backup of masters" are exactly the rules a real run would need to prove out on **real personal data**). If (b) is intended, that's worth stating explicitly as a deliberate choice, not something a reader has to disambiguate from phrasing.
  - Question for planner: does manual verification run against a synthetic fixture (safer, but doesn't prove the tool against the actual mess this project exists to solve) or the author's real `~/Downloads` (higher signal, but the first-ever run of new code against irreplaceable personal files)?

## Unresolved tensions

- **U1** — Open question 1's runtime recommendation is partly justified by easing a future epic (desktop UI), which sits in tension with `north_star.avoid`'s explicit instruction to not over-build for the future before the local tool works.
  - Draft location: §6, Open Question 1 ("My lean is **Node/TS**... it sets up a natural on-ramp to the desktop-UI epic later")
  - Tension: `project-profile.yaml → north_star.avoid` says "Over-building for distribution/packaging before the local tool works reliably" is exactly what to avoid. Picking a runtime *because* it eases a not-yet-started, explicitly-deferred UI epic is a small instance of optimizing for the deferred thing over the immediate one, even though the recommendation is framed as a CLI-scoped decision.
  - Question for planner: should the runtime choice be made on CLI-only merits (packaging ergonomics, test-runner ecosystem, string/file-path handling for this specific set of commands) with the desktop-UI on-ramp noted as a tie-breaker at most rather than a leading reason — or is paving that on-ramp now explicitly acceptable given it's a one-time, low-cost decision (unlike deferring actual UI code)?

## Convention violations

Clean. No prior feedback memos or established conventions exist for this project yet (confirmed at kickoff — single commit, no CLAUDE.md, no prior Hive planning history) for the draft to contradict.

## Posture mismatches

Not applicable. Hive's own architectural posture (composable substrate, atomic skills) governs Hive's internal development, not a downstream consumer project's CLI design. No posture reference applies here.

## Notes

The draft is otherwise well-grounded — every claim in §2/§3/§4 cites a specific script, line range, or doc. The three findings above are all resolvable by answering/tightening rather than restructuring; none suggest the draft's overall approach is wrong.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings. Each finding above ends with a question for the planner; resolving them (by draft revision or an explicit accepted-deviation note) is the next step, not this one.

# Grill Record — ai-approvals-ui

**Source draft:** .pHive/epics/ai-approvals-ui/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass — same as prior epic, standard research-brief template has no such field)
**round_number:** 1
**unresolved_count:** 3
**Generated:** 2026-08-11

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: 2 findings
- Unresolved tensions: 1 finding
- Convention violations: clean
- Posture mismatches: not applicable

## Vocabulary mismatches

Clean. Draft terminology (buckets, plan, dry-run/`--go`, master paths) matches `.pHive/CONTEXT.md` and the prior epic's established usage throughout.

## Hidden assumptions

- **H1** — §3 step 3 assumes `--go`'s relationship to the approval queue without stating it: does adding an approval queue make going through the UI a REQUIRED gate before `--go` can act, or is it purely additive (an optional review path that coexists with today's direct `--go` behavior)? These have very different implications: the former changes `sort`/`reclaim`'s already-shipped, already-tested contract; the latter risks the approval queue becoming decorative if `--go` still works standalone with no forcing function to use it.
  - Draft location: §3 step 3 ("Wire `sort --go`/`reclaim --go` to consume the approval queue instead of (or in addition to) their current 'act on the whole plan' behavior")
  - Why this matters: the prior epic's `sort`/`reclaim` stories both have hard-won safety guarantees (per-entry error isolation, master-paths refusal) built around "the plan is the plan, `--go` executes it." Silently changing what `--go` means is exactly the kind of undocumented behavior shift that epic's own reviews kept catching in other forms.
  - Question for planner: is approval-gating mandatory once the queue exists, optional via a flag, or does direct `--go` remain fully independent of the UI/queue entirely (queue is purely an additional, parallel review path some users may never touch)?

- **H2** — The approval-queue design (§3 step 1) references "the plan entry it came from" without addressing staleness: a plan is computed from a live filesystem walk; if time passes between plan computation and queue review (the whole point of an approval queue is to defer action), the referenced file could move, be deleted, or change size in the interim.
  - Draft location: §3 step 1 ("a queue of proposed actions... each referencing the plan entry it came from")
  - Why this matters: an approved action executing against a src path that no longer matches what was reviewed (different file now sitting at that path, or the file already gone) undermines the entire point of a review-before-action safety model — the prior epic's reclaim-command story treated exactly this class of race condition (a file deleted mid-run) as a critical, must-fix finding.
  - Question for planner: does approval execution re-verify the entry against the current filesystem state before acting (and refuse/re-flag if it's changed), or is a snapshot-at-plan-time treated as good enough for this epic's scope?

## Unresolved tensions

- **U1** — The draft's sequencing decision (manual approvals UI first, AI second, §1) quietly de-prioritizes the AI half of what the user explicitly asked for by name ("AI-provider-API and approvals-UI epic"), without surfacing this reframing as something for the user to actively confirm rather than just read past in a design doc.
  - Draft location: §1 ("If the AI half turns out to need its own epic once the manual half is built, that's a legitimate outcome of this sequencing, not a failure of it.")
  - Tension: the user asked for both by name in one epic; the draft's internal risk analysis (reasonably) concludes AI should come second and might slip out entirely — but that's a real scope interpretation, not just a sequencing detail, and it's currently framed as something the planner decided rather than something the user weighed in on.
  - Question for planner: does the user explicitly confirm the manual-UI-first / AI-second sequencing (and accept that AI might land in a follow-on epic), or do they want both halves committed to within this epic regardless of how the slicing falls out?

## Convention violations

Clean. No established UI/AI conventions exist yet in this project for the draft to contradict.

## Posture mismatches

Not applicable — consumer-project CLI/UI design, not Hive-internal architecture.

## Notes

The draft's "no *ambient* network" reframing (§2, §5) is a genuinely useful precision improvement over the research brief's flagging of the raw tension — it doesn't need further grilling, it's already a resolved, well-reasoned position. The UI-technology recommendation (§6 Q1) is well-argued (visual files → needs image rendering → browser over TUI) but should explicitly commit to binding the local server to `127.0.0.1` only, not just note that loopback "stays within" the no-network constraint — that's a concrete implementation detail worth pinning down now rather than assuming.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings. Each finding above ends with a question for the planner; resolving them (by draft revision, an explicit accepted-deviation note, or asking the user) is the next step.

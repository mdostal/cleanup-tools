# Grill Record — guided-sort-and-cluster

**Source draft:** `.pHive/epics/guided-sort-and-cluster/docs/design-discussion.md`
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (research brief predates the signal-emission convention; heuristic pass against research brief + CONTEXT.md + project-profile.yaml directly)
**round_number:** 1
**unresolved_count:** 5
**Generated:** 2026-08-15T02:00:00Z

## Summary

- Vocabulary mismatches: 1 finding
- Hidden assumptions: 3 findings
- Unresolved tensions: 1 finding
- Convention violations: clean
- Posture mismatches: clean

## Vocabulary mismatches

- **V1** — "root-slug" is used throughout §3 (Proposed approach) as load-bearing terminology (`sort:<root-slug>:<bucket>`) but is never precisely defined — is it a literal lowercase dir name (`downloads`), a sanitized/slugified form of an arbitrary path segment, or a fixed enum of known roots? The draft's own §4 risk ("root-dir inference ambiguity") acknowledges the concept is fuzzy but the approach section treats it as already well-defined.
  - Draft location: §3 "Schema" paragraph
  - Reference: draft's own §4 "Root-dir inference ambiguity" risk, which contradicts the confidence of §3's usage
  - Question for planner: is `root-slug` a fixed enum (`downloads`/`desktop`/`documents`/`other`) decided at design time, or a derived-at-runtime value? The story that implements this needs one concrete answer, not the current "derived from Path(entry.src)'s relationship to the adapter's known standard dirs" hand-wave.

## Hidden assumptions

- **H1** — §3 assumes `Path(entry.src)`'s relationship to standard dirs is resolvable via some unspecified method, but doesn't name the actual resolution algorithm (prefix match on the resolved absolute path? symlink-aware `realpath` comparison? substring match on `resolve_standard_dir()`'s return value?). The research brief confirms `adapter.resolve_standard_dir("downloads")` exists and is called for sort's default target, but never confirms it exists/works symmetrically for `"desktop"`/`"documents"`, nor that it's the right primitive to reuse for classifying an arbitrary `src` after the fact.
  - Draft location: §3 "Schema" paragraph, §4 "Root-dir inference ambiguity"
  - Why this matters: if the resolution algorithm is wrong or inconsistent with how `resolve_standard_dir` behaves elsewhere, entries could silently land in the wrong tree branch, or the "unknown root" fallback (open question 2) could swallow far more entries than expected.
  - Question for planner: confirm `adapter.resolve_standard_dir` (or an equivalent) supports desktop/documents before committing to it as the classification primitive, and write the exact algorithm into the story spec, not just "derived from."

- **H2** — §3 asserts old flat `group_key`s "continue to parse as 'root: unknown' without crashing or needing a backfill migration," but this is asserted, not verified against the actual current format. The research brief confirms today's real formats are `sort:<bucket>`, `reclaim:<category>`, `corral-screenshots` (no colon at all), and bare `None`. A parser expecting `sort:<root>:<bucket>` (3 segments) applied to `sort:<bucket>` (2 segments) needs an explicit "fewer segments than expected → treat as unknown-root" branch, and applied to `corral-screenshots` (0 colons) needs a separate branch entirely. The draft doesn't specify this parsing logic concretely enough to know it's actually safe.
  - Draft location: §3 "Schema" paragraph (backward-compatibility claim)
  - Why this matters: this is the single highest-risk line in the whole document — it's a claim about safely handling the user's real 6,800+-entry queue, asserted without a worked example.
  - Question for planner: write the actual parser (or at minimum, a table of "input format → parsed output") into the story spec before implementation starts, and add a test fixture built from a representative sample of the real flat-key formats research confirmed exist today.

- **H3** — Neither the draft nor the research brief confirms whether any existing code parses/splits `group_key` on `:` for a reason other than exact-match/display (e.g. a filter, a CLI arg, a report). The research brief's coverage of `group_key` consumers (`_bulk_target_ids`, `_group_entries`, `queue.html`'s badge rendering) all treat it as an opaque string for exact comparison or display — but this wasn't stated as an exhaustive audit, just what those specific functions do.
  - Draft location: §3, §5 ("Dependencies: None outside this repo")
  - Why this matters: if some other code path (e.g. a report script, a CLI filter flag) parses `group_key` by splitting on `:` and assumes a fixed segment count, adding a segment would silently break it with no test coverage catching it, since the research pass wasn't scoped to find such a consumer exhaustively.
  - Question for planner: add "grep the whole repo for `.group_key` usage and confirm every consumer treats it as opaque" as an explicit research/verification step in the implementing story, not an assumption carried into the design.

## Unresolved tensions

- **U1** — §2's epic-split recommendation frames epic #1 as small enough to "land in days," but §4's risk list (real-scale migration correctness, root-dir ambiguity, testing at 6,800-entry scale) describes exactly the kind of correctness-critical, hard-to-fully-test-in-advance work that historically has NOT been quick in this codebase (the research brief's own §9 notes pagination and background-jobs exist specifically because earlier "should be simple" assumptions broke at this queue's real scale). The draft doesn't reconcile "small/days" with its own named risks.
  - Draft location: §2 (epic-split table, "land in days") vs. §4 (risks)
  - Tension: optimistic scope-sizing vs. the draft's own documented correctness risks at real scale
  - Question for planner: either soften the "days" framing before presenting to the user, or explicitly scope the H/V slice plan so the schema-migration slice (the risky part) is isolated and can be validated against a real queue snapshot before the tree-UI slice (the low-risk part) is built on top of it.

## Convention violations

Clean — no findings. The draft explicitly preserves propose→queue→execute, preserves cap-as-pre-call-slice (not applicable to this epic but not contradicted), and follows the existing tolerant-backward-compatible-parsing posture `config.py`'s `load_config` already establishes.

## Posture mismatches

Clean — no findings. The recommendation to split into four sequential, independently-shippable epics matches this project's own established delivery posture (every prior epic in this repo shipped and merged independently rather than as one mega-epic).

## Notes

The draft's open questions (1–3) are genuine open questions correctly left for the user, not findings — they don't need grill treatment. Worth flagging to the planner as a meta-observation: open question 1 (whether to include Documents) is scope the user did not explicitly ask for; presenting it as a recommendation rather than a default is the right call and should stay that way when this is presented for review.

## Out of scope (this pass)

Grill does not propose solutions, score quality, gate work, or prioritize findings. Each finding above ends with a question for the planner; resolving H1/H2/H3/V1/U1 (or explicitly deferring them with rationale) is the next step before this draft is presented to the user.

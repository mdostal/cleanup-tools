# Grill record: chat-agent-plan-builder (round 1)

unresolved_count: 0 (all 4 findings resolved in design-discussion.md before presentation)
round_number: 1

Self-run adversarial pass (no separate `tpm` teammate spawned this session; solo-
orchestrator grill, consistent with how `guided-sort-and-cluster`'s round-1 grill was run
earlier this session).

## Hidden assumptions

**H1 — §2.5's "streaming reuse" claim is false as written.** `jobs.py`'s `JobState`
(confirmed by direct read) has exactly `status`/`current`/`total`/`result`/`error` —
`current`/`total` are integers for numeric progress, and `result` is only ever set once,
at the terminal `done` transition inside `_run()`'s success branch. There is no field a
running job can use to publish growing partial TEXT while `status == "running"`. §2.5
claims this "reuses jobs.py exactly" without flagging that `jobs.py` itself needs a small
generic extension first (e.g. a `partial: Any = None` field + a second callback the
target_fn can call to update it, mirroring `progress_callback`'s existing shape). Silent
reuse-as-written would just not work.

## Unresolved tensions

**T1 — `propose_moves`' path construction is asserted, not specified.** §2.1/§2.3 say a
chat-proposed entry gets "the same group_key scheme" and reuses protected-path checks,
but never states HOW `dest` is actually computed from a model-proposed `(src,
dest_bucket)` pair. `_stage_sort_plan` computes `dest` via `sort._plan`'s own rule-engine
bucket assignment; a model's proposed bucket is independent of that rule engine by
design (that's the point of asking a model instead of the static rules). The design
needs to state explicitly: `propose_moves` computes `location = _location_for_src(src,
config, adapter)` (reused, unmodified) and `dest = <resolved location root> /
sort.SORTED_SUBDIR / dest_bucket / Path(src).name` (the same `_sorted/<bucket>/`
convention every other pipeline uses) — not left as an implied "it just matches."

## Convention violations

**C1 — Settings location for the new turn-cap/model config is unspecified.** §2.6 says
"configurable in Settings" and "Settings-level... opt-in" without saying which pane or
what `Config` fields back it. Given this session's established pattern (every real
preference is a named `Config` field, e.g. `ui_mode`, `icon_choice`), this needs the same
treatment named explicitly, not left implicit — including which existing Settings pane
(if any) it extends, matching the sidebar-of-sections shell already built.

## Posture mismatches

**P1 — "BYOK" is used as a headline framing without saying what's new about it.** The
epic's own name references BYOK, but §2.1-2.8 never states plainly that BYOK here means
"reuse the existing `get_provider()`/credentials-file resolution exactly, zero new
credential-entry UI" — a reader could reasonably assume a new key-entry flow is in scope.
Given this project's stated posture of not building UI that doesn't need to exist yet,
this should be stated as an explicit non-goal, not left to inference.

## Vocabulary mismatches

None beyond T1/H1 above — "turn" is used consistently to mean one full user-message-to-
assistant-response cycle (which may include several internal tool round-trips), but this
was only ever implicit; recommend stating it as an explicit definition given how much of
§2.6's cost-control math depends on precisely what's being capped.

## Resolution

- H1 → §2.5 now specifies the exact `jobs.py` extension (`JobState.partial` +
  `partial_callback`) required before the polling-reuse claim is true.
- T1 → §2.3 now states `propose_moves`' path/group_key construction concretely
  (`_location_for_src` + `SORTED_SUBDIR` + the 3-segment scheme), not asserted.
- C1 → §2.6 now names the two `Config` fields and the exact settings pane they extend.
- P1 → §2.6 now states BYOK = existing credential resolution reused unchanged, no new
  credential-entry UI, as an explicit non-goal.
- Vocabulary → §2.5 now ends with an explicit "turn" definition.

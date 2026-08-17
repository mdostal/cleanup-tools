# Grill record: photo-face-clustering (round 1)

unresolved_count: 0 (all 3 findings resolved in design-discussion.md before presentation)
round_number: 1

Self-run adversarial pass (no separate `tpm` teammate spawned this session; solo-
orchestrator grill, consistent with this session's four prior epics).

## Unresolved tensions

**T1 — Document-topic clusters and person clusters would land in the same flat
`_clusters/<slug>/` namespace, with no stated separation.** §5 reuses
`document-topic-clustering`'s exact `_clusters/<slug>/` dest convention for person
clusters too. A human browsing `_clusters/` after running BOTH epics would see
`cluster-1/`, `cluster-2/`, `person-1/`, `person-3/` mixed together with no visual grouping
by kind — confusing, and avoidable.

**T2 — §5 says group/multi-person photos are "indexed... so a future epic could revisit
them," without justifying why that processing cost is worth paying now for a capability
that doesn't exist yet.**

## Convention violations

**C1 — Reuse of the recursive-scan/protected-path/iCloud-guard walk from
`semantic/pipeline.py` is implied but never stated explicitly**, risking a second,
duplicated implementation of the exact same walk logic `document-topic-clustering` already
built, parameterized only by which extractor (text vs. face) runs per file.

## Posture mismatches

None — the dependency-weight tradeoff, offline-reliability audit, and multi-person-photo
scoping decision are all already stated as explicit, justified choices.

## Vocabulary mismatches

None beyond T1 above.

## Resolution

- T1 → §5 now specifies domain-separated subdirectories: `_clusters/by-topic/<slug>/` for
  document clusters, `_clusters/by-person/<slug>/` for person clusters — resolved at the
  `dest` construction level in each epic's own pipeline, zero ambiguity for a human
  browsing the result, and zero possibility of a topic slug and a person slug colliding
  even coincidentally.
- T2 → §5 now states the justification plainly: indexing (not staging) group photos costs
  the same per-face detection+embedding work regardless, and avoids a full re-scan when a
  later epic solves multi-membership tagging — explicit tradeoff, not assumed self-evident.
- C1 → §6 now states explicitly that the face pipeline reuses `semantic/pipeline.py`'s
  existing walk (recursive scan + protected-path + iCloud guard), parameterized by
  extractor, rather than a second parallel implementation.

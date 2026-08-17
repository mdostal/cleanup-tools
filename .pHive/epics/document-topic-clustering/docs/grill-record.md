# Grill record: document-topic-clustering (round 1)

unresolved_count: 0 (all 4 findings resolved in design-discussion.md before presentation)
round_number: 1

Self-run adversarial pass (no separate `tpm` teammate spawned this session; solo-
orchestrator grill, consistent with how this session's three prior epics' grill passes were
run).

## Hidden assumptions

**H1 — The scan scope (shallow vs. recursive) is never stated, and the default assumption
(shallow, mirroring `sort._plan`'s `max_depth=0`) would badly undercut the whole feature.**
`sort.py`'s existing scan is deliberately shallow (loose top-level Downloads clutter). But
documents worth topic-clustering overwhelmingly live NESTED in folders (`~/Documents/Taxes/
2023/`, `~/Documents/House Sale/`, ...), not loose at a location's top level. §2.2/§2.5
never say whether `semantic/pipeline.py`'s walk is shallow or recursive — silently
defaulting to shallow (the only precedent in this codebase) would make the feature
near-useless for its stated purpose.

## Unresolved tensions

**T1 — Recursive scanning breaks the flat `_clusters/<label>/<filename>` dest scheme's
implicit no-collision assumption.** `sort._plan`'s flat `_sorted/<bucket>/<filename>` dest
already accepts SOME collision risk, but it's scanning one directory level, one filename
namespace. Once §2.5's proposal is "scan recursively" (per H1's resolution), two files
named `notes.pdf` in different subfolders now collide in the SAME flat
`_clusters/<label>/` destination — undefined in the current draft.

**T2 — "the same capped-hash approach `build_plan_snapshot` uses" conflates two different
things: a change-detection cache KEY, and the actual TEXT fed to the embedder.**
`build_plan_snapshot`'s hash is explicitly documented as capped-prefix, staleness-signal-
only, "not a duplicate-detection claim." §2.2/§2.3 cite it as the precedent for
`semantic/index.py`'s cache key without saying whether `extract_text()`'s FULL output (not
capped) is what actually gets embedded. Left as written, a future implementer could
reasonably (and wrongly) truncate extracted text to the same 8MiB prefix before embedding,
which is both unnecessary (typical documents are nowhere near 8MiB of text) and would
silently degrade embedding quality for the rare long document.

## Convention violations

None beyond T2 above (which is really a vocabulary-precision issue wearing a convention
hat — the "reuse X" framing needs to specify WHICH property of X is being reused).

## Posture mismatches

None — the dependency-footprint, no-network-call, and iCloud-guard decisions are all
already stated as explicit, justified choices rather than silent defaults.

## Vocabulary mismatches

**V1 — "cluster-label" and "cluster-slug" are used interchangeably with no stated
relationship.** A human-readable label (whatever the word-frequency heuristic in §2.4
produces, e.g. "invoice") and a filesystem-safe path segment for the `dest` in §2.1/§2.5
(needs to survive being a directory name — no `/`, no leading dot, reasonable length) are
NOT necessarily the same string, but the draft never says how one derives from the other.

## Resolution

- H1 → §2.2/§2.5 now state the scan is recursive (matching where real documents actually
  live), bounded by the same protected-path/iCloud-placeholder guards as every file
  encountered, not just top-level ones.
- T1 → §2.5's dest construction now disambiguates on collision: if
  `<location>/_clusters/<label>/<filename>` already exists (or is already proposed by
  another entry in this same run), the destination gets the file's own relative-path-based
  disambiguator appended before the extension, mirroring how a human would resolve "same
  name" conflicts, and is surfaced via the entry's existing `dest_exists` signal at review
  time either way — never silently overwrites.
- T2 → §2.2/§2.3 now explicitly separate "content-hash as an incremental-reindex cache key
  (capped-prefix, staleness signal only, same contract as `build_plan_snapshot`)" from
  "the FULL `extract_text()` output is what gets embedded, never truncated to the hash's
  cap."
- V1 → §2.4 now states the slug is a deterministic filesystem-safe transform of the label
  (lowercase, non-alphanumeric runs collapsed to a single `-`, capped length), with the
  human-readable label kept separately for display (e.g. in the queue disclosure from
  §2.5) rather than only ever showing the slug.

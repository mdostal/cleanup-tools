# Vertical Slice Plan: AI-provider integration + approvals UI

**Input:** horizontal-plan.md + design-discussion.md + user-confirmed decisions + team H/V
review (caught two real issues, both fixed below: a diff-view requirement that didn't map to
real data, and an undersliced UI milestone given its own pre-exec wireframe escalation).

## 1. Slicing Strategy

```
STRATEGY:
  Total horizontal items: 5 layers, ~25 items total.
  Planned slices: 6 (was 5 — Slice 3 split per H/V review; see below)
  First slice goal: a standalone, concurrency-safe approval queue store — proven correct in
                     isolation before anything consumes it.
  Final slice goal: the full loop — AI proposes a bucket for an "other" file, it lands in the
                     queue, the UI shows it for review, approve executes it via --from-queue.

  Slicing rationale: mirrors the confirmed manual-UI-first/AI-second sequencing exactly. Slices
  1-3b deliver a complete, real, standalone manual approvals workflow (queue → CLI execution →
  UI) with zero AI involvement — genuinely shippable if the AI half slips to a follow-on epic,
  per the user's confirmed answer to open question 6. Slices 4-5 add AI on top without touching
  1-3b's code paths except at the one integration point horizontal-plan.md §3 identified.

  Split rationale (Slice 3 → 3a/3b): team H/V review caught that the original single Slice 3
  bundled dashboard, plan-trigger, single approve/reject/undo, thumbnails, bulk actions,
  keyboard shortcuts, and pagination into one commit — despite being the one layer its own
  reviewer (ui-designer lens) flagged for pre-exec wireframing specifically because its
  interaction model was the least settled thing in the whole epic. Every other slice is
  narrowly scoped to one capability; this one wasn't. Split into 3a (core CRUD: list, single
  approve/reject/undo, thumbnails — the minimum for "the UI works at all") and 3b (triage
  efficiency: bulk actions, keyboard shortcuts, pagination — the minimum for "the UI is fast to
  actually use," which is the whole point given screenshots/photos are this project's single
  biggest clutter category).
```

## 2. Vertical Slice Plan

```
## Slice 1: Approval Queue Store

WHAT WORKS AFTER THIS SLICE:
  A Python API (no CLI/UI yet) that reliably creates, reads, updates, and atomically persists
  queue entries, safe under concurrent access (two processes read-modify-write without losing
  updates), with staleness re-verification against the live filesystem.

LAYERS TOUCHED:
  Approval Queue Store:
    - QueueEntry schema, load_queue/save_queue (atomic, locked), staleness check

NOT YET:
  - CLI flags, UI, AI — nothing consumes this yet

VERIFIED BY:
  - pytest: schema round-trip, atomic-write-doesn't-corrupt-on-concurrent-access (simulate two
    processes via threads/multiprocessing racing a read-modify-write), staleness check catches
    a file that moved/changed/disappeared since the entry was created
  - Manual: none needed — pure library code, same as os-adapter's own first slice

COMMIT REPRESENTS: A concurrency-safe approval queue store, proven correct standalone

---

## Slice 2: CLI Queue Integration

WHAT WORKS AFTER THIS SLICE:
  Running `sort --from-queue` or `reclaim --from-queue` executes only the approved entries in
  the queue, re-verifying staleness first, recording outcomes — while `sort --go`/`reclaim --go`
  continue to work completely unchanged from the prior epic.

BUILDS ON: Slice 1 (queue store)

LAYERS TOUCHED:
  CLI Queue Integration:
    - --from-queue flag on both sort and reclaim, consuming Slice 1's staleness-checked
      execution path

NOT YET:
  - UI (queue entries are hand-built/fixture-based for this slice's own tests)
  - AI

VERIFIED BY:
  - pytest: --from-queue executes only approved entries, skips pending/rejected, records
    execution outcome in status_history, refuses a stale entry with a clear message
  - pytest: --go's existing behavior is byte-for-byte unchanged (re-run the prior epic's
    sort/reclaim test suites against this slice's code — zero regressions expected)
  - Manual: a hand-crafted queue file with a mix of approved/pending/rejected entries, run
    against a synthetic fixture directory, confirm only approved entries move/delete

COMMIT REPRESENTS: The queue is real — CLI can execute against it, existing --go untouched

---

## Slice 3a: Approvals UI — core CRUD + overview dashboard

WHAT WORKS AFTER THIS SLICE:
  Running `cleanup approve` starts a localhost-only Flask server, opens your browser, shows a
  **real overview dashboard** (post-sign-off addition, user-requested — DiskDrill-style
  bucket/category breakdown with sizes and counts, not just status counts) where you can trigger
  a sort/reclaim plan (deduped against already-pending entries for the same path) and review each
  entry one at a time — with image thumbnails — approving, rejecting, or undoing it. All writes
  to the Slice-1 queue, executable via Slice-2's --from-queue. Zero AI involvement, zero bulk/
  keyboard/pagination yet, but genuinely usable — just slow for a large plan.

BUILDS ON: Slice 1 (queue store) — corrected from the original plan's "Slice 1 + Slice 2";
  the UI only ever touches the queue directly, execution (Slice 2) is a separate CLI path the
  UI doesn't call into.

LAYERS TOUCHED:
  Approvals UI (partial):
    - Flask app skeleton, dashboard route (overview: grouped by bucket/category, sizes, counts
      — computed from existing queue-entry fields, no new data source), plan-trigger routes
      (with staging dedup), single approve/reject/undo routes, thumbnail generation/serving

NOT YET:
  - Bulk actions, keyboard shortcuts, pagination (Slice 3b), AI (Slices 4-5), any dedupe-style
    duplicate-vs-master comparison view (confirmed out of this epic entirely — revisit only if
    a future epic integrates `dedupe`)

VERIFIED BY:
  - pytest: route logic (approve/reject/undo update the queue correctly), plan-trigger dedup
    (staging the same plan twice doesn't duplicate pending entries), thumbnail generation for
    image files, dashboard grouping/sizing math (sums match the underlying queue entries)
  - Manual: the actual UI, run against a real or realistic fixture plan — confirm approve
    genuinely gates execution, confirm thumbnails render, confirm the dashboard's breakdown is
    readable and accurate against a realistic mixed-bucket plan, confirm the server only binds
    to 127.0.0.1 (verify via `lsof`/`netstat`, not just code review — a security property worth
    checking for real)

COMMIT REPRESENTS: A working, single-entry-at-a-time approvals UI with a real overview — usable
and legible, not yet fast for bulk triage

---

## Slice 3b: Approvals UI — triage efficiency (shippable-standalone milestone)

WHAT WORKS AFTER THIS SLICE:
  Everything from 3a, plus bulk actions (approve/reject a whole group in one action), keyboard
  shortcuts for fast single-entry triage, and pagination for large plans. This is the point
  where the manual approvals UI is actually fast to use against a real, messy Downloads folder
  — the shippable-standalone milestone the design discussion committed to, not 3a alone.

BUILDS ON: Slice 3a

LAYERS TOUCHED:
  Approvals UI (complete):
    - Bulk-approve/bulk-reject routes (group_key-scoped), keyboard shortcut handler, pagination

NOT YET:
  - AI-provider layer, AI-queue wiring, "propose with AI" action

VERIFIED BY:
  - pytest: bulk routes update every entry in a group_key correctly (and only that group),
    pagination boundaries
  - Manual: keyboard shortcuts actually work in a real browser session against a realistic
    (100+ entry) fixture plan — this is the concrete test of "is this actually fast to use,"
    which no automated test can fully answer

COMMIT REPRESENTS: A complete, working, AI-free approvals UI, fast enough for a real messy
folder — the epic's floor, not just a step toward the ceiling

---

## Slice 4: AI-Provider Layer

WHAT WORKS AFTER THIS SLICE:
  A standalone, tested AI-provider interface with one working implementation (Anthropic) that,
  given a filename, returns a typed proposal result — callable from a script/REPL, not yet
  wired into anything else.

BUILDS ON: nothing from slices 1-3b (genuinely independent — could be built in parallel with
  them, but sequenced after per the confirmed manual-first decision, and because reviewing an
  AI-provider security/cost design benefits from the queue's shape already being settled)

LAYERS TOUCHED:
  AI-Provider Layer:
    - AIProvider ABC, ProposalResult typed outcomes, AnthropicProvider, get_provider() factory,
      key storage (env var primary, credentials-file fallback), retry policy, call-volume cap

NOT YET:
  - Nothing consumes this yet (Slice 5 wires it in)

VERIFIED BY:
  - pytest: ALL tests use a mocked provider — no real Anthropic API calls in the automated
    suite (per the no-network-in-tests principle). Mock covers: success, auth-failure,
    rate-limited, timeout (and the one-retry-then-give-up policy), unparseable-response,
    call-volume-cap enforcement (refuses the (cap+1)th call before making it, not after)
  - Manual: one real call against the actual Anthropic API with a real (user-provided) key, to
    confirm the integration works end-to-end outside the mock — explicitly the one place in
    this epic's verification that touches the real network, and explicitly opt-in/manual, never
    part of the automated suite

COMMIT REPRESENTS: A working, tested, standalone AI-provider layer — not yet doing anything

---

## Slice 5: AI-Queue Wiring + "Propose with AI"

WHAT WORKS AFTER THIS SLICE:
  The full loop: triggering "propose with AI" (CLI command or UI button) runs Slice 4's
  Anthropic provider over a sort plan's "other"-bucketed files (respecting the call-volume cap),
  writes successful proposals into Slice 1's queue as pending/source=ai:anthropic with the
  proposed bucket, and Slice 3b's UI shows them for review exactly like manual entries — same
  approve/reject/undo/bulk/keyboard flow, no special-casing. This is the epic's ceiling.

BUILDS ON: Slice 1 (queue) + Slice 3b (UI, for the "propose with AI" action + displaying AI
  entries) + Slice 4 (provider)

LAYERS TOUCHED:
  AI-Queue Wiring:
    - The connector: read "other" entries from a sort plan, call Slice 4 per entry, write
      Slice-1 queue entries for successes, surface failures without creating queue entries

NOT YET:
  - Nothing — this is the epic's final slice

VERIFIED BY:
  - pytest: wiring logic with a mocked provider — successes become queue entries with the
    right source/bucket, failures are surfaced but create no entry, call-volume cap is
    respected end-to-end through the wiring layer
  - Manual: the real UI, real "other"-bucketed fixture files, real (opt-in) Anthropic call,
    confirm an AI proposal appears in the review queue identically to a manual one and that
    approving it actually executes via Slice 2's --from-queue path

COMMIT REPRESENTS: Epic-complete — the full AI-provider + approvals UI loop, end to end
```

## 3. Overlay Diagram

```
VERTICAL SLICE OVERLAY
────────────────────────────────────────────────────────────────────────────────────────────

              │ Slice 1  │ Slice 2   │ Slice 3a    │ Slice 3b    │ Slice 4    │ Slice 5       │
              │ (queue)  │ (CLI exec)│ (UI core)   │ (UI triage) │ (AI layer) │ (AI wiring)   │
──────────────┼──────────┼───────────┼─────────────┼─────────────┼────────────┼───────────────┤
Approval      │ schema,  │           │ (UI reads/  │             │            │ (wiring writes│
Queue Store   │ atomic+  │           │ writes it)  │             │            │ AI entries)   │
              │ lock     │           │             │             │            │               │
──────────────┼──────────┼───────────┼─────────────┼─────────────┼────────────┼───────────────┤
CLI Queue     │          │--from-    │             │             │            │               │
Integration   │          │ queue     │             │             │            │               │
──────────────┼──────────┼───────────┼─────────────┼─────────────┼────────────┼───────────────┤
Approvals UI  │          │           │ dashboard,  │ bulk, kbd,  │            │ "propose with │
(Flask)       │          │           │ single      │ pagination  │            │ AI" button    │
              │          │           │ approve/    │             │            │               │
              │          │           │ reject/undo │             │            │               │
──────────────┼──────────┼───────────┼─────────────┼─────────────┼────────────┼───────────────┤
AI-Provider   │          │           │             │             │ ABC +      │               │
Layer         │          │           │             │             │ Anthropic  │               │
──────────────┼──────────┼───────────┼─────────────┼─────────────┼────────────┼───────────────┤
AI-Queue      │          │           │             │             │            │ connector      │
Wiring        │          │           │             │             │            │               │
────────────────────────────────────────────────────────────────────────────────────────────

Slice 3b is the "shippable even if we stop here" milestone the design discussion committed to
(3a alone is working but not fast enough to be the real commitment).
```

## 4. Deferred Items

```
DEFERRED (not in current slice plan):
  - find/dedupe/corral-screenshots integration with the approval flow — future epic
  - A before/after diff/duplicate-vs-master-comparison view — confirmed out of this epic
    entirely (doesn't map to any real data `reclaim`/`sort` produce; that's `dedupe`'s domain).
    Explicitly noted here so it isn't lost: revisit once a future epic actually integrates
    `dedupe` and there's real duplicate-candidate data to build a comparison view against.
  - A second AI provider (Gemini, etc.) — interface shaped for it, not built
  - AI judgment on reclaim candidates — confirmed out of scope (open question 3)
  - Any authentication on the Flask UI — localhost-only binding is the security boundary for
    v1; revisit only if this ever needs to be reachable beyond the local machine (not planned)
  - A JS framework/build pipeline for the UI — plain HTML/vanilla JS is enough at this scope

RATIONALE: each is either explicitly out of this epic's confirmed scope (AI provider count,
reclaim AI judgment, diff view), or genuinely unneeded at "single local user, localhost-only"
scale (auth, JS framework) — safe to defer without blocking anything in slices 1-5.
```

## 5. Risk by Slice

```
RISK PER SLICE:
  Slice 1:  Medium — concurrency correctness (locking/atomicity) is genuinely new territory for
            this codebase; getting it wrong is subtle (a race might not show up in normal testing).
  Slice 2:  Low — thin CLI wrapper around Slice 1 + existing sort/reclaim internals; main risk is
            regressing --go, directly guarded by re-running the prior epic's test suite.
  Slice 3a: Medium — first UI this project has ever had; core CRUD is conceptually simple but
            the Flask app skeleton, thumbnail serving, the localhost-binding security property,
            and now the overview dashboard's grouping/sizing logic (added post-sign-off) are
            all genuinely new surface.
  Slice 3b: Medium — smaller in code volume than 3a but harder to verify well (keyboard/bulk UX
            quality is a manual-testing question an automated suite can't fully answer); this is
            the slice the pre-exec wireframe escalation exists for.
  Slice 4:  Medium — new kind of risk (network calls, secrets, cost) even though the code volume
            is modest; the manual (opt-in, non-automated) real-API-call verification step matters
            more here than in any other slice.
  Slice 5:  Low-Medium — mostly wiring between already-verified pieces; the main new risk is the
            call-volume cap actually holding under the wiring layer, not just in Slice 4 isolation.
```

## 6. Moldability Notes

- Slices 1-3b can ship as a complete epic on their own if Slice 4/5 (the AI half) needs to become
  a follow-on epic — this is the explicit, confirmed fallback the design discussion committed to,
  not a hoped-for outcome. 3a alone is a legitimate stopping point too, just a slower one.
- Slice 4 has no dependency on slices 1-3b at all and could technically be built in parallel with
  them — sequenced after only because the user confirmed manual-first, and because the queue's
  settled shape (Slice 1) gives Slice 4's design something concrete to target even though Slice 4
  itself doesn't touch the queue until Slice 5.
- If Slice 3a/3b's wireframing (pre-exec) surfaces a need to change the queue schema (e.g. a
  field the interaction model needs that Slice 1 didn't anticipate), that's an additive schema
  change, not a Slice 1 redo — same "extend, don't redesign" principle the prior epic's
  OS-adapter used.
- No slice can be dropped without dropping real, named scope — this is already the minimum slice
  count for 5 genuinely sequential layers (6 slices, since the UI layer split into 3a/3b) with a
  deliberate mid-epic shippable milestone (3b).

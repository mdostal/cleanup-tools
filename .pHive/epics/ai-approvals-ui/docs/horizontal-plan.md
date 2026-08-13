# Horizontal Planning Scan: AI-provider integration + approvals UI

**Input:** design-discussion.md (Large scope, all 6 open questions confirmed: Flask, Anthropic,
AI proposes buckets for "other" files only, manual-UI-first/AI-second sequencing, approval
queue as its own standalone store) + research-brief.md.

## 1. Layer Inventory

- **Approval Queue Store** — new. Standalone, locked, atomically-written store for
  pending/approved/rejected actions. Not a `Config` extension (concurrency race, resolved in
  design review).
- **CLI Queue Integration** — new. `sort --from-queue`/`reclaim --from-queue`, additive to
  existing `--go` (unchanged).
- **Approvals UI (Flask)** — new. Localhost-only web server, review/approve/reject interface
  with bulk actions, keyboard shortcuts, undo, pagination, thumbnails, diff view.
- **AI-Provider Layer** — new. Abstract interface (error taxonomy, retry policy, key storage,
  call-volume cap) + one concrete Anthropic implementation, scoped to proposing a bucket for
  `"other"`-bucketed files.
- **AI-Queue Wiring** — new. The thin connector that runs the AI-provider layer over a `sort`
  plan's `"other"` entries and writes proposals into the queue with `source: ai:anthropic`.

`find`/`dedupe`/`corral-screenshots` integration, multi-provider support beyond the interface,
and the "recurring keep-clean" trigger are explicitly out of this epic's layer inventory.

## 2. Per-Layer Requirements

```
## Layer: Approval Queue Store

SCHEMA NEEDED:
  - QueueEntry: {id, action (move|delete), src, dest (for move), status (pending|approved|
    rejected), source (manual|ai:<provider>), status_history (list of {status, timestamp} —
    for undo), group_key (optional — for bulk operations), created_at, plan_snapshot (the
    src/dest/bucket/size the entry was created from, for staleness comparison at execution time)
  - Store location: ~/.config/cleanup-tools/approval_queue.yaml (separate file from config.yaml)

I/O OPERATIONS NEEDED:
  - load_queue(adapter) -> list[QueueEntry] (empty list if file absent)
  - save_queue(adapter, entries) -> None — ATOMIC (temp file + os.replace), not a direct
    write_file overwrite like config.py's save_config
  - append/update operations that acquire an advisory lock (fcntl.flock) around the full
    read-modify-write cycle, so a UI request and a concurrent CLI invocation can't interleave
  - staleness check: given a QueueEntry, re-verify its plan_snapshot against the live
    filesystem (existence, size) immediately before execution

NOT NEEDED (v1):
  - Multi-user / multi-machine sync (single local user, per north_star.expected_scale)
  - A database — YAML + file locking is enough at this scale

---

## Layer: CLI Queue Integration

FLAGS NEEDED:
  - sort --from-queue — execute only queue entries with status=approved whose action=move and
    whose src falls under the sort target dir; does NOT replace --go, which is untouched
  - reclaim --from-queue — same pattern for action=delete entries
  - Both re-verify staleness (per Approval Queue Store layer) before executing, and update
    status_history to record execution outcome (executed / failed / stale-skipped)

NOT NEEDED (v1):
  - Any change to sort.run/reclaim.run's existing plan-computation logic — --from-queue is a
    new, separate execution path, not a modification of the existing --go path

---

## Layer: Approvals UI (Flask)

ROUTES NEEDED:
  - GET / — dashboard: a real overview (post-sign-off addition, DiskDrill-style — user request),
    not just status counts: bucket/category breakdown grouped from current queue entries, with
    sizes and counts per group, plus per-status counts and links to trigger a plan. Grouping/
    sizing is computed from data the queue already has (`group_key`, `plan_snapshot.size`) — no
    new data source, just a richer view over it.
  - GET /plan/sort, GET /plan/reclaim — trigger a fresh plan computation, stage entries into
    the queue as status=pending, source=manual. Deduped against already-pending entries for the
    same src path (a team H/V review caught that repeated hits would otherwise re-stage
    duplicates on every page load) — matching the same-spirit dedup `reclaim`'s own plan-building
    already does within a single run, just applied across staging calls over time here.
  - POST /queue/<id>/approve, POST /queue/<id>/reject — single-entry actions
  - POST /queue/bulk-approve, POST /queue/bulk-reject — group_key-scoped bulk actions
  - POST /queue/<id>/undo — revert to the previous status_history entry
  - GET /thumbnail/<id> — serve a resized image thumbnail for image-type queue entries (never
    serve the original full file over HTTP — resize server-side, localhost-only regardless, but
    still worth not piping arbitrary file bytes through an HTTP response unnecessarily)

UI ELEMENTS NEEDED:
  - Queue list view, paginated, filterable by bucket/category/status
  - Per-entry card: thumbnail (images), src/dest paths, bucket, approve/reject/undo buttons
  - Bulk-select + bulk-action bar
  - Keyboard shortcut handler (y/n/space/arrows, per ui-designer review)

NOT NEEDED (v1, corrected after H/V review): a before/after diff/duplicate-comparison view —
  doesn't map to any real data this epic's commands produce (that's `dedupe`'s domain, out of
  scope). Dropped from this epic entirely, not deferred within it.

SERVER NEEDED:
  - Flask app, `cleanup approve` CLI command starts it bound to 127.0.0.1 (never 0.0.0.0) and
    opens the default browser to it
  - Synchronous request handlers (no async) — AI-provider calls triggered from the UI (see
    AI-Queue Wiring layer) run as plain blocking calls in the request handler; acceptable given
    single local user, no concurrent request load

NOT NEEDED (v1):
  - Any authentication (single local user, localhost-only — the network boundary IS the auth
    boundary for this tool)
  - A JS framework/build step — plain HTML + minimal vanilla JS is enough for this scope

---

## Layer: AI-Provider Layer

INTERFACE NEEDED:
  - AIProvider ABC: propose_bucket(filename, file_metadata) -> ProposalResult
  - ProposalResult: typed outcome — Success(bucket, confidence, rationale) | AuthFailure |
    RateLimited | Timeout | UnparseableResponse — not a raw exception passed to callers
  - get_provider() factory, reading provider selection + API key from environment (primary) or
    a dedicated ~/.config/cleanup-tools/credentials file (0600 permissions, fallback) — NEVER
    from config.yaml or the approval queue file

IMPLEMENTATION NEEDED (v1):
  - AnthropicProvider — one concrete implementation, calling the Anthropic API with the
    ambiguous file's name/extension/size as context, asking for a bucket + confidence +
    rationale
  - Retry policy: at most 1 retry, only on Timeout/RateLimited, never more
  - Call-volume cap: configurable max calls per invocation (default small, e.g. 20), enforced
    before any calls are made, not silently truncated after

NOT NEEDED (v1):
  - A second provider (Gemini etc.) — interface shaped for it, not built
  - Any AI judgment on reclaim candidates (out of scope per confirmed open question 3)
  - Streaming responses — a single request/response per file is enough at this scope

---

## Layer: AI-Queue Wiring

OPERATIONS NEEDED:
  - A CLI command or UI action ("propose with AI") that: takes a sort plan's `other`-bucketed
    entries, calls the AI-provider layer per entry (respecting the call-volume cap), and writes
    successful proposals into the approval queue as pending, source=ai:anthropic — using the
    proposed bucket, not "other"
  - Failed proposals (any ProposalResult other than Success) are surfaced to the user (in the
    UI or CLI output) but do NOT create a queue entry — nothing pending for a file the AI
    couldn't classify
```

## 3. Cross-Layer Dependencies

```
DEPENDENCIES:

CLI Queue Integration → Approval Queue Store (needs load/save/staleness-check to exist)
Approvals UI → Approval Queue Store (reads/writes queue state for every route)
Approvals UI → sort.run/reclaim.run (triggers plan computation, from the prior epic, unchanged)
AI-Queue Wiring → Approval Queue Store (writes proposals into it)
AI-Queue Wiring → AI-Provider Layer (calls propose_bucket per ambiguous file)
AI-Queue Wiring → sort.run (reads the "other"-bucketed subset of a computed plan)
Approvals UI → AI-Queue Wiring (the "propose with AI" UI action triggers it) — this is the ONE
  point where the UI layer and the AI layer touch; everything else about the UI works identically
  whether or not the AI layer exists, which is exactly what the manual-first sequencing needs.
```

The Approval Queue Store is the one layer every other layer depends on — same "shared
foundation" role the OS-adapter/config-schema played last epic, now explicitly NOT the existing
`Config`, per the concurrency fix.

## 4. Layer Map Diagram

```
HORIZONTAL LAYER MAP
─────────────────────────────────────────────────────────────────────────

Approval Queue │ schema (entry +   │ atomic save +   │ staleness       │
Store           │ status_history)   │ file lock       │ re-check        │
────────────────┼───────────────────┼─────────────────┼──────────────────┤
CLI Queue       │ sort --from-queue │ reclaim         │ status_history   │
Integration     │                   │ --from-queue    │ execution record │
────────────────┼───────────────────┼─────────────────┼──────────────────┤
Approvals UI    │ dashboard +       │ approve/reject/ │ bulk + keyboard  │
(Flask)         │ plan-trigger      │ undo routes     │ + thumbnails     │
────────────────┼───────────────────┼─────────────────┼──────────────────┤
AI-Provider     │ ABC + typed       │ AnthropicProvider│ retry + call-cap│
Layer           │ ProposalResult    │                 │ + key storage    │
────────────────┼───────────────────┼─────────────────┼──────────────────┤
AI-Queue        │ "other"-entries → │ proposal →       │                  │
Wiring          │ AI calls          │ queue write      │                  │
─────────────────────────────────────────────────────────────────────────
```

## 5. Scope Summary

```
HORIZONTAL SCOPE:
  Layers affected: 5 (Approval Queue Store, CLI Queue Integration, Approvals UI, AI-Provider
    Layer, AI-Queue Wiring) — all 5 are new; none are "extends existing" (the queue moved out
    of Config during design review).
  Total items: ~7 queue-store operations, 2 CLI flags, ~8 UI routes + their templates/JS,
    ~5 AI-provider interface pieces + 1 concrete implementation, 1 wiring connector.
  New vs modified: entirely new code; the only "modified" surface is sort.py/reclaim.py gaining
    a --from-queue flag alongside their existing, untouched --go path.
  Estimated total effort: large.

  LARGEST LAYER: Approvals UI (most routes, most interaction-model surface, the one with its
    own pre-exec wireframing escalation).
  RISKIEST LAYER: AI-Provider Layer (network calls, security/cost/retry surface this codebase
    has never had before) and Approval Queue Store (concurrency correctness) are tied for
    riskiest — both are new KINDS of risk for this project, not just new code.
```

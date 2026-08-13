# Structured Outline: AI-provider integration + approvals UI

## Part 1: Executive Summary

We're adding two things on top of the working `cleanup survey`/`sort`/`reclaim` CLI: an approval
queue + web UI where you review proposed file moves/deletes before anything happens, and a
pluggable AI-provider layer (Anthropic first) that proposes buckets for files `sort` currently
dumps in `"other"`. Both are named in `project-profile.yaml`'s `north_star`; both were greenfield
going in — no existing pattern in this codebase for either.

Your feedback confirmed five of six original open questions as proposed (Flask over
FastAPI/Textual/native desktop; Anthropic first; AI scoped narrowly to `"other"`-bucket
proposals; manual-UI-first/AI-second sequencing, explicitly accepting the AI half might land in
a follow-on epic) and left the sixth (approval mechanics) resolved by team review rather than
your input — team review caught that my original plan to extend `Config` with approval state
would create a real concurrency bug once a UI process and CLI both touch it, so the approval
queue became its own standalone, locked, atomically-written store instead.

Two further corrections came from the H/V review gate: a "diff view for judging duplicates"
requirement in the original UI design didn't map to any real data this epic's commands produce
(that's `dedupe`'s domain, out of scope) and was dropped entirely — noted explicitly for
revisiting once a future epic integrates `dedupe`; and the UI slice was split in two (core CRUD,
then triage-efficiency: bulk/keyboard/pagination) because it was the one layer flagged for
pre-exec wireframing and the original single slice didn't respect that. **At sign-off, one more
scope addition**: the user asked for a DiskDrill-style visual overview dashboard (bucket/category
breakdown with sizes and counts) as part of this epic's floor rather than a later polish pass —
folded into Phase 3a's `GET /` route below, using data the queue already carries.

**Implementation strategy in five sentences:** Build the approval queue as an isolated,
concurrency-safe store first — nothing else can be correct if this isn't. Wire CLI execution
against it before any UI exists, so the queue is provably real before it has a face. Build the
manual approvals UI in two slices (works, then fast), which alone is real shippable value even
if AI never lands. Build the AI-provider interface as a fully separate, independently-testable
component with its own security/retry/cost model — not a network-flavored copy of the OS-adapter
pattern. Wire AI proposals into the same queue last, so the UI never needs to know or care
whether a pending entry came from a human or a model.

```
PRODUCT GOALS (optional):
  Success metrics: none formally tracked (solo project, no metrics concern configured at
    kickoff) — qualitative goal is "I actually use this to clear my real Downloads/Desktop
    faster than doing it by hand."
  Non-goals: dedupe/find-wallets/corral-screenshots integration; a second AI provider; AI
    judgment on reclaim candidates; any authentication (localhost-only IS the auth boundary);
    a JS build pipeline.
  Stakeholders: none beyond the author — solo project, no team sign-off needed beyond this
    document's own gate.
```

## Part 2: Detailed Approach

### Phase 1: Approval Queue Store

**Goal:** A standalone, concurrency-safe store for pending/approved/rejected actions, proven
correct before anything depends on it.
**Depends on:** Nothing (first phase).

#### Changes

1. **`src/cleanup_tools/adapters/base.py`** (modify — team review caught this was missing from
   the original plan, which claimed `queue.py` would reuse `config.py`'s adapter I/O boundary
   without actually adding the capability that claim requires. `OSAdapter.write_file` today is a
   plain `Path.write_text` overwrite — no atomicity, no locking. Adding these as adapter
   primitives keeps the adapter as the single I/O boundary, the same additive-extension pattern
   the prior epic used for `list_subdirs`/`find_dirs`/`dir_size_bytes` when a real gap was found
   mid-implementation):
   - `write_file_atomic(path, content) -> None` — write to a temp file in the same directory,
     then `os.replace(temp, path)`. Concrete method on `OSAdapter`, shared (not per-platform —
     `os.replace` is atomic on both macOS and Linux).
   - `file_lock(path)` context manager — `fcntl.flock` (exclusive, blocking with a timeout,
     e.g. 5s, raising `TimeoutError` rather than hanging forever). Shared, POSIX-only (acceptable
     — this project targets macOS/Arch, both POSIX; `fcntl` is stdlib, no new dependency).

2. **`src/cleanup_tools/queue.py`** (new)
   - `QueueEntry` dataclass: `id` (uuid4 hex string), `action` (`"move" | "delete"`), `src`
     (str path), `dest` (str path, empty for delete), `status`
     (`"pending" | "approved" | "rejected"`), `source` (`"manual" | "ai:<provider>"`),
     `status_history` (list of `{status, timestamp}`, append-only), `group_key` (str, optional —
     e.g. the bucket name, for bulk operations), `created_at` (ISO 8601 str), `plan_snapshot`
     (dict — the size/mtime the entry was created from, for staleness comparison).
   - `load_queue(adapter, path=None) -> list[QueueEntry]` — mirrors `config.load_config`'s
     shape (default path via `adapter.resolve_home() / ".config/cleanup-tools/approval_queue.
     yaml"`, returns `[]` if absent). Uses `adapter.read_file` (existing method — reads don't
     need atomicity, only writes do).
   - `save_queue(adapter, entries, path=None) -> None` — calls `adapter.write_file_atomic`
     (new method from step 1) instead of `adapter.write_file`.
   - `with_queue_lock(adapter, path)` — thin wrapper around `adapter.file_lock` (new method from
     step 1), around the full read-modify-write cycle any mutating operation performs. Every
     mutating helper (`stage_entries`, `set_status`, below) uses this internally — callers never
     manage the lock themselves.
   - `stage_entries(adapter, new_entries: list[QueueEntry]) -> list[QueueEntry]` — appends,
     but **dedupes against existing pending entries with the same `src`** (the H/V review
     finding — repeated staging shouldn't create duplicate pending entries for the same file).
     Returns the entries actually added (empty list if everything was a dupe).
   - `set_status(adapter, entry_id, new_status) -> QueueEntry` — updates status, appends to
     `status_history`, saves atomically under the lock. Raises a clear `KeyError`-style error if
     `entry_id` doesn't exist.
   - `undo(adapter, entry_id) -> QueueEntry` — pops the last `status_history` entry, reverts
     `status` to the one before it. Raises if there's nothing to undo (only one history entry).
   - `check_staleness(adapter, entry: QueueEntry) -> bool` — re-stats `entry.src` (existence +
     size), compares against `plan_snapshot`. Returns `True` if stale (changed or gone).

3. **`pyproject.toml`** — no new dependency (`fcntl`/`os.replace` are stdlib).

#### Interfaces

```python
def stage_entries(adapter: OSAdapter, new_entries: list[QueueEntry]) -> list[QueueEntry]: ...
def set_status(adapter: OSAdapter, entry_id: str, new_status: str) -> QueueEntry: ...
def undo(adapter: OSAdapter, entry_id: str) -> QueueEntry: ...
def check_staleness(adapter: OSAdapter, entry: QueueEntry) -> bool: ...
```
Error conditions: `set_status`/`undo` raise `ValueError` naming the missing `entry_id` (mirrors
`config.py`'s file-naming error convention). `with_queue_lock` raises `TimeoutError` if the lock
isn't acquired within the timeout, rather than hanging.

#### Validation

- `adapter.write_file_atomic`/`adapter.file_lock` unit tests (both concrete/shared, so one test
  each covers both `MacOSAdapter` and `ArchLinuxAdapter` — same pattern as the prior epic's
  other shared-method tests).
- Two threads racing `stage_entries` against the same queue file: confirm no entry is lost and
  no file corruption (parse the final file successfully).
- `check_staleness` correctly flags a file that was deleted, resized, or whose mtime changed
  since `plan_snapshot` was captured.
- Could silently break: a lock timeout that's too short for a slow disk, causing spurious
  `TimeoutError`s under normal use — tune empirically, default generous (5s).

---

### Phase 2: CLI Queue Integration

**Goal:** `sort --from-queue`/`reclaim --from-queue` execute only approved entries, with zero
change to existing `--go` behavior.
**Depends on:** Phase 1.

#### Changes

1. **`src/cleanup_tools/commands/sort.py`** (modify)
   - Add `--from-queue` handling in `run(adapter, args)`: when set, load the queue
     (`queue.load_queue`), filter to `status == "approved"` and `action == "move"` entries whose
     `src` falls under the resolved target dir, re-check staleness per entry (skip + record
     `"stale, re-plan"` if stale), execute via `adapter.move` with the SAME per-entry
     `try/except OSError` isolation `--go` already uses, call `queue.set_status` to record the
     outcome (`"executed"` — note: this needs a 4th status value or a separate outcome field;
     resolved below in Interfaces).
   - `--go` code path: **completely untouched** — verified by re-running the prior epic's
     `test_sort.py` unchanged against this phase's code.

2. **`src/cleanup_tools/commands/reclaim.py`** (modify) — same pattern, `action == "delete"`,
   **explicitly calling the existing private `_master_path_refusal` helper** before executing
   any queue-sourced delete — not a fresh re-implementation of that check. This is the epic's
   single highest-stakes correctness requirement (see Risk Registry #2) and reusing the
   already-hardened helper (fixed after a real bypass bug in the prior epic — canonicalization +
   case-insensitive matching) is both less work and categorically safer than writing new
   refusal logic for the second execution path.

3. **`src/cleanup_tools/cli.py`** (modify) — add `--from-queue` flag to both `sort` and
   `reclaim` subparsers.

#### Interfaces

Resolving the "4th status" gap noted above: `QueueEntry.status` stays 3-valued
(`pending`/`approved`/`rejected`); execution outcome is a separate field,
`QueueEntry.executed_at` (ISO 8601 str, `None` until executed) plus an `execution_error` field
(`str | None`). `status` answers "is this approved," the new fields answer "did it actually run
and how'd it go" — keeps the approval concept and the execution concept from being conflated in
one enum.

#### Validation

- `--from-queue` executes only `approved` entries (not `pending`, not `rejected`) — parametrized
  test over all three statuses.
- A stale entry is skipped with a clear outcome, not executed against wrong/missing data.
- Full prior-epic `test_sort.py`/`test_reclaim.py` suites pass unmodified against this phase's
  `sort.py`/`reclaim.py` — the concrete regression check for "`--go` is untouched."
- Could silently break: forgetting to apply the SAME master-paths-refusal check `reclaim --go`
  has to `reclaim --from-queue` — this must be re-verified explicitly, it's the highest-stakes
  invariant in the whole codebase and it would be easy to only wire it into one of the two
  execution paths.

---

### Phase 3a: Approvals UI — Core CRUD

**Goal:** A working (if slow-for-large-plans) browser-based approval interface.
**Depends on:** Phase 1 (queue). NOT Phase 2 — the UI never calls `--from-queue` directly, it
only reads/writes the queue; execution is a separate CLI invocation the user runs themselves
(or, per Phase 3b/5, that a future "run approved now" button could shell out to — not built in
this phase).

#### Changes

1. **`src/cleanup_tools/ui/__init__.py`, `src/cleanup_tools/ui/app.py`** (new) — Flask app
   factory. `create_app(adapter) -> Flask`. Binds to `127.0.0.1` only, never `0.0.0.0` — enforced
   in the CLI command (below), not just assumed in app config.
2. **`src/cleanup_tools/ui/routes.py`** (new)
   - `GET /` — dashboard: **overview breakdown** (post-sign-off addition — user asked for a
     DiskDrill-style visual dashboard as part of this epic's floor, not deferred). Groups
     current queue entries by `group_key`/bucket, computing entry count and total size per
     group from `plan_snapshot.size` (data the queue already carries — no new source), plus
     per-status counts and links to trigger a plan. This is a read/aggregate view only — it
     doesn't mutate the queue.
   - `GET /plan/sort`, `GET /plan/reclaim` — run `sort.run`/`reclaim.run` (dry-run, no `--go`),
     convert plan entries to `QueueEntry` objects, `queue.stage_entries` (dedup applied here).
   - `GET /queue` — list view, one entry per row/card, filterable by status via query param.
   - `POST /queue/<id>/approve`, `POST /queue/<id>/reject` — `queue.set_status`.
   - `POST /queue/<id>/undo` — `queue.undo`.
   - `GET /thumbnail/<id>` — for image-suffix `src` paths, generate + serve a resized thumbnail
     (Pillow — new dependency, or shell out to macOS `sips`/check for a Linux equivalent; lean
     Pillow for cross-platform consistency, avoiding another BSD/GNU-shaped divergence).
3. **`src/cleanup_tools/ui/templates/`** (new) — `dashboard.html`, `queue.html`, base layout.
   Plain HTML + minimal vanilla JS (fetch calls to the POST routes), no framework/build step.
4. **`src/cleanup_tools/cli.py`** (modify) — add an `approve` subcommand: starts the Flask app
   via `app.run(host="127.0.0.1", port=<default, e.g. 5000>)`, opens the default browser via
   `webbrowser.open(...)`.
5. **`pyproject.toml`** (modify) — add `Flask`, `Pillow` as runtime dependencies.

#### Interfaces

Route contracts (JSON in/out for the `POST` routes, HTML for `GET`):
```
POST /queue/<id>/approve  -> 200 {"id": ..., "status": "approved"} | 404 if id unknown
POST /queue/<id>/reject   -> 200 {"id": ..., "status": "rejected"} | 404
POST /queue/<id>/undo     -> 200 {"id": ..., "status": <reverted>} | 400 if nothing to undo
```

#### Validation

- Route logic tests (Flask test client, no real server binding needed in automated tests).
- Thumbnail generation for a real small test image, confirm resized output.
- Manual: start the real server, confirm via `lsof -i :5000` (or equivalent) that it's bound to
  `127.0.0.1` and NOT reachable from another machine on the same network — an actual network
  check, not just a code-review confirmation that the bind call looks right.
- Could silently break: staging the same plan twice creating duplicate entries if the dedup
  key (`src` path) isn't actually being checked correctly — direct regression target for a test.

---

### Phase 3b: Approvals UI — Triage Efficiency

**Goal:** The UI is fast enough to actually use against a real, large, messy folder.
**Depends on:** Phase 3a.

#### Changes

1. **`src/cleanup_tools/ui/routes.py`** (modify)
   - `POST /queue/bulk-approve`, `POST /queue/bulk-reject` — accept a list of ids OR a
     `group_key`, apply `queue.set_status` to every matching entry.
   - `GET /queue` (modify) — add `?page=`/`?per_page=` pagination.
2. **`src/cleanup_tools/ui/templates/queue.html`** (modify) — bulk-select checkboxes + action
   bar; pagination controls.
3. **`src/cleanup_tools/ui/static/keyboard.js`** (new) — `y`/`n`/`space` for approve/reject/
   select on the focused entry, arrow keys to move focus.

#### Interfaces

```
POST /queue/bulk-approve  body: {"ids": [...]} | {"group_key": "..."}  -> 200 {"updated": N}
```

#### Validation

- Bulk routes update every matching entry and ONLY matching entries (a group_key bulk-approve
  must not touch entries outside that group).
- Manual: keyboard shortcuts against a real 100+-entry fixture plan in an actual browser — the
  concrete test of "is this fast," which no automated test fully answers.
- Could silently break: a bulk action racing a concurrent single-entry action on the same entry
  (both mutating the queue) — Phase 1's locking should already cover this, but worth an explicit
  test at this layer too, not just trusting Phase 1's own tests transitively.

---

### Phase 4: AI-Provider Layer

**Goal:** A standalone, tested AI-provider interface + one implementation, not wired into
anything yet.
**Depends on:** Nothing from phases 1-3b (see Part 6 dependency map).

#### Changes

1. **`src/cleanup_tools/ai/__init__.py`, `src/cleanup_tools/ai/base.py`** (new)
   - `ProposalResult` — a tagged union (e.g. a dataclass with a `kind` field: `"success"` |
     `"auth_failure"` | `"rate_limited"` | `"timeout"` | `"unparseable"`, plus `bucket`/
     `confidence`/`rationale` fields populated only when `kind == "success"`).
   - `AIProvider` ABC: `propose_bucket(filename: str, metadata: dict) -> ProposalResult`
     (`metadata` carries extension, size — enough context for a proposal without reading file
     contents, keeping this fast and not requiring filesystem access from within the provider).
2. **`src/cleanup_tools/ai/anthropic_provider.py`** (new)
   - `AnthropicProvider(AIProvider)` — calls the Anthropic API (message asking for a bucket
     name + confidence + one-sentence rationale, given the filename/extension/size). Retry: at
     most 1 retry, only on `timeout`/`rate_limited` outcomes, implemented as a plain loop, not a
     backoff library (keeps the "at most one extra network call" guarantee legible in the code,
     not buried in a library's default policy).
3. **`src/cleanup_tools/ai/__init__.py`** — `get_provider(name: str = "anthropic") -> AIProvider`
   factory; reads the API key from `ANTHROPIC_API_KEY` env var first, falls back to
   `~/.config/cleanup-tools/credentials` (a small YAML/plain file, `0600` permissions, checked
   and enforced/corrected on read if the mode is wrong) — **never** `config.yaml` or the
   approval queue file.
4. **`pyproject.toml`** (modify) — add `anthropic` (the official SDK) as a runtime dependency.
   Explicit check during implementation: confirm the SDK's default client doesn't enable any
   telemetry/analytics by default (or disable it if it does) — this is the one place the "no
   ambient network" hard rule could be silently violated by a dependency's own defaults, per
   design-discussion §4's named risk.

#### Interfaces

```python
class ProposalResult:
    kind: Literal["success","auth_failure","rate_limited","timeout","unparseable"]
    bucket: str | None
    confidence: float | None
    rationale: str | None

class AIProvider(ABC):
    @abstractmethod
    def propose_bucket(self, filename: str, metadata: dict) -> ProposalResult: ...
```

#### Validation

- All automated tests use a mocked `AIProvider` — zero real network calls in the test suite.
  Cover every `ProposalResult.kind`, the one-retry policy (mock a timeout-then-success sequence
  AND a timeout-then-timeout sequence, confirm exactly 2 total attempts in the latter case, not
  3+).
- Manual, opt-in, real-key: one real call against the actual Anthropic API — the one place in
  this epic's verification that touches the real network, explicitly never automated.
- Could silently break: the SDK upgrading and changing its default telemetry behavior out from
  under this code — worth a comment at the client-construction site flagging this as a thing to
  re-check on any `anthropic` package version bump.

---

### Phase 5: AI-Queue Wiring + "Propose with AI"

**Goal:** The full loop, end to end.
**Depends on:** Phase 1 (queue), Phase 3b (UI, for the trigger action + review display), Phase 4
(provider).

#### Changes

1. **`src/cleanup_tools/ai/wiring.py`** (new)
   - `propose_for_other_bucket(adapter, config, provider, cap=20) -> dict` — runs `sort.run`
     (dry-run), filters to `bucket == "other"` entries, caps at `cap` files, calls
     `provider.propose_bucket` per file, builds `QueueEntry` objects for `kind == "success"`
     results (`source="ai:<provider name>"`, `bucket=<proposed>`), calls `queue.stage_entries`.
     Non-success results are collected into a `failures` list in the return dict, not silently
     dropped, not turned into queue entries.
2. **`src/cleanup_tools/ui/routes.py`** (modify) — `POST /propose-ai` route triggering the
   above, surfacing `failures` in the response/UI.
3. **`src/cleanup_tools/cli.py`** (modify) — optional: a `cleanup propose-ai` CLI command as a
   non-UI trigger too, for completeness/scriptability.

#### Interfaces

```python
def propose_for_other_bucket(
    adapter: OSAdapter, config: Config, provider: AIProvider, cap: int = 20
) -> dict:
    """Returns {"proposed": [QueueEntry, ...], "failures": [{"filename": ..., "kind": ...}]}"""
```

#### Validation

- Mocked-provider tests: successes become correctly-shaped queue entries, failures are surfaced
  without creating entries, the cap is enforced BEFORE the (cap+1)th call is made (assert call
  count, not just result count).
- Manual, opt-in: real UI, real fixture `"other"` files, real Anthropic call, confirm an AI
  proposal shows up in the review queue and behaves identically to a manual entry through
  approve → `--from-queue` execution.
- Could silently break: the cap being enforced on the RESULT list instead of the CALL count,
  which would mean a misbehaving provider that returns instantly could still be called far more
  than `cap` times before the check catches up — must be a pre-call gate, not a post-call filter.

## Part 3: Verification Plan

**Per-phase verification:** see each phase's "Validation" subsection above — this section adds
the coverage matrix and the explicit non-verification callout the template asks for.

```
Phase 1 verification:
  Automated: pytest — schema round-trip, concurrent-write race simulation, staleness detection
  Manual: none (pure library code)
  Tools: pytest, threading/multiprocessing (stdlib)

Phase 2 verification:
  Automated: pytest — --from-queue status filtering, staleness-skip, master-paths-refusal
             re-verification, full regression of prior-epic sort/reclaim suites
  Manual: hand-crafted mixed-status queue file against a synthetic fixture dir
  Tools: pytest

Phase 3a verification:
  Automated: pytest (Flask test client) — route logic, staging dedup, thumbnail generation
  Manual: real server, lsof/netstat bind check, real browser click-through
  Tools: pytest, Flask test client, lsof

Phase 3b verification:
  Automated: pytest — bulk-action scoping, pagination boundaries
  Manual: real 100+-entry fixture, real browser, keyboard shortcut usability check
  Tools: pytest

Phase 4 verification:
  Automated: pytest — every ProposalResult kind, retry-count exactness, mocked provider only
  Manual: ONE real Anthropic API call, opt-in, never automated
  Tools: pytest, unittest.mock

Phase 5 verification:
  Automated: pytest — mocked wiring, cap-enforced-pre-call, failure surfacing
  Manual: full real loop (opt-in AI call) through to --from-queue execution
  Tools: pytest, unittest.mock
```

**Verification coverage matrix:**

```
| Acceptance-shaped criterion                          | Test Type          | Tool    | Phase |
|-------------------------------------------------------|---------------------|---------|-------|
| Queue survives concurrent read-modify-write            | Concurrency test    | pytest  | 1     |
| Staleness re-check catches a changed/missing file      | Unit                | pytest  | 1     |
| --from-queue executes only approved entries            | Unit                | pytest  | 2     |
| --go behavior is byte-for-byte unchanged                | Regression          | pytest  | 2     |
| reclaim --from-queue still enforces master-paths refusal| Unit (critical)     | pytest  | 2     |
| UI approve/reject/undo mutate the queue correctly       | Route test          | pytest  | 3a    |
| Repeated plan-staging doesn't duplicate pending entries | Unit                | pytest  | 3a    |
| Server binds to 127.0.0.1 only                          | Manual (security)   | lsof    | 3a    |
| Bulk actions scoped correctly (only matching entries)   | Route test          | pytest  | 3b    |
| Keyboard shortcuts are actually usable                  | Manual              | browser | 3b    |
| Every ProposalResult kind handled                       | Unit                | pytest  | 4     |
| Retry policy is exactly one retry, never more            | Unit                | pytest  | 4     |
| Real Anthropic call succeeds end-to-end                  | Manual, opt-in      | -       | 4     |
| Call-volume cap enforced pre-call, not post-filter        | Unit                | pytest  | 5     |
| AI proposal reviews/executes identically to manual entry | Manual, opt-in      | browser | 5     |
```

**What's NOT being verified and why:**
- **AI judgment quality** (is the proposed bucket actually *good*) — a product-quality question,
  not a correctness test; no automated oracle exists for "is this the right bucket."
- **Arch Linux** — deferred, same as the prior epic ("we do arch another day"). Everything here
  is tested on macOS only until that follow-up happens.
- **Load/concurrency beyond two writers** — this is a single-local-user tool; the locking design
  is verified for "UI + one CLI invocation," not for many simultaneous writers, because that
  scenario doesn't occur in this tool's actual usage.
- **Multi-provider behavior** — only Anthropic exists; nothing to verify for providers that
  aren't built.

## Part 3b: Cross-Cutting Concerns

- **Error handling strategy:** every layer that can fail (queue I/O, filesystem execution, AI
  calls) uses typed outcomes or the established per-entry `try/except OSError` isolation from
  the prior epic — nothing propagates an uncaught exception up through a route handler to a
  raw 500 error with no explanation. Flask routes catch domain errors (`ValueError` from
  `queue.set_status` on an unknown id, etc.) and return a clear 4xx JSON body.
- **Migration plan:** none needed — no existing users, no existing queue-shaped data to migrate.
- **Rollback plan:** every phase is a separate commit on `feat/ai-approvals-ui`; if a later
  phase's approach turns out wrong, revert to the last-good phase's commit and re-plan from
  there. No production deployment to roll back — this is a local tool, "rollback" means
  `git revert` plus reinstalling the package.
- **Performance implications:** the AI-provider layer introduces real latency (network calls,
  seconds each) for the first time in this codebase — mitigated by the call-volume cap and by
  keeping AI calls synchronous-but-bounded rather than trying to parallelize them in v1 (a
  future optimization, not this epic's problem).
- **Documentation impact:** README.md's Status section needs updates at the end of Phase 3b
  (manual UI ships) and Phase 5 (AI loop ships) — same doc-check pattern the prior epic used
  per-story. `.pHive/CONTEXT.md` needs new glossary entries for "approval queue," "staleness
  check," and the AI-provider terms once they exist.
- **Security considerations:** the Flask server's `127.0.0.1`-only binding is the primary
  security boundary (Phase 3a); the AI-provider's credentials file needs `0600` permissions
  enforced, not just assumed (Phase 4); the AI SDK's own telemetry defaults need an explicit
  check (Phase 4) since that's the most likely way this epic could accidentally violate the
  no-ambient-network hard rule.

## Part 4: File Change Manifest

```
FILES:

CREATE:
  - src/cleanup_tools/queue.py — approval queue store (schema, atomic I/O, locking, staleness)
  - tests/test_queue.py — tests for above
  - src/cleanup_tools/ui/__init__.py — UI package marker
  - src/cleanup_tools/ui/app.py — Flask app factory
  - src/cleanup_tools/ui/routes.py — all UI routes (Phase 3a + 3b + 5's /propose-ai)
  - src/cleanup_tools/ui/templates/base.html, dashboard.html, queue.html — HTML templates
  - src/cleanup_tools/ui/static/keyboard.js — keyboard shortcut handler (Phase 3b)
  - tests/test_ui_routes.py — Flask test-client route tests
  - src/cleanup_tools/ai/__init__.py — AI package marker + get_provider() factory
  - src/cleanup_tools/ai/base.py — AIProvider ABC, ProposalResult
  - src/cleanup_tools/ai/anthropic_provider.py — AnthropicProvider
  - tests/test_ai_provider.py — mocked-provider tests
  - src/cleanup_tools/ai/wiring.py — propose_for_other_bucket connector
  - tests/test_ai_wiring.py — mocked wiring tests

MODIFY:
  - src/cleanup_tools/adapters/base.py — add write_file_atomic() and file_lock() (new, shared
    concrete methods — queue.py's atomicity/locking needs, missing from the original plan,
    caught by team review)
  - src/cleanup_tools/commands/sort.py — add --from-queue execution path
  - src/cleanup_tools/commands/reclaim.py — add --from-queue execution path, explicitly reusing
    the existing private `_master_path_refusal` helper (not a re-implementation) so both
    execution paths share one refusal check
  - src/cleanup_tools/cli.py — add --from-queue flags, `approve` subcommand, optional
    `propose-ai` subcommand
  - tests/test_sort.py — add --from-queue test cases (existing --go tests untouched)
  - tests/test_reclaim.py — add --from-queue test cases (existing --go tests untouched)
  - tests/test_cli.py — add subcommand-parsing tests for the new commands/flags
  - pyproject.toml — add Flask, Pillow, anthropic as runtime dependencies
  - README.md — Status section updates (Phase 3b, Phase 5)
  - .pHive/CONTEXT.md — new glossary entries (approval queue, staleness check, AI provider terms)

DELETE:
  (none)

UNCHANGED (but affected):
  - src/cleanup_tools/config.py — read (not modified) for bucket_rules/master_paths; the
    approval queue deliberately does NOT extend this file
```

## Part 5: Risk Registry

| # | Risk | Severity | Likelihood | Mitigation | Owner |
|---|------|----------|------------|------------|-------|
| 1 | Queue file corruption or lost update under concurrent UI+CLI access | High | Medium | Atomic write (temp+`os.replace`) + `fcntl.flock` around every read-modify-write, explicit concurrency test (Phase 1) | Phase 1 |
| 2 | `reclaim --from-queue` ships without (or with a re-implemented, weaker) master-paths refusal check | High | **High** — this exact bug class already shipped once in the prior epic; its own post-run audit record names it verbatim "the most serious finding of the epic" (`.pHive/audits/post-run/harden-cleanup-cli-epic-close.yaml`) | Phase 2 explicitly reuses `reclaim.py`'s existing, already-hardened `_master_path_refusal` helper rather than writing new logic; test both execution paths against the same fixture master-path scenario | Phase 2 |
| 3 | AI SDK's default telemetry/analytics violates the no-ambient-network hard rule | High | Medium | Explicit check during Phase 4 implementation before the dependency is considered "done"; document the finding either way | Phase 4 |
| 4 | Call-volume cap enforced post-call instead of pre-call, allowing more real API calls than intended | Medium | Medium | Named explicitly in Phase 5's Validation section; test asserts call COUNT, not just result count | Phase 5 |
| 5 | API key ends up in a world-readable file or in `config.yaml`/the queue file (the wrong place) | High | Low | Dedicated credentials file, `0600` enforced on read, explicitly never `config.yaml` or `approval_queue.yaml` — named in Phase 4 | Phase 4 |
| 6 | Flask server accidentally reachable beyond localhost (binds to `0.0.0.0` or a misconfigured proxy) | High | Low | Explicit manual network check (`lsof`) in Phase 3a validation, not just code review | Phase 3a |
| 7 | UI interaction model (bulk/keyboard/pagination) ships unusable despite passing automated tests | Medium | Medium | Pre-exec wireframing (cycle-state escalation already recorded); Phase 3b's validation explicitly requires manual browser testing against a large fixture, not just route tests | Phase 3b |
| 8 | Approval-queue schema needs a field the UI wireframing surfaces late, after Phase 1 ships | Low | Medium | Documented as an acceptable additive schema change in vertical-plan.md's moldability notes — not a blocker, not a redo | Phase 1/3a |

**Detailed mitigation — Risk 1 (queue corruption, High severity):** Beyond the atomic-write +
lock design itself, Phase 1's test suite must include a genuine concurrency simulation (not just
sequential calls that happen to pass) — spin up real threads or subprocesses racing
`stage_entries`/`set_status` against the same file, and assert the final file is valid YAML with
no entries lost. This is the one test in this epic that, if weak or missing, could let a real
data-integrity bug ship silently.

**Detailed mitigation — Risk 2 (master-paths refusal gap, High/High):** This is the epic's
actual highest-priority correctness item, not Risk 1 — the prior epic shipped a real bypass of
this exact protection (relative paths, `..` segments, and case-insensitivity all defeated the
original check before it was fixed and re-verified against symlink indirection and boundary
cases). `_master_path_refusal` in `reclaim.py` is the hardened result of that process. Phase 2's
job is narrow and disciplined: call that existing helper from the new `--from-queue` path,
don't re-derive the logic. The test plan must run the SAME fixture scenarios that caught the
original bug (relative/`..` master paths, case-mismatched paths) against `--from-queue`
specifically, not just trust that reusing the helper is sufficient without re-confirming it.

**Detailed mitigation — Risk 3 (AI SDK telemetry, High severity):** Before Phase 4 is considered
complete, explicitly inspect the `anthropic` Python SDK's client construction for any
telemetry/analytics/update-check behavior enabled by default (read the SDK's own docs/source,
don't assume). If any exists, disable it explicitly in `get_provider()`'s client construction
and note the finding in the story's implementation notes so it's not silently re-introduced by a
future SDK upgrade.

## Part 6: Dependency Map

```
INTERNAL DEPENDENCIES:
  Phase 2 depends on Phase 1 (queue schema + execution-safe staleness check)
  Phase 3a depends on Phase 1 (queue read/write) — NOT Phase 2 (UI never calls --from-queue)
  Phase 3b depends on Phase 3a (extends its routes/templates)
  Phase 4 depends on nothing internal (genuinely independent, sequenced by user decision only)
  Phase 5 depends on Phase 1 (queue), Phase 3b (UI trigger + display), Phase 4 (provider)

EXTERNAL DEPENDENCIES:
  Library: Flask (unpinned minor, pin major) — the UI's web framework, synchronous per the
    confirmed design decision.
  Library: Pillow — thumbnail generation; cross-platform, avoids a BSD/GNU-shaped divergence
    shelling out to sips/ImageMagick would risk.
  Library: anthropic (official SDK) — Phase 4's provider implementation. If down/rate-limited:
    typed ProposalResult outcomes (rate_limited/timeout) surface this to the caller cleanly;
    the rest of the tool (survey/sort/reclaim/manual approvals) is entirely unaffected — the AI
    layer's failure mode is "no proposals today," never "the tool doesn't work."

BLOCKING QUESTIONS:
  (none remaining — all 6 open questions from design-discussion.md were resolved by user
  confirmation or team review before this outline was written)
```

## Part 7: Elicitation — Stress-Testing This Plan

#### Why Won't This Work?

1. **Failure:** The `fcntl.flock`-based locking doesn't actually prevent the race it's meant to
   prevent, because Flask's dev server (or a future production server) spawns multiple worker
   processes/threads that don't share the assumption correctly, or because a Flask request
   holds the lock across a slow operation (like waiting on user input mid-request, which
   shouldn't happen but could via a bug) and starves the CLI's own lock acquisition.
   **Trigger:** Running the Flask server with `threaded=True` or multiple workers without
   re-verifying the locking design still holds under that configuration.
   **Impact:** Either a deadlock (CLI hangs waiting for a lock the UI process holds indefinitely)
   or, worse, a race that the lock was supposed to prevent.
   **Signal:** Phase 1's concurrency test would need to specifically simulate this shape (a
   long-held lock from one caller, a waiting caller with a timeout) — if that test only checks
   "two quick sequential-ish writes don't corrupt," it wouldn't catch this.
   **Our answer:** Two separate mitigations for the two separate triggers named above — an
   earlier draft of this answer only covered the first and quietly dropped the second, which a
   review pass caught. (a) Multi-worker/multi-threaded Flask contention: run Flask's dev server
   single-threaded for v1 (appropriate for a single local user) — noted as a Phase 3a decision,
   not an oversight, so a future move to threaded/production serving is a deliberate re-check.
   (b) Cross-process lock-holding (UI process vs. the separate CLI process): this is the more
   important half, and thread count doesn't affect it at all. The mitigation is in Phase 1's own
   design — `with_queue_lock` wraps only the read-modify-write of the queue file itself, never
   the slow work that produces the data going into it (a `sort`/`reclaim` plan computation,
   which can take real time per the prior epic's own `dir_size_bytes`/`du` performance story,
   happens BEFORE `stage_entries` acquires the lock, not while holding it). As long as every
   caller follows that ordering — compute first, lock briefly to write, release — a slow plan
   computation never blocks the other process's lock acquisition. This ordering constraint
   needs to be an explicit code-review checklist item for every future change to `queue.py`'s
   callers, not just true by accident of the current implementation.

2. **Failure:** The Anthropic API's actual response format for "propose a bucket" doesn't match
   what `AnthropicProvider` expects to parse, and every real call returns `unparseable`.
   **Trigger:** The prompt design (not detailed in this outline — it's an implementation detail
   of Phase 4) produces free-form text instead of a structured response, and the parsing logic
   is too strict or too loose.
   **Impact:** The AI layer looks "wired up" (no crashes, clean `ProposalResult.kind ==
   "unparseable"` handling) but never actually produces a usable proposal — a silent, boring
   failure mode that could go unnoticed if the manual opt-in verification step is skipped.
   **Signal:** The Phase 4 manual, opt-in, real-API-call verification step is exactly the
   detection point — if that step is skipped "because the mocked tests all pass," this failure
   mode ships invisibly.
   **Our answer:** The manual real-call verification is explicitly required (not optional) before
   Phase 4 is considered done, precisely because mocked tests can't catch a real-format mismatch.

3. **Failure:** The approval queue grows unbounded (executed/rejected entries never pruned) and
   `load_queue`/`save_queue`'s full-file read-modify-write becomes slow or the file becomes
   unwieldy after months of real use.
   **Trigger:** No pruning/archival logic exists anywhere in this plan.
   **Impact:** Degraded performance over long-term use; not a correctness bug, a scale one, and
   this project's own `expected_scale` (single local user, not high volume) makes this a slow
   burn, not an acute risk.
   **Signal:** Would show up as the UI/CLI feeling sluggish after months, not during this epic's
   own development/testing window (fixture-sized queues won't reveal it).
   **Our answer:** Explicitly deferred — not fixed in this epic. Worth a one-line note in
   `README.md`'s eventual "known limitations" if one exists, but not a blocker for shipping v1
   given the realistic usage pattern (periodic cleanup sessions, not continuous high-volume use).

#### What Assumptions Are We Making?

- **VERIFIED** — `sort.run`/`reclaim.run`'s exact plan-dict shapes (researcher lens confirmed
  against the actual code, this outline's Phase 2/5 changes are built against those confirmed
  shapes).
- **VERIFIED** — no existing AI SDK/web framework/network dependency in this codebase
  (confirmed by grep, both in design-discussion research and again in team review).
- **ASSUMED** — Flask's synchronous request model is fine for AI-provider calls made from within
  a request handler (Phase 5's `/propose-ai` route). Reasonable because this is a single local
  user clicking one button at a time, not a concurrent-request-load scenario — explicitly the
  reasoning that ruled out FastAPI/async in the design discussion.
- **ASSUMED** — Pillow is an acceptable new dependency for thumbnail generation, staying inside
  the "dependency-minimal by design" principle the design discussion flagged as a risk to weigh
  choices against. Reasonable because it's a narrow, well-established library for exactly one
  job (image resizing), not a framework that pulls in a large dependency tree.
- **RISKY** — that a single `~/.config/cleanup-tools/credentials` file with `0600` permissions is
  "secure enough" for an API key on a personal machine. This is reasonable for the stated threat
  model (protecting against other users/processes on a personal single-user Mac, not protecting
  against a compromised machine) but would need real reconsideration if this project's
  `expected_scale`/`audience` ever changes from "single local user, own machine" — flagged in
  design-discussion §5 as a hard constraint tied to that exact scale assumption.
- **RISKY** — that "propose a bucket for `other`-bucketed files" is a well-enough-defined task
  for an LLM to do usefully without any fine-tuning or few-shot examples beyond filename/
  extension/size. If proposal quality turns out poor in practice (Part 3's "not verifying AI
  judgment quality" acknowledges this isn't tested), the AI half of this epic could ship
  technically-correct but practically-useless — a product-quality risk this outline can name but
  not resolve in advance.

#### What's the Simplest Version?

- **Must have:** Phases 1-3b (queue store, CLI execution, both UI slices) — this is the
  confirmed, standalone-shippable floor. Without all of 1-3b, there's no working approvals
  feature at all, manual or AI-assisted.
- **Should have:** Phase 4 (AI-provider layer) as a standalone, tested, but unwired component —
  even if Phase 5 doesn't land in this epic, a working, tested `AnthropicProvider` is real
  progress toward the north_star goal and de-risks a future epic that wires it in.
- **Could cut:** Phase 5 (AI-queue wiring) if the epic runs long — this is the explicit,
  user-confirmed fallback (open question 6). Cutting it doesn't waste Phase 4's work; it just
  means the "propose with AI" button doesn't exist yet, and Phase 4's tested interface sits ready
  for a follow-on epic to consume.
- **Could cut, more aggressively:** Phase 3b's pagination specifically, if the realistic plan
  sizes this tool produces turn out smaller than assumed (e.g., if `sort`'s `other` bucket and
  `reclaim`'s categories rarely exceed a few dozen entries in practice) — bulk actions and
  keyboard shortcuts matter more for triage speed than pagination does for a small list.

#### What Will We Wish We Had Thought Of?

- **Technical debt knowingly taken on:** the queue file will grow unbounded with no
  pruning/archival (named in elicitation finding 3 above) — acceptable now because this project's
  usage pattern is periodic, not continuous, so the practical impact is far in the future.
- **Edge cases deferred:** what happens if the SAME file is proposed by both a manual `GET
  /plan/sort` AND an AI `/propose-ai` call, with different buckets? The staging dedup (Phase 1)
  keys on `src` path alone — the second staging attempt would be silently dropped as a dupe,
  meaning "whichever proposal staged first wins" with no visible conflict resolution. Safe to
  defer because it's a rare case (same file, two sources, same session) with a benign outcome
  (nothing crashes, one proposal is silently preferred) rather than a data-loss risk.
- **Integration points not fully validated:** the interaction between `check_staleness` (Phase 1)
  and `--from-queue`'s execution (Phase 2) when the file changed in a way that's still "safe" to
  act on (e.g., only its mtime changed but content/size are identical) — the current design
  treats any mtime/size change as stale, which is conservative (safe) but might create
  false-positive "stale, re-plan" outcomes more often than strictly necessary. Acceptable
  because erring toward re-planning is the safe direction, matching this project's whole safety
  posture (dry-run by default, refuse rather than guess).
- **User workflows not fully considered:** running the approvals UI while a `sort --go` or
  `reclaim --go` is executing directly in another terminal at the same time — both would be
  touching the filesystem (not the queue) concurrently. This isn't a queue-corruption risk (only
  the queue file has locking; direct filesystem moves/deletes were never queue-mediated to begin
  with), but it means the UI's displayed plan could go stale mid-review if a concurrent direct
  `--go` run changes the same files — covered by the same staleness re-check at execution time,
  just worth naming explicitly as a workflow this design tolerates rather than prevents.

#### Where Are We Over-Engineering?

- **Abstractions with one consumer:** `AIProvider` as an ABC with a factory, for exactly one
  concrete implementation (`AnthropicProvider`) — this mirrors `OSAdapter`'s two-implementation
  precedent, but here there's only one implementation in this epic. Kept anyway because the
  north_star explicitly calls for "pluggable... bring-your-own provider," so building the
  abstraction now (even with one implementation) is the actual ask, not speculative generality —
  the same judgment call the prior epic made keeping `OSAdapter` abstract for a second platform
  that also wasn't fully built out (Arch's `find_installed_app` gap).
- **Error handling for unlikely scenarios:** the lock-timeout `TimeoutError` path (Phase 1) —
  under normal single-local-user operation, lock contention should be rare (one UI, occasional
  CLI invocations). Kept because the alternative (no timeout, a hang) is strictly worse and the
  cost of a timeout + clear error is low.
- **Configurability not requested:** the call-volume cap (Phase 5) defaults to a fixed number
  (20) rather than being exposed as a CLI flag or config field in this epic. This is deliberately
  NOT over-engineered — no configurability was requested, a sensible hardcoded default is enough
  for v1, and adding a config surface for it can happen later if it's ever actually needed.
- **Backward compatibility:** not applicable — no prior release of this epic's surfaces exists to
  be compatible with.

## Part 8: Decision Points for Sign-Off

```
DECISIONS REQUIRING SIGN-OFF:

1. [APPROACH] Approval queue as a standalone, locked, atomically-written store, NOT a Config
   extension — team review caught the concurrency race a shared file would create; this is a
   correction from my original design-discussion draft, not something you were asked about
   directly before.
   → Affirm / Change direction

2. [SCOPE] The "diff/before-after view for duplicates" is dropped entirely (doesn't map to real
   `reclaim` data; that's `dedupe`'s future-epic domain) — corrected during H/V review.
   → Affirm / Adjust scope

3. [SCOPE] Slice 3 split into 3a (core CRUD) and 3b (bulk/keyboard/pagination) — 3b, not 3a, is
   the actual "shippable even if AI never lands" commitment.
   → Affirm / Adjust scope

4. [RISK ACCEPTANCE] Flask dev server runs single-threaded for v1 (elicitation finding 1) —
   accepting that a future move to threaded/production serving needs its own re-check of the
   locking design, not assuming it "just works."
   → Accept / Require mitigation now

5. [RISK ACCEPTANCE] API key storage via env var + a 0600 credentials file is judged "secure
   enough" for this project's stated single-local-user threat model (elicitation's RISKY
   assumption) — not a general-purpose secrets-management solution.
   → Accept / Require mitigation now

6. [TRADE-OFF] AI proposal quality is explicitly not verified by this epic's test suite (Part 3)
   — only that the plumbing works, not that the AI's bucket suggestions are good. You'll find
   out proposal quality by actually using it, not from a green test suite.
   → Affirm / Reconsider
```

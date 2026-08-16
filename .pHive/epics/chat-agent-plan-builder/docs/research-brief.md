# Research brief: chat-agent-plan-builder

## What already exists (do not rebuild)

**Approval-queue primitives** (`src/cleanup_tools/queue.py`):
- `stage_entries(adapter, new_entries, path)` — the ONE landing point for any proposed
  action, manual or AI. Dedupes against pending `src`s. This is where the chat agent's
  proposals must land — as `QueueEntry` objects, not a parallel store.
- `set_status` / `undo` / `edit_entry` — per-entry status transitions and pre-approval
  edits, all under the queue file lock, all appending to `status_history`.
- `check_staleness` — content/size/mtime drift detection since a plan snapshot was taken.

**Bulk/prefix routes** (`src/cleanup_tools/ui/routes.py`):
- `_bulk_target_ids(candidates, group_key, entry_ids)` — a `group_key` ending in `":"` is
  a **prefix** match across every entry whose `group_key` starts with it (added this
  session for the tree/branch bulk-approve UI). This is the exact mechanism "approve this
  section of the plan" should reuse: a chat-proposed plan, once staged, is just entries
  sharing a group_key prefix — "approve this section" = the same `/queue/bulk-approve`
  POST the tree's "Approve all" button already makes.
- `parse_group_key(group_key)` → `{pipeline, location, bucket}` — defensive parser for
  every group_key shape in the codebase.
- `short_path()`, `bucket_icon()`, the `data-intent`/`data-confirm` button conventions,
  the shared `_status_icon.html` macro, and the `--text-*` type-scale tokens (all added
  this session) are the established visual language any new chat UI should reuse.

**AI provider plumbing** (`src/cleanup_tools/ai/`):
- `ai/base.py`'s `AIProvider` ABC + `ProposalResult` — single-call, forced-tool-choice
  shape (`propose_bucket(filename, metadata) -> ProposalResult`). Explicitly documents
  itself as "not a general chat interface" (line 4) — the chat agent is new surface, not
  an extension of this narrow interface.
- `ai/anthropic_provider.py`'s `AnthropicProvider` — one Messages API call per
  `propose_bucket()`, `tool_choice={"type": "tool", "name": "propose_bucket"}` (forces
  exactly one tool call, no conversation loop), `max_retries=0` + a hand-rolled
  ≤2-attempt retry (timeout/rate-limit only), `DEFAULT_MODEL = "claude-haiku-4-5"`,
  `DEFAULT_MAX_TOKENS = 300`. **Nothing here supports multi-turn state, streaming, or an
  open tool loop where the model chooses among several tools across turns** — a chat
  agent is a different shape, needs new code, not a parameter tweak.
- `ai/__init__.py`'s `get_provider()` / credential resolution (env var
  `ANTHROPIC_API_KEY` first, else `~/.config/cleanup-tools/credentials`, 0600-enforced)
  is BYOK today and should stay the single credential source — no new credential UI
  needed, just reuse `get_provider()`.
- `ai/wiring.py`'s `propose_for_other_bucket()` is the ONE existing call site: cap
  enforced by slicing candidates *before* any call (never post-hoc), `group_key=f"sort:
  {bucket}"` — note this is the OLD 2-segment group_key format, not this session's new
  location-aware 3-segment scheme (`sort:<location>:<bucket>`) — a real, pre-existing
  inconsistency worth fixing if this epic's stories touch AI-proposed entry staging.

**Async/background-work pattern** (`src/cleanup_tools/ui/jobs.py` +
`static/plan-trigger.js`): the project already solved "long-running work, single-
threaded dev server, no bundler" — a background `threading.Thread` reports progress into
an in-memory `JobState`, the client polls `GET /status/<job_id>` every 400ms
(`POLL_INTERVAL_MS`) until a terminal state. **This is directly reusable for the chat
agent's "streaming" response**: no Server-Sent Events needed. A chat turn can run as a
background job that appends to a growing partial-response string as the Anthropic
streaming API yields chunks; the client polls the same way it already polls plan-job
progress, and appends new text to the visible message. This keeps the chat interaction
entirely within the project's existing, proven, zero-bundler async pattern instead of
introducing a new transport.

**Config/mode system** (`src/cleanup_tools/config.py`, `Config.ui_mode`): three
interaction modes (Standard/Guided/Console) now exist as a real, config-persisted user
preference. A new chat surface should respect `ui_mode` where relevant (e.g. Console
mode's density conventions) rather than inventing a fourth parallel style system.

## What's stale and worth correcting as part of this epic

- `ai/base.py`'s docstring calls itself "the ONE sanctioned exception to this project's
  'no network, no telemetry' hard rule" (line 5-7) — that phrasing predates this
  session's 2026-08-14 product-owner correction (`.pHive/CONTEXT.md`: the real rule is no
  *ambient/telemetry* calls; opt-in/visibly-triggered features are fine). The update-
  checker (`src-tauri`) is already a second sanctioned network feature, and this epic
  will be a third. Worth a one-line doc fix wherever this epic's stories touch that file,
  not a blocking dependency.

## Constraints confirmed (not assumptions)

- Zero JS bundler/build step, confirmed repeatedly this session including through the
  full UI design-review pass. Any chat UI must be plain Jinja + a new vanilla
  `static/*.js` file, following the polling pattern above — not a frontend framework.
- This targets `src/cleanup_tools/` (the Flask UI), not `src-tauri/` directly. The
  Tauri shell wraps this UI as a webview; a chat response's polling loop runs inside
  that webview exactly like every other page today. No sidecar/IPC changes anticipated
  unless a story surfaces a real reason.
- `.pHive/research/semantic-desktop-organization.md` (epic #4, not yet planned) already
  recommends an embeddings/clustering architecture the chat agent could eventually query
  as a "tool" — but that subsystem does not exist yet. Any "query semantic clusters" tool
  this epic considers must be explicitly deferred/stubbed, not built against a
  nonexistent index.

## Open technical questions for the design discussion to resolve

1. Tool architecture: what specific tools does the in-app agent get, and how is each
   one scoped to avoid the agent re-scanning the whole filesystem/queue every turn?
2. Cost control: hard per-conversation cap (tokens? tool-calls? both?), user-visible
   usage indicator, or both — and what happens when the cap is hit mid-conversation?
3. Epic #3 absorption: does "user-controlled AI-sort scope" (other/everything/within-
   bucket, adjustable cap) become one of this agent's tools, or does it stay a separate,
   simpler batch feature? The original sequencing table flagged this as "likely absorbed,
   re-evaluate when planned" — this needs a real decision, not a default.
4. Prompt-injection surface: filenames and paths are attacker-influenceable strings
   (a user's own filesystem, but also potentially downloaded/shared files) that will be
   fed into agent context as tool results — what's actually at risk given the agent's
   tools are all read-plus-propose (never execute), and does anything need explicit
   hardening beyond "the agent can only ever *propose*, a human still approves"?
5. Scope size: is this one epic, or does the size (first chat/streaming surface, new
   tool architecture, cost control, UI) call for a sequential split?

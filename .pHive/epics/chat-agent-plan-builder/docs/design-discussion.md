# Design discussion: chat-agent-plan-builder

## 0. Prelude

Epic #2 in `guided-sort-and-cluster`'s sequencing table ("In-app chat/agent plan-builder,
BYOK, with tool-access to this epic's primitives"). Builds directly on `guided-sort-and-
cluster` and `settings-and-transparency` (both shipped this session): the approval-queue
primitives, the location-aware `group_key` scheme, the prefix-match bulk routes, and the
full UI design-review visual language (`short_path`, `data-intent` buttons, icon+text
status, the shared type scale, `ui_mode`).

No prior KG decisions or `north_star` block exist for this project outside this session's
own memory — nothing to reconcile against beyond `research-brief.md` above.

## 1. Goal

Let a user have a real conversation with an agent, inside the app, to figure out where
things should sort — not just click one "Propose with AI" button. The agent can look at
the actual queue/config state, propose moves, and the user approves what they like,
section by section, without leaving the chat.

## 2. Proposed approach

### 2.1 The write path is the existing queue — always

The single most important design decision: **the chat agent never executes anything.**
Its only "write" capability is `propose_moves`, a tool that calls the exact same
`queue.stage_entries()` every other pipeline in this app already uses. A chat-proposed
entry is a `QueueEntry` with `status="pending"`, `source="ai:chat"` (a new tag, distinct
from the existing single-shot `"ai:anthropic"` so the dashboard/History can still tell
"one classification call" apart from "a conversation"), and the SAME `group_key` scheme
`_stage_sort_plan` already uses (`sort:<location>:<bucket>`) — so a chat-proposed
"screenshots" entry lands in the identical dashboard tree branch a manually-planned one
would, with zero special-casing anywhere downstream. Nothing about approve/reject/undo/
bulk-approve/History needs to know or care that an entry came from a conversation.

This one decision is what keeps the whole epic's risk profile bounded: there is no new
"the agent did something to my files" failure mode to design against, because the agent
structurally cannot do that — only a human clicking Approve (in the queue, on the
dashboard tree, or in the new in-chat approve-these-proposals action, see §2.4) can.

### 2.2 Tools

Every tool is either **read-only** or **stage-only** (propose_moves). None execute,
delete, or touch `master_paths`/config directly.

| Tool | Returns | Why bounded |
|---|---|---|
| `list_locations` | configured `search_roots` (or fallback trio), short-labeled | Cheap, grounds the agent without scanning |
| `list_queue_summary(location?, bucket?)` | aggregate counts/sizes per bucket — the SAME shape `_group_entries_hierarchical` already produces | Never a per-entry dump; answers "what's out there" without spending context on filenames |
| `scan_location(location, subset?)` | a fresh dry-run plan's bucket counts (reuses `sort._plan`/`reclaim` internals) | Real, current data — never cached/stale — but summarized, not enumerated |
| `list_candidate_files(location, bucket, limit≤50)` | actual filenames in one bucket, capped | Only called when the agent (or user) needs specifics — keeps default turns cheap |
| `propose_moves(items: [{src, dest_bucket}])` | staged entry ids + a short summary | THE only write tool — see §2.1 and §2.3 for its two mandatory guards |

This directly answers the research brief's open question #1 ("how does a multi-turn
conversation stay grounded without re-scanning everything every turn"): `list_queue_summary`
and `scan_location` return aggregates, never full listings, so a turn's context cost is
flat regardless of queue size — the same "aggregates only" principle the dashboard tree
already committed to this session for the identical reason (real-scale performance).

A semantic-clustering tool (querying epic #4's not-yet-built embedding index) is
explicitly OUT of scope here — epic #4 doesn't exist yet. `list_candidate_files`/
`scan_location` are the closest thing to "specifics" this epic offers; a future
`query_semantic_clusters` tool slots in next to them once epic #4 ships, without needing
to touch this epic's engine.

### 2.3 propose_moves: path construction and two mandatory guards

`propose_moves` takes `{src, dest_bucket}` pairs the MODEL proposes — `dest_bucket` is
independent of `sort._plan`'s static rule engine by design (a model can be more
contextual than the fixed extension/filename rules; that's the reason to ask it at all).
Concretely, for each proposed pair:

1. `location = _location_for_src(src, config, adapter)` — the exact existing helper
   `_stage_sort_plan` already uses, unmodified.
2. `dest = Path(location) / sort_module.SORTED_SUBDIR / dest_bucket / Path(src).name` —
   the same `_sorted/<bucket>/` convention every pipeline already writes to, so a
   chat-proposed destination looks, on disk, identical to a rule-engine-proposed one.
3. `group_key = f"sort:{location}:{dest_bucket}"` — the current 3-segment scheme, built
   the same way `_stage_sort_plan` builds it, not a new namespace.

Then two mandatory guards, checked before `stage_entries` is ever called:

1. **Protected-path reuse.** Every candidate `src` (and the resolved `location`) is run
   through the SAME `_is_protected_path` check `_stage_sort_plan`/`_stage_reclaim_plan`
   already enforce. A model proposing to move something under `/System` (however that
   proposal arose) is refused structurally, exactly like every other staging path — no
   new hard-block mechanism, reuse of the existing one.
2. **Dest-bucket validation.** `dest_bucket` must match a plain bucket-name shape
   (`^[a-zA-Z0-9_-]+$`, no `/`, no `..`, no absolute paths) before step 2 above ever
   builds a path with it. Rejected server-side, not trusted from the model's tool-call
   arguments — mirrors `anthropic_provider._parse_response`'s existing discipline of
   never trusting a tool result's shape without validation.

### 2.4 In-chat approval

`propose_moves`' tool result includes the real `entry_id`s `stage_entries` returned. The
chat UI renders a compact inline list of what was just proposed (bucket, count, location
— using `short_path`/`bucket_icon`, the same visual language as everywhere else) with an
**Approve these N** button. That button POSTs the existing `/queue/bulk-approve` with an
explicit `entry_ids` list (the id-list bulk mechanism `_bulk_target_ids` already supports
today, not a new group_key scheme) — so "approve this section of the plan" maps onto the
existing per-entry-id bulk primitive exactly as it exists today. No new approval code
path. The user can also ignore the in-chat button entirely and approve later from the
Dashboard tree or Review Queue — the entries are real, ordinary pending `QueueEntry`s the
moment they're staged.

### 2.5 "Streaming" without Server-Sent Events

This project has zero JS bundler and already solved "long-running work, single-threaded
dev server" once: `jobs.py`'s background-thread-plus-polling pattern
(`start_job`/`get_job`, polled via `plan-trigger.js` every 400ms). A chat turn reuses
this *pattern*, but `jobs.py` as it stands today only carries numeric `current`/`total`
progress and a `result` that's set exactly once, at the terminal `done` transition — it
has no field a running job can use to publish *growing text* while still `"running"`.
This epic therefore includes a small, generic extension to `jobs.py` (not chat-specific):
add `partial: Any = None` to `JobState`, plus a second callback (`partial_callback`,
alongside the existing `progress_callback`) that `start_job`'s `target_fn` can call to
overwrite `partial` at any point before completion. `GET /status/<job_id>` starts
including `partial` in its JSON whenever present. This is a couple-line addition to an
already-generic module, reusable by anything else that wants incremental text later — not
a chat-only hack bolted on top.

With that extension, one chat turn is: `POST /chat/<id>/message` starts a background job
whose body runs the Anthropic tool-calling loop for that turn (potentially several tool
round-trips within one turn) using the SDK's streaming API internally, calling
`partial_callback` with the accumulated text as deltas arrive. The client polls exactly
the way any other background job is polled today and renders the growing `partial` text.
This is not real Server-Sent Events, and does not need to be — it's the same mechanism
this codebase already trusts for "long operation, incremental feedback, no bundler,"
generalized by one field instead of duplicated.

**Definition of "turn," used precisely from here on:** one full user-message-to-
assistant-response cycle — i.e. one `POST /chat/<id>/message` call and everything that
happens inside its background job (which may include several internal tool round-trips
before the model produces its final text) counts as exactly one turn. §2.6's turn cap
counts these, not individual tool calls.

### 2.6 Cost control

- **Per-conversation hard cap on assistant turns** (default 20, configurable in
  Settings). Checked BEFORE a new turn starts — mirrors `propose_for_other_bucket`'s
  own "slice before calling, never call-then-discard" cap discipline. The turn already
  in flight when the cap is reached is allowed to finish (never truncated mid-response);
  the input box then disables with an explicit "conversation limit reached, start a new
  one" message — never a silent, confusing cutoff.
- **Per-conversation cap on total files proposed** (default 500) — stops one runaway
  turn from staging an enormous plan with no review checkpoint.
- **Visible running usage indicator** in the chat UI ("Turn 4 of 20") — legible, not a
  hidden budget.
- **Cheap model by default.** The conversation defaults to the SAME
  `DEFAULT_MODEL = "claude-haiku-4-5"` `anthropic_provider.py` already uses for
  `propose_bucket`, with an explicit Settings-level (not per-message) opt-in to a
  stronger model — never a silent per-turn upgrade.

**Where this lives, concretely:** two new `Config` fields (`chat_turn_cap: int = 20`,
`chat_model: str = "claude-haiku-4-5"`), persisted exactly like `ui_mode`/`icon_choice`
(config-based, not localStorage — a real preference, not a cosmetic one). Surfaced as two
new fields on the existing Settings **AI Provider** pane (extending it, not adding a new
sidebar section — that pane already shows "configured Y/N", these are the two other
knobs an AI-touching feature needs).

**BYOK, stated plainly — a non-goal, not a gap.** "BYOK" here means this epic reuses
`ai/__init__.py`'s existing `get_provider()`/credential resolution (env var, else the
0600 credentials file) completely unchanged. There is no new credential-entry UI in this
epic — the AI Provider pane's existing read-only "configured via X" status is sufficient;
entering/rotating a key still happens exactly as it does today (env var or the
credentials file by hand). If that ever needs a real in-app entry form, it's a separate,
future story against the existing AI Provider pane, not part of this epic.

### 2.7 Epic #3 absorption — decided, not deferred

**Absorbed.** "User-controlled AI-sort scope" (other/everything/within-bucket, adjustable
cap) becomes conversation, not a fixed three-way UI selector: a user tells the agent
"just look at the leftover files" or "re-sort my whole Downloads folder" or "look inside
my photos bucket for anything miscategorized," and `scan_location`'s `subset` argument
scopes accordingly. The "adjustable cap" becomes §2.6's uniform turn/file caps, not a
separate per-run number field. Epic #3 does not need its own planning pass.

The existing single-shot `POST /propose-ai` / `cleanup propose-ai` path (today's
"classify the 'other' pile, one capped batch call, no conversation") is **not replaced**
— it stays exactly as-is for a user who wants the cheap one-shot behavior without opening
a conversation. The chat agent is additive, richer surface, not a migration.

### 2.8 Prompt-injection posture

Because every tool is read-only-or-stage-only (§2.1), the realistic worst case is "the
model proposes a strange bucket for a strangely-named file" — not code execution, not
data exfiltration, not an unreviewed filesystem change. Concrete hardening beyond that
structural bound:

- Tool results carry filenames/paths/counts only, never file contents — matches
  `propose_bucket`'s existing "never see the file's contents" contract exactly.
- The system prompt explicitly frames tool results as **data to classify, never
  instructions to follow** — the same discipline this session already applies to
  untrusted text encountered elsewhere (e.g. shared-artifact titles).
- §2.3's two guards (protected-path reuse, dest-bucket shape validation) are the actual
  backstop — enforced in code, not just prompted for.

## 3. Risks

- **Cost overrun from a pathological conversation.** Mitigated by §2.6's hard caps
  (checked before each turn, not after) and the visible usage indicator.
- **A chat-proposed entry silently drifting from the dashboard's existing group_key
  scheme**, re-introducing the exact inconsistency `ai/wiring.py`'s batch path already
  has (old 2-segment `sort:{bucket}` format, not the new 3-segment location-aware one).
  Mitigated by §2.1's explicit requirement to reuse the current 3-segment scheme, not
  invent a new one — and this is a good moment to fix `ai/wiring.py`'s existing old-format
  group_key too, since a story here touches the same staging code path anyway.
- **Protected-path bypass via a tool-proposed destination.** Mitigated by §2.3's reuse
  of the existing `_is_protected_path` check at the actual staging call site, not a new
  parallel check that could drift from the real one.
- **In-memory conversation state lost on process restart.** Deliberate, not a gap —
  conversations are ephemeral UI state (like background jobs), not durable data; the
  only durable output of a conversation is whatever got staged into `queue.yaml`, which
  survives independently. A restarted conversation just starts fresh, same as a lost
  background job today.

## 4. Dependencies

- Builds on `guided-sort-and-cluster` (group_key scheme, bulk prefix routes, protected
  paths) and `settings-and-transparency` (settings shell for the new turn-cap/model
  settings, `ui_mode`) — both already shipped this session.
- No dependency on epic #4 (semantic clustering) — explicitly deferred per §2.2.

## 5. Open questions — resolved above, restated for the review gate

1. Tool architecture — §2.2/§2.3.
2. Cost control — §2.6.
3. Epic #3 absorption — §2.7 (absorbed; old batch path stays).
4. Prompt injection — §2.8.
5. Scope size — Large (see §6).

## 6. Scale assessment

**Large.** First chat/streaming surface in the codebase, a new (if reused-pattern)
async architecture, a new tool-calling engine, real security-adjacent design work
(prompt injection, protected-path reuse), and a genuinely new UI surface. Proceeding to
H/V decomposition.

# Horizontal plan: chat-agent-plan-builder

## Layers

**1. Background-job infrastructure (extend, don't replace)**
`src/cleanup_tools/ui/jobs.py`: add `JobState.partial` + a `partial_callback` a
target_fn can call to publish growing text before the job reaches a terminal state.
Generic — not chat-specific — so `GET /status/<job_id>` gaining an optional `partial`
field is safe for every existing caller (`plan-trigger.js` never reads a field it
doesn't request).

**2. Conversation-state store (new, small, in-memory)**
A new `src/cleanup_tools/chat/state.py` (mirrors `jobs.py`'s own module shape: a
module-level dict + one lock, never persisted to disk). Holds per-conversation message
history (role + text, no tool-call internals need to survive a restart) and the running
turn count, keyed by a `conversation_id`. Lost on process restart — deliberate, see
design discussion §3.

**3. Engine (new)**
`src/cleanup_tools/chat/engine.py`: the Anthropic tool-calling loop for one turn —
takes the conversation's message history + a new user message, runs the model with the
tool set below (streaming, via `partial_callback`), executes whichever tools the model
calls (each a plain Python function against real adapter/config/queue state), feeds
results back to the model, repeats until the model produces a final text response with
no further tool calls, then returns `{text, staged_entry_ids}`.

**4. Tools (new)**
`src/cleanup_tools/chat/tools.py`: `list_locations`, `list_queue_summary`,
`scan_location`, `list_candidate_files` (all read-only), `propose_moves` (the one write
tool, per design discussion §2.3's two mandatory guards). Each tool is a plain function
the engine calls directly — no new IPC/network hop, these run in-process exactly like
every other command module this codebase already has.

**5. Routes + UI (new)**
`src/cleanup_tools/ui/routes.py`: `GET /chat` (page), `POST /chat/new`, `POST
/chat/<id>/message` (kicks off the turn as a background job via the now-extended
`jobs.start_job`), reuses `GET /status/<job_id>` unmodified (just gains `partial`).
New `templates/chat.html` + `static/chat.js` (polling, message rendering, in-chat
Approve-these-N action posting to the existing `/queue/bulk-approve` with an
`entry_ids` list). New nav link, matching History's precedent (a real page, gets
`.nav-link`/`aria-current`).

**6. Config + Settings (extend existing)**
Two new `Config` fields (`chat_turn_cap`, `chat_model`), surfaced on the existing AI
Provider settings pane — no new sidebar section.

## Cross-layer dependencies

Layer 1 (jobs.py extension) blocks layer 5 (routes need `partial` to exist).
Layer 2 (conversation state) and layer 3 (engine) are mutually dependent — the engine
reads/appends to conversation state every turn.
Layer 4 (tools) depends on layer 3 only for the calling convention (plain functions);
tools themselves depend on existing `queue.py`/`routes.py`/`config.py` primitives, not
on anything new in this epic.
Layer 6 (config fields) is needed by layer 3 (cap enforcement) and layer 5 (settings UI)
independently — no ordering constraint between them.

## Sequencing

1→2→3 (engine cannot run without both the extended job plumbing and somewhere to keep
conversation history), then 4 (tools plug into the now-working engine one at a time,
read-only first), then 5+6 together (UI and its two settings fields land once there's a
real engine+tools to point them at).

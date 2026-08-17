# Vertical plan: chat-agent-plan-builder

Four slices, each a genuinely working state. Sequential — each depends on the prior.

## Slice 1 — chat-engine-and-minimal-ui

The whole pipeline end-to-end with the smallest possible tool set: `jobs.py` extension,
conversation-state store, the tool-calling engine, ONE trivial read-only tool
(`list_locations`), `/chat` page + polling JS + nav link. **Working state:** open `/chat`,
ask "what locations are configured," get a real streamed answer built from actual config
data. Proves the async/polling architecture works before any of the higher-risk pieces
(write path, cost caps) are built on top of it.

## Slice 2 — chat-queue-and-filesystem-tools

Add `list_queue_summary`, `scan_location`, `list_candidate_files` — all read-only, all
returning aggregates/capped lists per design discussion §2.2. **Working state:** the
agent can meaningfully discuss the real queue and filesystem state ("you have 42 photos
and 8 pdfs pending in Downloads") without proposing anything yet — a strictly additive,
lower-risk slice than the write path.

## Slice 3 — chat-propose-and-approve

Add `propose_moves` (design discussion §2.3's path construction + two mandatory guards:
protected-path reuse, dest-bucket validation) and the in-chat "Approve these N" action
(entry-id bulk-approve, reusing the existing primitive). **Working state:** a full
conversation can propose a real plan and the user can approve it without leaving the
chat — the epic's actual headline capability.

## Slice 4 — chat-cost-control-and-settings

Turn/file caps (checked before each turn starts, graceful cutoff message), the visible
usage indicator, the two new `Config` fields on the AI Provider settings pane, and the
explicit epic-3-absorption documentation note. Also folds in the `ai/wiring.py` old-
format `group_key` fix flagged in the design discussion's risks (a good moment to fix it
since this slice touches the same staging convention). **Working state:** the feature is
safe to actually ship — bounded cost, visible usage, configurable — not just functionally
complete.

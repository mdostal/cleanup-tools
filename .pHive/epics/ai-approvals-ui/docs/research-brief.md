# Research Brief: AI-provider integration + approvals UI

**Validation note:** codebase-only for the existing CLI (fully read, high confidence).
context7/web research not escalated yet — no AI SDK or UI framework has been chosen, so
there's no third-party API surface to validate against until the design discussion resolves
those two open questions. Confidence: **high** for current-state findings; **low** for
forward-looking recommendations, since this epic is greenfield on both the AI layer and the UI.

## Summary

`cleanup-tools`' Python CLI (from the `harden-cleanup-cli` epic) has no AI SDK, no web/GUI
framework, and no network dependency of any kind today — `pyproject.toml` declares only
`PyYAML` (runtime) and `pytest` (dev). This epic adds two new subsystems on top of that CLI: a
pluggable AI-provider layer (bring-your-own API key) that helps decide what to do with ambiguous
files, and an approvals UI where the user reviews proposed moves before anything happens. Both
are explicitly named in `project-profile.yaml`'s `north_star.goal`, and both are greenfield —
there is no existing pattern in this codebase to build on, unlike the previous epic where the
OS-adapter and config patterns gave later stories something to follow.

## Key files & surfaces

- `src/cleanup_tools/commands/sort.py` (`run(adapter, args)`, lines 53-102) — returns a `plan`
  list of `{src, dest, bucket, dest_exists, [moved, error]}` dicts. This is the exact shape an
  approvals UI would need to render and let the user act on for sort proposals. Dry-run entries
  omit `moved`/`error` entirely (not `None`) — any UI/AI code reading this must check key
  presence, not just truthiness.
- `src/cleanup_tools/commands/reclaim.py` (`run(adapter, args)`, lines 432-499) — returns a
  richer structure: `categories.<name>.entries[]` (each `{path, size_bytes, master_path_refused,
  [deleted, reason|error]}`), `master_path_refusals[]`, and top-level byte/GB totals. Two
  distinct failure-signal keys exist — `reason` (master-path refusal, set at plan time, present
  even in dry-run) vs `error` (runtime `OSError`, `--go` only) — an approvals UI must distinguish
  these, not conflate them into one "failed" state.
- `src/cleanup_tools/config.py` — `BucketRule`/`MasterPath`/`Config` dataclasses,
  `load_config`/`save_config` (lines 148-262). **No "proposed but unapproved" state exists
  today** — `Config` is a flat, already-active rule list. An AI layer proposing new bucket rules,
  or a UI staging changes for approval, has nothing to hook into yet; this is new schema surface
  this epic must design, not just consume.
- `pyproject.toml` — `dependencies = ["PyYAML"]`, `dev = ["pytest"]`. No HTTP/web framework
  (Flask/FastAPI/aiohttp/httpx/requests), no GUI framework (Textual/PyQt/PySide/tkinter wrapper),
  no AI SDK (`anthropic`/`openai`/`google-generativeai`/etc.) anywhere — confirmed via `grep`
  across `src/`, not just the manifest. This epic picks the first of each.
- `docs/REQUIREMENTS.md:54-57` (`## Stack`) — "harden as a Node/TS or Python CLI... Optional tiny
  local UI later. **No network, no telemetry** — this touches personal files and must stay fully
  local." No AI/LLM mention anywhere in REQUIREMENTS.md — the AI vision lives only in
  `project-profile.yaml`'s `north_star`, not in the original requirements doc.
- `.pHive/project-profile.yaml:148-154` (`north_star`) — the authoritative source for this
  epic's intent: AI is "pluggable... bring-your-own API key/provider" (not a fixed vendor
  dependency) with a narrow job ("help decide what to do with **ambiguous files**", not a general
  assistant); `has_ui: true` is explicitly annotated as forward-looking, not descriptive of
  current code; `avoid` warns against over-building for distribution and against "any network or
  telemetry calls."

## Patterns & conventions

- **Adapter-style pluggability already exists for the OS layer** (`OSAdapter` ABC +
  `get_adapter()` factory in `src/cleanup_tools/adapters/`) — the AI-provider layer's
  "bring-your-own provider" requirement is structurally the same shape (an abstract interface +
  per-provider implementations + a factory/selector), so this epic can reuse that pattern rather
  than inventing a new one.
- **Plan-then-mutate is the established safety pattern** (`sort.py`, `reclaim.py`): compute a
  full plan first, only act on `--go`, record per-entry outcomes. An approvals UI is a natural
  extension of this — the "plan" is already the right shape to review; what's missing is a step
  between "plan computed" and "`--go` executes" where a human (or AI-assisted human) says yes/no
  per entry.
- **Config has no versioning/pending-state concept** — every other new-behavior story in the
  prior epic (master paths, bucket rules) extended `Config`'s schema additively. This epic likely
  needs to do the same for "AI-proposed rule, not yet approved" or "queued approval decision."

## Constraints

- **Hard constraint, in direct tension with this epic's core feature: no network, no telemetry.**
  `project-profile.yaml`'s own `north_star.avoid` states this explicitly, and it predates the AI
  vision in the same document — meaning the project's stated intent is "no network calls" AND
  "pluggable AI provider" simultaneously. These can only coexist if network access is scoped
  strictly to explicit, user-initiated AI-provider calls (the user's own key, the user's own
  request) and never anything ambient, automatic, or telemetry-shaped. This needs to be resolved
  as an explicit design decision, not glossed over — flagged for grill.
- **No existing UI technology choice** — three genuinely different directions exist (a local web
  UI served to the browser, a native desktop UI, a terminal UI), each with different dependency
  footprints and each plausible given "nice UI" + "local-first" + a currently pure-Python
  codebase. This is the single biggest open question for design discussion, structurally
  identical to the runtime choice in the prior epic.
- **No existing AI-provider choice or abstraction** — REQUIREMENTS.md never named specific
  providers; `north_star` says "Anthropic, Gemini, etc." illustratively, not prescriptively.
  Provider-agnostic design (matching the OS-adapter pattern) is implied, not optional.
- **`Config`'s current schema has no pending/proposed state** — must be extended, and the shape
  of that extension affects both the AI layer (what it writes) and the UI (what it reads/shows)
  equally, so it's a shared-foundation decision like the OS-adapter/config schema were last epic.

## Risks

- **High — scope ambiguity between "AI provider integration" and "approvals UI" as one epic.**
  These are two substantial, largely-independent subsystems (a network-calling AI abstraction;
  a rendering/interaction layer) that could each be their own epic. Bundling them risks the same
  "thin first slice" problem the design discussion will need to address head-on — likely via
  vertical slicing (e.g., ship the approval-queue mechanism and a manual/no-AI approval UI first,
  then layer AI-generated proposals into that queue as a second slice) rather than building both
  halves in parallel and integrating at the end.
- **Medium — the no-network hard rule could be violated by accident**, not just by design gap:
  any AI SDK usage that includes default telemetry/analytics/update-checking (many do) would
  violate `north_star.avoid` even if the core AI-call logic is properly gated behind explicit user
  action. Needs an explicit check per candidate SDK, not just a policy statement.
- **Medium — UI technology choice has irreversible-ish downstream cost.** Unlike the runtime
  choice (Python vs. Node, both viable, low switching cost), a web-UI vs. native-desktop vs. TUI
  choice shapes packaging, distribution, and the eventual "downloadable app" vision very
  differently — worth getting right now rather than treating as a detail.
- **Low — `Config`'s lack of pending-state today means whatever schema this epic adds has no
  prior art to diverge from accidentally**, unlike the bash-parity risk that dominated the last
  epic's bucket-rule work. This epic's correctness risk is more about design coherence than
  bash-compatibility.

## Open questions

1. **UI technology: local web UI (small Flask/FastAPI backend + browser frontend), native
   desktop (e.g. a Python GUI toolkit), or terminal UI?** No existing dependency points either
   way. This is this epic's equivalent of the runtime-choice decision from the prior epic.
2. **Which AI provider(s) ship first, and how is the abstraction shaped?** north_star says
   "Anthropic, Gemini, etc." illustratively — does v1 need multiple providers behind one
   interface (mirroring the OS-adapter's two-platform pattern) or is one provider (with the
   interface designed for more later) enough to start?
3. **What exactly does the AI layer decide, concretely?** "Help decide what to do with ambiguous
   files" is the north_star framing — does this mean: proposing bucket-rule matches for files
   `sort` currently dumps in "other"? Flagging `reclaim` candidates for the user's attention?
   Something else? This needs to be pinned down before any story can be written.
4. **How does "approval" actually work mechanically?** Does the AI/heuristic layer write
   proposals into an extended `Config` schema (pending rules/actions), which the UI then
   reads and the user approves/rejects, updating that same state? Or is there a separate
   approval-queue data structure? This determines the shared-foundation schema this epic's
   stories all depend on, same role `Config`/`OSAdapter` played last epic.
5. **Does this epic also need a non-AI, manual approvals UI as its own slice** (review
   `sort`/`reclaim`'s existing plan output and approve/reject before `--go`, with no AI
   involved at all), or is AI-assisted proposal review the only mode this epic builds?

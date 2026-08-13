# Design Discussion: AI-provider integration + approvals UI

## 1. What Are We Doing?

The `harden-cleanup-cli` epic gave us a real Python CLI: `cleanup survey`/`sort`/`reclaim`, all
working, all tested, all following the same pattern — compute a full plan, only mutate on
`--go`. This epic adds two things on top of that: a pluggable AI-provider layer (bring your own
API key) that helps decide what to do with ambiguous files, and an approvals UI where you review
proposed moves — with or without AI help — before anything actually happens.

I want to name the risk up front: "AI provider" and "approvals UI" are two substantial,
largely-independent subsystems (a network-calling abstraction; a rendering/interaction layer).
Treating them as one undifferentiated blob would blow past "thin first slice" the same way
trying to do all six scripts in one epic would have last time. So "done" for this epic
specifically means something narrower and sequenced: **first**, a manual approvals UI — no AI
at all — that lets you review `sort`'s and `reclaim`'s existing plan output and approve/reject
per entry before `--go` runs, backed by a new standalone approval-queue store (team review
caught that folding this into `Config` would create a concurrent-write race once both a UI
process and the CLI touch it — see §3 step 1). **Second**, an AI-provider layer (one provider to
start, interface shaped for more)
that proposes bucket-rule matches for files `sort` currently dumps in "other" — feeding into
the *same* approval queue the manual UI already built, not a separate mechanism. Grill flagged
that this sequencing quietly de-prioritizes the AI half of what was explicitly asked for by
name — I'm not deciding that unilaterally; open question 6 below asks you to confirm it
directly, since "AI might land in a follow-on epic" is a real scope call, not just an
implementation detail.

## 2. What I Found

- `sort.run`/`reclaim.run` already return exactly the data shape an approvals UI needs to render
  — `sort`'s plan is `[{src, dest, bucket, dest_exists, [moved, error]}]`; `reclaim`'s is richer
  (`categories.<name>.entries[]`, `master_path_refusals[]`, byte/GB totals). Nothing about either
  command needs to change to be "approval-ready" — the gap is entirely on the consuming side: no
  UI reads this today, and there's no persisted "pending decision" state between plan and
  `--go`.
- `config.py`'s `Config` (`bucket_rules`, `search_roots`, `master_paths`) has **no pending/
  proposed state** — every field is already-active. My first instinct was to extend this schema
  for approval state, the same role it played as shared foundation last epic; team review caught
  why that's wrong here (§3 step 1) — this epic's foundation is a *new*, standalone store instead.
- Zero AI SDK, zero web/GUI framework, zero network dependency anywhere in `pyproject.toml` or
  `src/` today — confirmed by direct grep, not inference. Both halves of this epic are
  greenfield; unlike last epic's OS-adapter (which had bash scripts to port against), there's no
  existing code pattern for either the AI layer or the UI to follow, only the OS-adapter's
  *shape* (abstract interface + factory) as a structural precedent worth reusing for the AI
  provider side.
- `docs/REQUIREMENTS.md`'s hard rule — "no network, no telemetry... must stay fully local" —
  predates the AI vision (which lives only in `project-profile.yaml`'s `north_star`) and is in
  direct, explicit tension with it. A bring-your-own-key AI layer necessarily makes network
  calls. I don't think this is actually a contradiction once stated precisely: "no network" means
  no *ambient/automatic* network activity — nothing phones home on its own. An AI call the user
  explicitly triggers, to a provider *they* configured with *their* key, is categorically
  different from telemetry — it's the one network activity this local-first tool is allowed to
  have, and only because the user asked for it in that exact moment. I want this distinction
  written down explicitly rather than assumed, because it's the kind of thing that's easy to
  slowly erode (e.g. an AI SDK's default telemetry/update-check would violate it even if the core
  AI-call logic is clean).

## 3. My Proposed Approach

1. **Build the approval queue as its own store, separate from `config.yaml`** — not an
   extension of `Config` after all. Team review (architect lens) caught a real problem with my
   original plan to fold this into `Config`: `load_config`/`save_config` do an unlocked,
   non-atomic full-file read-modify-write, which has been safe only because exactly one CLI
   process has ever touched that file at a time. This epic's whole premise is a long-running UI
   process and CLI invocations (`sort --from-queue`, step 3 below) both reading and writing
   queue state, which turns that same read-modify-write into a lost-update race — the UI loads,
   the user approves something, a CLI process loads/saves in between, the UI's save clobbers it.
   Splitting the hot, frequently-mutated queue into its own file (`~/.config/cleanup-tools/
   approval_queue.yaml`) — atomic writes (temp file + `os.replace`) plus an advisory lock
   (`fcntl.flock`) around each read-modify-write — fixes this without touching the cold,
   rarely-written `bucket_rules`/`master_paths` data at all. Queue entry shape: proposed action
   (move/delete), a reference to the plan entry it came from, a status
   (`pending`/`approved`/`rejected`), a source (`manual` or `ai:<provider>`), and (per the
   ui-designer lens's review — see open question 1) enough structure to support undo (status
   history, not just current status) and bulk operations (a group key so "approve all
   screenshots" is one write, not N). Grill's staleness finding still applies here regardless of
   which file the queue lives in: approval **execution** re-checks the referenced entry against
   the live filesystem immediately before acting and refuses with a clear "entry is stale,
   re-plan" outcome rather than trusting a snapshot that may no longer be true — additive to,
   not a replacement for, the per-entry error isolation `sort`/`reclaim` already have.
2. **Add an approval-queue-consuming execution path, additive to `--go`, not a replacement for
   it — right after step 1, since it only depends on the queue schema, not on the UI existing.**
   Team review (tpm lens) caught that my original ordering put this after the UI build for no
   real dependency reason — it can be built and tested against hand-built fixture queue entries
   before any UI exists, in parallel with or ahead of UI work. Grill separately caught that my
   first draft was ambiguous about whether this changes what `--go` means: it doesn't. Direct
   `sort --go`/`reclaim --go` from the terminal keeps working exactly as the prior epic built and
   tested it — no forced dependency on the UI or queue. The queue adds a *second*, separate way
   to execute (`sort --from-queue`/`reclaim --from-queue`, only acting on entries the queue
   marked `approved`), for anyone who wants the review step. This preserves the prior epic's
   safety guarantees untouched rather than silently redefining them.
3. **Build the manual approvals UI against `sort`/`reclaim`'s existing plan output** — no AI
   involved yet. This is deliberately the harder architectural decision (open question 1) to
   resolve first, since the AI slice is comparatively simple once a UI exists to feed proposals
   into. ui-designer's review flagged that the interaction model (not just the visuals) needs
   deciding now, before this story is written — see open question 1 and the pre-exec escalation
   noted there.
4. **Design the AI-provider interface** — mirroring `OSAdapter`'s shape (abstract interface +
   factory) for *pluggability*, but team review (architect lens) caught that the analogy stops
   there: an AI call is a different kind of thing than a local syscall, and the interface needs
   to say so explicitly, not just inherit the OS-adapter's shape and assume the rest follows.
   Concretely, scoped to "given an ambiguous file (one `sort` would bucket as `other`), propose a
   bucket and a confidence/rationale," with:
   - **Error taxonomy**: the interface returns a small set of typed outcomes (success,
     auth-failure, rate-limited, timeout, unparseable-response) — not a raw provider exception —
     so callers (the queue-writer) can react sensibly to each without knowing provider internals.
   - **Retry policy**: at most one retry, only on timeout/rate-limit, never silently more —
     directly closing the "retry-with-backoff that silently calls out more times than the user
     asked for" risk named in §4.
   - **API-key storage**: NOT in `config.yaml` (that file is plaintext and, per open question 4,
     I'd been assuming it was the natural home — it isn't, for a secret). Read from an
     environment variable first; optionally a dedicated `~/.config/cleanup-tools/credentials`
     file with `0600` permissions as a fallback, never the same file as bucket rules.
   - **Call volume**: one call per ambiguous file, with a per-invocation cap (configurable,
     defaulting to something small) so running `sort` against a large, mostly-`other` Downloads
     folder doesn't silently fire hundreds of calls.
   - **Sync execution**: the provider call is a plain synchronous function call from the UI's
     request handler — see open question 1's revised framework lean (Flask, not an async
     framework) — so there's no blocking-the-event-loop question to solve at all.
5. **Wire AI proposals into the queue from step 1** — the UI doesn't need to know whether a
   pending entry came from a human running `sort` or from the AI layer; it's the same
   review/approve/reject flow either way.
6. **Tests throughout**, same discipline as last epic — this epic's stakes are lower than
   `reclaim`'s master-paths guarantee (nothing here deletes anything the approval flow didn't
   explicitly approve), but the approval-queue's locking/atomicity and the AI-call network
   boundary both deserve the same rigor.

I'm explicitly not building `find`/`dedupe`/`corral-screenshots` integration with this flow, not
building multi-provider AI support beyond an interface that could hold more later, and not
building the "recurring keep-clean" trigger — all future-epic material, consistent with the
prior epic's boundary discipline.

## 4. What Could Go Wrong

- **High — the AI-provider interface could get designed against exactly one provider's quirks**
  and need rework the moment a second provider is added, the same risk the OS-adapter interface
  named for itself last epic (and resolved fine by staying narrow). Mitigation is the same:
  keep the interface to exactly what "propose a bucket for an ambiguous file" needs, nothing
  provider-specific leaking through.
- **High — network-call scope creep.** Once an AI SDK is a dependency, it's easy for something
  beyond the explicit, user-triggered call to sneak in (SDK default telemetry, an update check, a
  retry-with-backoff that silently calls out more than once). This directly threatens the
  no-network hard rule in a way that's easy to miss in review unless specifically checked for.
- **Medium — the approval-queue schema, if designed too narrowly around `sort`'s plan shape,
  might not generalize to `reclaim`'s richer/multi-category shape** (or vice versa) — worth
  designing against both shapes from the start, not just the simpler one.
- **Medium — UI technology adds a real dependency footprint to a project that's been
  dependency-minimal by design** (one runtime dep, `PyYAML`, so far). Whatever gets chosen in
  open question 1 should be weighed against that minimalism, not chosen for developer novelty.
- **Low — this epic could balloon into "the AI epic" and "the UI epic" retroactively** if the
  manual-UI slice turns out to be bigger than expected. I think the sequencing in §3 makes that
  an acceptable outcome (ship the manual UI as real, working value even if the AI half becomes
  its own follow-on), not a plan failure.

## 5. Dependencies and Constraints

- **Hard constraint, now precisely scoped: no *ambient* network/telemetry.** Explicit,
  user-triggered AI-provider calls (their key, their request) are the one sanctioned exception —
  see §2's reasoning. Every other network-shaped thing (SDK telemetry, update checks, anything
  automatic) stays forbidden.
- **Hard constraint, revised after team review: `--go` stays independent, not gated by
  approval.** My first draft said "nothing gets `--go`'d without going through approval," which
  contradicts §3 step 2's fix for the same gap grill caught — direct `--go` keeps working exactly
  as the prior epic built it; the queue (`--from-queue`) is an additive, optional review path,
  not a replacement gate. The actual invariant this epic must not weaken: `sort`/`reclaim`'s
  existing dry-run/`--go` safety model stays byte-for-byte as tested, whether or not anyone ever
  touches the approval queue.
- **This epic depends on `harden-cleanup-cli` being complete** (it is) — specifically on
  `sort.run`/`reclaim.run`'s plan shapes and `config.py`'s schema, both stable dependencies to
  build on.
- **This epic blocks nothing further named in `north_star`** — packaging/distribution for other
  users is still explicitly secondary/later, per `north_star.avoid`.

## 6. Open Questions (all confirmed by user)

1. **UI technology: local web UI, native desktop, or terminal UI?** My lean is a **local web
   UI** — a small **Flask** server (synchronous — team review (architect lens) flagged that an
   async framework like FastAPI buys nothing for a single-local-user tool and would raise a
   real blocking-the-event-loop question once AI-provider calls are involved; Flask sidesteps
   that entirely) bound to `127.0.0.1` only (never `0.0.0.0` — grill's finding, now a committed
   requirement, not an implied property), opened in the browser via a CLI command (e.g. `cleanup
   approve`). Reasoning: this project's ambiguous files are overwhelmingly *visual* (screenshots,
   photos — the CLEANUP-PLAN.md survey's single biggest clutter category), and being able to see
   a thumbnail before approving a move matters a lot for a "nice UI." A terminal UI (Textual)
   can't show images; a native desktop toolkit adds more packaging complexity for the same visual
   capability a browser already has for free. **team review (ui-designer lens) flagged that this
   is a review-speed tool, not just a viewer** — the interaction model needs, at minimum: bulk
   actions (approve-all-within-a-group, since `other`/reclaim categories can hold hundreds of
   entries), keyboard shortcuts for fast single-entry triage (the single biggest clutter category
   this whole project exists to address), undo, and pagination for large plans. (A team H/V
   review caught that the ui-designer lens's original "before/after diff view for judging
   duplicates" doesn't map to any real data this epic touches — `reclaim.run`'s categories are
   junk/build-cache/installer/docker, not duplicate-vs-master comparison; that's `dedupe`'s
   domain. **Confirmed dropped from this epic** — noted explicitly for revisiting once a future
   epic actually integrates `dedupe`, so this doesn't get silently lost.) **After sign-off
   review, the user also asked for a DiskDrill-style visual overview** — a real dashboard
   showing bucket/category breakdown with sizes and counts, not just a status-count summary —
   as part of this epic's floor, not a later polish pass. This enriches the `GET /` dashboard
   route (§3 step 3 / Slice 3a) rather than adding a new layer: same data the queue already
   has, grouped and sized for an at-a-glance view of how files are being organized, alongside
   the approve/reject flow, not a separate visualization system. These aren't
   polish on top of the approval-queue schema (step 1 above) — they're requirements ON it (status
   history for undo, group keys for bulk ops, ordering/cursors for pagination). **ui-designer's
   review called `SCALE_CALL: pre-exec`** — the interaction model
   needs wireframing now, before story-writing locks in that schema, not sometime during
   implementation. Recorded as an escalation in cycle state. **CONFIRMED: Flask.**
2. **Which AI provider ships first?** **CONFIRMED: Anthropic**, interface shaped so
   Gemini/others are additive later, mirroring the OS-adapter's macOS-first, Arch-second
   precedent.
3. **What exactly does the AI layer decide?** **CONFIRMED: propose a bucket for files `sort`
   currently dumps in `"other"`** — the concrete, bounded case that already exists in the code
   today. Reclaim-candidate judgment and anything broader stays out of scope for this epic.
4. **Approval mechanics: settled as its own store, not a `Config` extension** (§3 step 1,
   revised after team review caught the concurrent-write race a shared file would create).
5. **Does the manual (no-AI) approvals UI ship as real, standalone value even if the AI half
   slips to a follow-on epic?** **CONFIRMED: yes.**
6. **Manual-UI-first / AI-second sequencing, accepting the AI half might land in a follow-on
   epic if this one runs long?** **CONFIRMED: yes, sequence it.**

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest for the approval-queue schema and AI-provider interface; manual verification
         for the UI itself (a local web UI's actual look-and-feel isn't unit-testable the same
         way plan/config logic is).
  Platforms: macOS (this box) — same platform scope as the CLI epic; Arch Linux verification
             deferred the same way it was last epic ("we do arch another day").
  Automated: approval-queue read/write/status-transition logic, AI-provider interface's
             propose-a-bucket contract (mocked provider — no real API calls in automated tests,
             per the no-network-in-CI-shaped-work principle), sort/reclaim's --go-consumes-
             approval-queue wiring.
  Manual: run the actual local web UI against a real (or realistic fixture) sort/reclaim plan,
          confirm approve/reject actually gates --go, confirm thumbnails render for image files.
  Not verifying: the AI provider's actual judgment quality (whether its bucket proposals are
                 *good* is a product-quality question, not a correctness test); multi-provider
                 support beyond the interface existing; Arch Linux (deferred).
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~20-25 across three new subsystems (a standalone approval-queue store with
                  locking/atomicity, a new Flask UI package, a new AI-provider package) plus
                  wiring changes to sort.py/reclaim.py.
  Subsystems: approval-queue (new, standalone, not a Config extension), AI-provider layer (new,
              its own error/retry/security/cost model — NOT just an OSAdapter clone), UI layer
              (new, first UI this project has ever had, with a real interaction model — bulk/
              keyboard/undo/pagination/diff — not just a viewer) — three layers, all three
              genuinely new, one (UI) flagged by its own reviewer for pre-exec wireframing.
  Migration required: no data migration (no users yet); new standalone store, not a schema
                       migration on existing config.
  Cross-team coordination: no — solo project.
  Unknowns: 4 open questions remaining (UI technology + wireframe scope, AI provider choice, AI
            scope, epic-boundary sequencing confirmation), plus a recorded pre-exec escalation
            for UI wireframing.

  RECOMMENDATION: team review (tpm lens) caught that my first draft's own risk analysis argued
  for Large scope while I'd downgraded the call to Medium via team-size/no-migration reasoning
  that didn't actually address the complexity drivers I'd just named. Taking that correction:
  this needs the full H/V + structured outline treatment, not just H/V. The structured
  outline's Risk Registry is exactly where the AI-provider security/cost model and the
  approval-queue concurrency design belong — this doc's revisions patched the worst gaps team
  review found, but a Medium-scope H/V pass alone wouldn't have caught them in the first place.
  RATIONALE: Three genuinely new subsystems (not two, and not "extends existing" for any of
  them anymore, since the approval queue moved out of Config), a concurrency/security surface
  this codebase has never had before, and an explicit pre-exec UI escalation together argue for
  Large, not Medium — even though it's still a solo project with no migration and no
  cross-team coordination.
```

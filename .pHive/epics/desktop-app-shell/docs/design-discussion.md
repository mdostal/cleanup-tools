# Design Discussion: Desktop App Shell (Tauri + Python Sidecar)

## 1. Goal

Three epics in, `cleanup-tools` is a real, tested local tool: a hardened CLI
(`survey`/`sort`/`reclaim`/`find-wallets`/`dedupe`/`corral-screenshots`), an approval-queue-backed
Flask review UI (`cleanup approve`) with AI-assisted "other"-bucket proposals, and 414 passing
tests. `north_star.goal` in `.pHive/project-profile.yaml` names this project's destination
explicitly: *"Ship a local-first desktop app anyone can download to clean up their Mac... Near-term
priority is making it work reliably as a local tool for the author first; packaging/distribution
for other users is secondary."* The "reliable local tool" phase is the part that's now done. This
epic is the deliberate start of the second half of that sentence: turn the existing Flask review UI
+ CLI into an actual double-clickable desktop app — something you launch from the Dock/app menu,
not `cleanup approve` typed into a terminal — for macOS and Arch Linux, with Windows kept
architecturally possible but explicitly not built yet.

This is not a rewrite. Nothing about `sort`/`reclaim`/`queue.py`/the AI-provider layer changes.
The Flask app under `src/cleanup_tools/ui/` stays the product; this epic wraps it in a native
shell (Tauri v2) and fixes the one piece of its current behavior that reads fine as a CLI/browser
tool but reads as *broken* inside a native app window: `/plan/reclaim`'s ~2-minute, no-feedback
`du`-shelling wait (see §2 of the research brief, and confirmed directly in
`src/cleanup_tools/commands/reclaim.py`'s `dir_size_bytes` calls, lines ~226/257). A native window
that just sits there for two minutes gets force-quit; that would undermine the entire point of
packaging this as a real product, so the project owner has already decided this performance fix
ships as part of this epic, not deferred to a follow-on.

The research brief (`.pHive/epics/desktop-app-shell/docs/research-brief.md`) already answered the
technology questions this design discussion would otherwise have to litigate — Tauri v2, PyInstaller
sidecar, per-platform build requirement, unsigned macOS, local-only PKGBUILD. This document takes
those as settled and focuses on shaping the epic: how the pieces decompose, what's genuinely
ambiguous, what could go wrong, and how big this actually is.

## 2. Proposed Approach

Five pieces, likely five (or six, if the performance fix and Tauri shell split) future stories.

### a. PyInstaller build for the Flask backend

Freeze `src/cleanup_tools/ui/`'s Flask app plus its full dependency tree (Flask, Pillow, PyYAML,
`anthropic` and its transitive httpx/pydantic stack) into one binary per target platform, per the
research brief's Option A recommendation (the dominant real-world pattern for exactly this
Tauri+Flask shape — every real prior-art example found uses it, none uses Nuitka or a bundled
runtime). Concrete gotchas to budget real time for, all cited in the brief §2:

- **Hidden imports and import-scanning breakage.** `anthropic`/httpx/pydantic's import graph is
  the most likely source of "works from source, breaks in the frozen binary" bugs — PyInstaller's
  static import scanner can miss dynamically-imported modules these libraries use. Needs a real
  spike (build the binary, exercise every route including `/propose-ai`) before trusting it, not
  just a `pip list` review.
- **Pillow's compiled deps.** libjpeg/libpng bindings must be present in the *build* environment
  per platform, not just importable at dev time — this is a per-platform-build-machine concern,
  reinforcing why there's no single-machine cross-build for this piece either.
- **Templates/static files are data, not code.** `src/cleanup_tools/ui/templates/*.html` and
  `static/keyboard.js` need explicit PyInstaller `datas=`/`--add-data` treatment (spec-file, not
  bare `--onefile`) or the frozen binary serves `TemplateNotFound` on every route — this fails
  loudly in a first smoke test, which is good, but it's an easy thing to forget on the first pass
  and worth calling out in the story itself so it isn't rediscovered by trial and error.
- **`--onefile` vs `--onedir`** is not fully decided yet — see Open Questions.

One useful finding while reading the actual code: `run_server()` in `src/cleanup_tools/ui/app.py`
already takes an `open_browser: bool = True` parameter. The sidecar's entrypoint doesn't need a new
CLI flag or a forked code path to suppress the "open a system browser tab" behavior — it can call
`run_server(adapter, open_browser=False)` as-is. That's real, existing surface area this epic gets
for free.

### b. The Tauri v2 shell

`tauri.conf.json`'s `bundle.externalBin` points at the PyInstaller binary, named per Rust
target-triple convention (`bin/cleanup-ui-x86_64-unknown-linux-gnu`, etc. — see brief §1).
`src-tauri/capabilities/default.json` grants exactly the `shell:allow-execute` (or
`allow-spawn`) permission scoped to that one sidecar binary, nothing broader. Process lifecycle
follows the brief's §6 pattern directly, since it's well-trodden prior art rather than something to
invent: Tauri's `setup()` hook spawns the sidecar in the background and returns immediately
(never blocks waiting for readiness — the brief flags synchronous waiting inside `setup()` as *the*
common mistake); the webview window appears immediately showing a static, Tauri-bundled loading
screen (plain HTML/JS shipped with the Tauri frontend, not served by Flask — it has to render before
Flask is listening); the frontend polls a cheap health endpoint on `127.0.0.1:<port>` until it
responds, then navigates to the real UI. **This requires adding a `/healthz` route to
`routes.py`** — none of the current routes (`/`, `/plan/*`, `/propose-ai`, `/queue*`,
`/thumbnail/<id>`) are a suitably cheap readiness check; `/` does real queue-loading work. A small,
explicit addition, not a design risk.

Crash/quit handling has to be hand-rolled — the brief confirms no polished official Tauri primitive
exists for this yet (the relevant plugin proposal is open, not merged). The PyInstaller-bootloader-
PID gotcha is the specific thing to design around: if the sidecar is a PyInstaller `--onefile`
binary, Tauri's `kill()` only has the bootloader's PID, not the real Python/Flask process it execs
— naively killing it can orphan the actual server (and leave port 5151 bound) after the app quits.
Mitigations the brief names: prefer `--onedir` (bootloader forwards signals more reliably), have
Flask handle `SIGTERM` and shut down cleanly, and/or kill the whole process group from the Rust
side rather than a single tracked PID. Sidecar process-exit (crash) should surface as an explicit
error state in the webview, not a silent hang.

### c. Flask-side progress reporting for `survey`/`reclaim`

This has to fit the codebase as it actually is today, not an idealized rewrite. Reading
`src/cleanup_tools/ui/app.py` and `routes.py` directly: this is a single Flask process, run via
plain `app.run(host, port, debug=False, use_reloader=False)` — no `threaded=` argument set (so
Werkzeug's dev server defaults to handling one request at a time), no task queue, no async
framework, no persistence layer beyond `queue.yaml`/`config.yaml`. `/plan/reclaim` is the concrete
slow path right now: it calls `reclaim_module.run(adapter, args=None)` synchronously, which
shells out to `du` per candidate directory via `adapter.dir_size_bytes`. (`cleanup survey` has the
same `du`-shelling shape per the brief, but it's CLI-only today — no UI route calls it — so the
*current, concrete* instance of the 2-minute problem inside the packaged app is `/plan/reclaim`;
the fix should still be written as a small reusable helper so a future `survey` route isn't a
second bespoke implementation.)

**Proposed shape: background thread + job-id + polling `/status/<job_id>`, not Server-Sent
Events.** Reasoning: SSE needs a long-lived streaming connection, which is a meaningfully bigger
lift on a single-threaded Werkzeug dev server (real concurrency handling, connection lifecycle,
more surface for the "GUI app with no browser dev-tools to debug a stuck stream" failure mode) for
a problem that plain polling solves adequately at this scale (one user, one job at a time,
sub-second acceptable poll interval). Concretely:

- A new small module (e.g. `src/cleanup_tools/ui/jobs.py`) holding an in-memory,
  lock-guarded `dict[job_id, JobState]` — deliberately **not** persisted to disk. A job only
  matters for the life of one open app window/browser tab; unlike `queue.yaml` (which must survive
  process restarts and be safe for concurrent CLI+UI access), a job registry losing state on
  restart is an acceptable, even correct, tradeoff.
- `POST /plan/reclaim` (or a new `/jobs/reclaim` route, TBD at story-write time) spawns a
  `threading.Thread` running the existing `_stage_reclaim_plan`/`reclaim_module.run` work, records
  progress as it iterates candidates, and returns a `job_id` immediately rather than blocking.
- `GET /status/<job_id>` returns `{status: "running"|"done"|"error", current, total, result?,
  error?}` — cheap, poll-friendly, matches the plain synchronous request/response style every
  other route in this file already uses.
- **`app.run(..., threaded=True)` needs to be added** alongside this — today's `run_server` doesn't
  set it, which is fine for one-request-at-a-time browser use but would make `/status` polls queue
  up behind whatever else is in flight once a job is running. Small, explicit change, worth naming
  in the story rather than discovering it as a bug later.

This is a real, if modest, change to `routes.py`/`app.py` — flagged here so the design discussion
doesn't understate it as "just add a progress bar."

### d. macOS packaging

`.app` + `.dmg` via Tauri's bundler, **unsigned**. Per the brief and the project owner's decision:
rely on right-click → Open to bypass Gatekeeper's unidentified-developer block, since this is a
personal tool being installed on the owner's own machine(s), not distributed to strangers.
Signing/notarization (Developer ID cert + Apple Developer Program, $99/yr) stays a clearly-flagged
future story — the brief confirms this is a config/secrets problem, not an architecture problem, so
deferring it doesn't paint the app into a corner later.

### e. Arch Linux packaging

A **local-only PKGBUILD**, build-from-source style: `makedepends` on `cargo`/`nodejs`/whatever JS
package manager the Tauri frontend uses, running `tauri build -b deb` (or equivalent) under
`makepkg -si` on the owner's own Arch box. Not published to the public AUR — no AUR-maintainer
obligations (SSH-key auth, `.SRCINFO` generation, ongoing update responsibility) for now. Runtime
deps: `webkit2gtk-4.1`, `gtk3`, `cairo`, per Tauri's WebKitGTK-based Linux runtime.
`options=('!strip')` (or similar) is likely needed so `makepkg` doesn't strip the bundled
PyInstaller sidecar binary in a way that breaks it — noted in the brief, worth carrying into the
actual PKGBUILD when it's written.

### f. Explicitly out of scope for this epic

- **Windows implementation.** No build, no installer, no Windows-specific testing. The only
  obligation this epic has toward Windows is not painting the architecture into a corner — e.g.
  keeping the sidecar/health-check/job-status design platform-agnostic so a future Windows story
  can slot in NSIS (not MSI, since NSIS is the one of the two Tauri supports building
  cross-platform, per brief §5) without rearchitecting anything built here.
- **Publishing to the public AUR.** The PKGBUILD is real and used, just not pushed to
  `aur.archlinux.org`.
- **Code signing / notarization** for macOS, and any equivalent Linux package-signing concern.
  Deferred, not forgotten — revisit if/when distributing beyond the owner's own machines becomes a
  real goal.

These are deliberate exclusions the project owner already made, not gaps in this document's
coverage — restated here so a reader of this doc alone (without the kickoff brief) doesn't mistake
silence for an oversight.

## 3. Open Questions

1. **Does the packaged app need its own branding, or does it wrap the existing Flask templates
   as-is for v1?** Tauri itself imposes a *floor* here regardless of preference: `tauri build`
   requires an app icon set (icns/ico/png) to produce a bundle at all, so "literally zero visual
   changes" isn't fully achievable — at minimum an icon has to exist. Beyond that floor (window
   title, an "About" screen, any restyling of the existing `base.html`/`dashboard.html`/`queue.html`
   templates), this is a real product decision, not something inferable from the research brief.
   My lean, stated but not decided: ship v1 with the existing templates completely unchanged and
   only the Tauri-required minimum (icon, window title = app name) — branding/restyling is cheap to
   add later and shouldn't gate the first working double-click build.
2. **Shared config/data directory: does the packaged app write to the exact same
   `~/.config/cleanup-tools/` paths the CLI already uses?** Almost certainly yes — the whole value
   of the approval queue is that `cleanup sort --from-queue`/`cleanup reclaim --from-queue` run
   from a terminal act on the same entries the packaged app's UI approved. Stating it explicitly
   here because it's a real design commitment (not a separate sandboxed data directory the way some
   packaged-app conventions default to), and because the queue's existing file-locking
   (`queue.py`) is exactly what makes this safe: it already assumes concurrent CLI+UI access.
3. **Does `cleanup approve`'s browser-auto-open behavior change once the desktop app exists?**
   No — both continue to coexist as two independent ways to reach the same UI, not one replacing
   the other. But reading `app.py` surfaced a concrete edge case this framing has to answer: both
   `run_server()` (CLI path) and the Tauri sidecar's entrypoint call the same function, hard-bound
   to `127.0.0.1:5151` (`DEFAULT_PORT`). If a user runs `cleanup approve` from a terminal while the
   desktop app is also open, the second one to bind fails outright. Worth deciding at story-write
   time whether the desktop app should (a) detect the port's already bound and just navigate its
   webview to the existing instance instead of trying to spawn its own sidecar, (b) fail with a
   clear "already running" message, or (c) pick a different port for the packaged app — this is a
   new failure mode that simply didn't exist when there was only ever one way to start the Flask
   process.
4. **`--onefile` vs `--onedir` for the PyInstaller build.** The brief flags this as "pending a
   spike into the bootloader-PID-kill gotcha," not fully resolved — `--onefile` is simpler for
   distribution but is the mode with the bootloader-PID gotcha; `--onedir` sidesteps that but ships
   a directory of files instead of one binary, which interacts with how `externalBin` and the
   macOS `.app`/Arch PKGBUILD packaging steps expect to consume it. Worth deciding whether this
   epic's first story includes that spike explicitly, since it affects the shape of every
   downstream packaging story.
5. **Binary size — genuinely unknown for this project's actual dependency tree.** The brief cites
   50-150MB as a general PyInstaller range for a Flask+Pillow+anthropic/httpx/pydantic stack, but
   explicitly flags it as unverified against this codebase. Worth an early `pyinstaller --onefile`
   spike (could be the same spike as Q4) to get a real number before any distribution-format
   assumptions (e.g. `.dmg` size, whether the local PKGBUILD download step is fast or annoying) get
   baked into a story.

## 4. Risks

Pulled forward from the research brief, in the project's own terms, plus what I found reading the
actual code:

- **PyInstaller import-scanning breakage** (brief §2) — `anthropic`/httpx/pydantic's import graph,
  plus Pillow's compiled deps, are the most likely source of "works from source, breaks in the
  frozen binary" failures. Real debugging time needs to be budgeted for this, not treated as a
  packaging formality.
- **PyInstaller-bootloader-PID kill gotcha** (brief §6) — killing the wrong PID on app quit can
  orphan the real Flask process holding port 5151. Not a data-corruption risk (the queue store's
  atomic writes + locking already protect against a mid-write crash), but a real resource-leak /
  "why won't this port free up" annoyance risk that needs the mitigations named in §2b above.
- **No single-machine cross-build** (brief §2, §4) — the PyInstaller sidecar binary and the Tauri
  bundle itself both need to be built on a real machine of each target platform (macOS, Arch/Linux,
  and ARM specifically if the Arch box is ARM). This is a genuinely new kind of dependency for a
  project that has had zero build-pipeline requirements until now.
- **Binary size unknown** (brief §2) — see Open Question 5; a real risk to distribution UX
  (download size, `.dmg` size, local PKGBUILD fetch time) that's currently a guess, not a number.
- **Toolchain-newness risk, not named in the brief because it's out of the brief's scope:** this is
  the first Rust/Tauri code this project has ever had. Every prior epic built on Python patterns
  already established in the codebase (the OS-adapter shape, the AI-provider interface shape); this
  epic has no internal precedent to build on for the Rust side at all — the research brief's cited
  prior art (dieharders' example repos, the zudo-tauri-wisdom docs) is the closest thing to
  in-house precedent that exists.
- **New failure mode from having two independent Flask-launch paths** — see Open Question 3's
  port-collision case. This risk didn't exist before this epic because there was only ever one way
  to start `run_server()`.
- **Re-verify the "no ambient network calls" guarantee against the actual Tauri dependency tree,
  not just docs.** The brief already flags this (§7) and treats it as "not found to be a problem"
  rather than "confirmed clean" — Tauri's core has no evidence of default telemetry, the updater is
  opt-in (this epic must not add `tauri-plugin-updater`), and analytics/crash-reporting plugins are
  opt-in too. Worth naming here in the same terms the AI-provider epic used for the Anthropic SDK:
  once real `Cargo.lock`/`package-lock.json` scaffolding exists, grep it directly rather than
  trusting this research pass alone — a transitive dependency pulling in something is a different
  risk category than a directly-added plugin, and only the actual lockfile can answer that.

## 5. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: a new Rust/Tauri package tree (src-tauri/, tauri.conf.json, capabilities/,
                  a bundled static loading-screen HTML/JS asset) — entirely new to the repo;
                  a new PyInstaller spec/build config; a small new Python module
                  (jobs.py or equivalent) plus real changes to routes.py/app.py for the health
                  endpoint and job-status polling; a macOS bundle config; an Arch PKGBUILD.
                  Comparable in count to the ai-approvals-ui epic's ~20-25 files, but spread
                  across more genuinely distinct technology stacks.
  Subsystems: five, all touched for the first time or in a new way — (1) a PyInstaller build
              pipeline, (2) a Tauri/Rust shell with its own permissions model and process-
              lifecycle code, (3) a Flask-side job/progress-reporting mechanism (new concurrency
              shape for a codebase that's been single-threaded-synchronous throughout), (4) a
              macOS distribution pipeline, (5) an Arch Linux distribution pipeline. The
              ai-approvals-ui epic was rated Large off three new subsystems; this one has five,
              two of which (Tauri/Rust, and dual-platform native builds) require infrastructure
              this project has never had before — a real build machine per target OS.
  Migration required: none (no existing users of a packaged app; the CLI/queue/config paths are
                      reused as-is per Open Question 2's near-certain "yes, same paths").
  Cross-team coordination: none — solo project, same as every prior epic.
  Unknowns: 5 open questions above, two of which (onefile/onedir, binary size) are recommended
            as explicit early spikes rather than assumptions to build the rest of the epic on.

  RECOMMENDATION: Large.
  RATIONALE: Not "Large because it sounds big" — weighing it the way prior epics did. Counting
  against Large: still a solo project, no migration, no cross-team coordination, and a good
  fraction of the work (tauri.conf.json, capabilities JSON, the PKGBUILD) is genuinely
  boilerplate/config-shaped rather than novel logic. Counting for Large, and outweighing that:
  this is the first time the project has ever depended on a second language/toolchain (Rust) with
  zero internal precedent to build from; it requires real per-platform build environments for the
  first time (no CI/build-pipeline dependency has existed until now); it bundles a genuine
  concurrency-shape change to the Flask backend (background threads + job polling, where
  everything has been synchronous-request/response so far) rather than deferring that as a
  separate epic; and it spans five subsystems against the AI epic's three, which was already
  Large. This document doesn't decompose into stories, but the five pieces in §2 are a reasonable
  starting shape for that decomposition, and several of the individual stories (e.g. Arch
  packaging, or the progress-reporting fix alone) would likely be Medium on their own — it's the
  sum, plus the new-toolchain and new-build-infrastructure unknowns, that argues for Large overall.
```

## 6. Dependencies

- **The Flask progress-reporting fix (§2c) has no hard dependency on the Tauri shell and should
  land early, likely first or fully in parallel.** It's a pure Flask-side change, testable via the
  existing `test_client()`-based suite with zero Tauri/Rust involvement, and it's independently
  useful to the current browser-based `cleanup approve` UI right now — a user hitting `/plan/reclaim`
  from a browser tab today gets the same silent 2-minute hang. The reverse dependency (Tauri's
  loading screen needs *a* health endpoint to poll) is trivial — a few-line `/healthz` route, not
  the full job-runner machinery — so it doesn't block early Tauri scaffolding either.
- **Early Tauri shell development does not need to wait on the finished PyInstaller build.** The
  sidecar mechanism just needs *a* binary at the expected path; a temporary manual stand-in
  (`python -m cleanup_tools.ui` wrapped in a thin shell script, or an early `--onedir` build) is
  enough to build and iterate on window creation, the loading screen, capabilities config, and the
  health-check-then-navigate flow. **Full crash/quit/bootloader-PID verification (§2b) is gated on
  the real frozen binary**, though — that gotcha is specific to how PyInstaller's actual bootloader
  behaves, and can't be meaningfully tested against a plain Python stand-in.
- **macOS and Arch packaging (§2d/§2e) both depend on the Tauri shell pattern being proven, and on
  having that platform's PyInstaller binary.** Recommend proving the full shell+sidecar+health-check
  pattern end-to-end on one platform first (macOS, being the primary work machine) before
  parallelizing the second platform's packaging story — otherwise two platforms are debugging the
  same unproven pattern simultaneously, which is harder to reason about than validating once and
  replicating.
- **Windows has zero dependency on anything in this epic** beyond the architectural
  NSIS-compatibility note in §2f — nothing here needs to wait for or coordinate with a Windows
  story that doesn't exist yet.

# Vertical Slice Plan: Desktop App Shell (Tauri + Python Sidecar)

**Input:** horizontal-plan.md + design-discussion.md §6 (Dependencies), which already answered
the sequencing questions this plan restates rather than re-derives.

## 1. Slicing Strategy

```
STRATEGY:
  Total horizontal items: 5 layers (Rust/Tauri package tree, PyInstaller build/spec layer, Flask
                          job/progress layer, macOS distribution layer, Arch distribution layer).
  Planned slices: 4 -- one per story.
  Slicing rationale: two layers (Flask job/progress, PyInstaller build/spec) have NO dependency on
  anything else in this epic and can start immediately, in parallel with each other -- confirmed
  directly by design-discussion §6 ("The Flask progress-reporting fix has no hard dependency on the
  Tauri shell", "Early Tauri shell development does not need to wait on the finished PyInstaller
  build"). The remaining two layers (macOS distribution, Arch distribution) are thin wrappers
  around the Rust/Tauri package tree once it exists, and design-discussion §6 explicitly recommends
  proving the shell+sidecar+health-check pattern on ONE platform (macOS) before parallelizing the
  second (Arch) -- so macOS packaging is folded into the Tauri-shell story itself (Slice 3) rather
  than split out, and Arch packaging becomes its own, later, dependent slice (Slice 4).
```

## 2. Vertical Slice Plan

```
## Slice 1: Flask progress reporting + health check

WHAT WORKS AFTER THIS SLICE:
  /plan/reclaim returns a job_id immediately instead of blocking for ~2 minutes; GET
  /status/<job_id> reports progress as the background thread works through candidates; GET
  /healthz answers cheaply for readiness polling. Fully usable from the EXISTING browser-based
  `cleanup approve` UI today -- no Tauri/Rust involvement at all.

BUILDS ON: nothing new -- src/cleanup_tools/ui/app.py and routes.py both already exist
  (ai-approvals-ui epic); this slice extends them.

LAYERS TOUCHED:
  Flask job/progress layer:
    - jobs.py (new): in-memory, lock-guarded job registry
    - background-thread runner wrapping _stage_reclaim_plan (routes.py:218-244)
    - GET /status/<job_id>, GET /healthz routes
    - app.run(..., threaded=True) at app.py:79

NOT YET:
  - Rust/Tauri package tree, PyInstaller binary, any packaging layer -- none of this slice's work
    requires them, and none of them require this slice's full job-runner machinery to exist yet
    either (a Tauri loading screen only needs /healthz, a few-line route, to start scaffolding).

VERIFIED BY:
  - pytest via Flask's existing test_client()-based suite (test_ui_routes.py pattern): job
    registry create/poll/complete/error transitions, /status/<job_id> shape, /healthz returns 200
    cheaply, /plan/reclaim returns a job_id without blocking.
  - Manual: none required -- this slice is fully covered by the automated suite, same as every
    other Flask-only story in this codebase's history.

COMMIT REPRESENTS: The concrete, current 2-minute-hang UX problem is fixed, independent of
  whether the desktop shell ever ships.

---

## Slice 2: PyInstaller sidecar build

WHAT WORKS AFTER THIS SLICE:
  A single frozen binary per target platform that runs the existing Flask app (routes, templates,
  static assets, AI-provider calls all included) with no Python interpreter pre-req -- verified by
  exercising every route (including /propose-ai) against the frozen binary directly, and an
  explicit onefile-vs-onedir decision backed by a real kill+verify-port-freed spike and a real
  measured binary size for this project's actual dependency tree.

BUILDS ON: nothing new -- freezes src/cleanup_tools/ui/ as it exists today (plus, if Slice 1 has
  landed by the time this runs, the /healthz route it added; if not, the frozen binary is
  perfectly testable via its existing routes and gets /healthz for free whenever Slice 1 lands,
  since both stories touch app.py/routes.py additively, not in conflicting ways).

LAYERS TOUCHED:
  PyInstaller build/spec layer:
    - .spec file with explicit datas= for templates/*.html and static/keyboard.js
    - onefile vs onedir spike (build both, kill+verify-port-freed, measure size) -> a documented
      decision
    - hidden-import verification across every route

NOT YET:
  - Rust/Tauri package tree -- the binary exists as a build artifact, not yet wired into any
    externalBin config.
  - Arch/macOS distribution packaging (the .spec's per-platform build; actual .app/.dmg/PKGBUILD
    wrapping happens in Slices 3-4).

VERIFIED BY:
  - Automated: a smoke-test script that starts the frozen binary as a subprocess, hits every route
    (including /propose-ai with a mocked/no-op AI call) over HTTP, and checks response shapes
    match the source-run Flask app's.
  - Manual/spike: kill the running frozen-binary process by its reported PID and verify (via
    lsof/netstat) whether the bound port frees immediately -- run once for --onefile, once for
    --onedir, decision recorded in the story.
  - Manual: eyeball the actual `du -sh`/`ls -la` binary size for both modes, recorded as a real
    number, not the brief's generic 50-150MB estimate.

COMMIT REPRESENTS: A real, verified, per-platform frozen binary + a settled onefile/onedir
  decision -- ready to be pointed at by externalBin whenever the Tauri shell needs it.

---

## Slice 3: Tauri v2 shell (macOS) + macOS packaging

WHAT WORKS AFTER THIS SLICE:
  `tauri dev`/`tauri build` produces a real macOS .app (and .dmg) that spawns the sidecar in the
  background (non-blocking setup()), shows a Tauri-bundled loading screen immediately, polls
  /healthz, navigates to the real Flask UI once ready, detects and navigates to an already-running
  instance instead of double-spawning on port collision, and handles sidecar crash/quit correctly
  against the REAL PyInstaller binary (not a stand-in) -- including the bootloader-PID mitigation
  decided in Slice 2. Unsigned, relies on right-click-Open.

BUILDS ON: Slice 2 for full verification (crash/quit/bootloader-PID behavior is specific to how
  PyInstaller's actual bootloader behaves and cannot be meaningfully tested against a stand-in).
  Early scaffolding within this slice (window creation, the loading screen, capabilities config,
  the health-check-then-navigate flow) can and should start against a temporary manual Python
  stand-in (e.g. `python -m cleanup_tools.ui` wrapped in a thin shell script) before Slice 2 lands
  -- design-discussion §6 explicitly names this as safe, since the sidecar mechanism only needs *a*
  binary at the expected path for that scaffolding work. Slice 1's /healthz route is consumed here
  but is trivial enough (a few-line route) that this slice does not hard-block on Slice 1 landing
  first either -- if Slice 1 hasn't landed yet, the stand-in script can serve a bare /healthz itself
  during early scaffolding.

LAYERS TOUCHED:
  Rust/Tauri package tree: tauri.conf.json (externalBin, window/icon config), capabilities/
    default.json (scoped shell:allow-execute/spawn), setup() hook, loading-screen HTML/JS,
    frontend healthz-poll-then-navigate logic, port-collision detection, crash/quit handling.
  macOS distribution layer: bundle.targets=[app, dmg], unsigned, app icon.

NOT YET:
  - Arch distribution layer (Slice 4) -- explicitly sequenced after this slice proves the pattern.
  - Windows -- architecturally left open, not built.

VERIFIED BY:
  - Automated (as much as a native-window app allows): Rust-side unit/integration tests for the
    port-collision-detection logic (a mocked /healthz response short-circuits spawning) and any
    platform-detection logic that's pure Rust/TS, run via cargo test / the JS test runner.
  - Manual (required, no automated substitute exists for a real native window): launch the built
    .app on the real macOS dev machine; confirm the loading screen appears immediately (not a
    blank/hung window); confirm navigation to the real UI once the sidecar is healthy; kill the
    sidecar process externally and confirm the webview surfaces an error state rather than hanging;
    quit the app and confirm (via lsof/ps) no orphaned sidecar process or bound port survives;
    start `cleanup approve` from a terminal while the desktop app is open and confirm the SECOND
    one to start navigates to the existing instance rather than erroring or double-spawning;
    right-click-Open the unsigned .app once to confirm Gatekeeper's bypass path works as expected.

COMMIT REPRESENTS: A real, working, unsigned macOS desktop app -- the pattern proven end-to-end on
  one platform, ready to be replicated for Arch.

---

## Slice 4: Arch Linux packaging

WHAT WORKS AFTER THIS SLICE:
  A local-only PKGBUILD that, when run with `makepkg -si` on a real Arch machine, builds the same
  Tauri shell + PyInstaller sidecar pattern proven in Slice 3, targeting Arch's runtime deps
  (webkit2gtk-4.1, gtk3, cairo). This story's own automated process can verify the PKGBUILD's
  syntax/structure and any pure-Python/Rust platform-detection logic, but CANNOT itself run/verify
  the actual build or the resulting package on Arch hardware -- that is an explicit, separate,
  owner-run manual step.

BUILDS ON: Slice 3 (the Tauri shell pattern must already be proven working on macOS before
  parallelizing a second platform's packaging story -- design-discussion §6's explicit
  recommendation, restated here as this slice's hard dependency) + Slice 2 (needs a Linux-target
  PyInstaller binary, built following the same onefile/onedir decision Slice 2 already made).

LAYERS TOUCHED:
  Arch distribution layer: PKGBUILD (pkgname/pkgver/makedepends/depends/build()/package()),
    options=('!strip'), Tauri Linux bundle config.

NOT YET:
  - Nothing -- this is the epic's final slice. Publishing to the public AUR remains permanently
    out of scope (decision 7), not a future slice.

VERIFIED BY:
  - Automated (what THIS story's own process can check without Arch hardware): PKGBUILD
    syntax/structure review (shellcheck-style sanity pass, correct field types/quoting), a diff of
    the makedepends/depends lists against documented Arch/Tauri conventions, review of any
    Python-side OS-detection logic this story touches (e.g. confirming nothing in the Python code
    assumes macOS-only paths that would break under an Arch-built sidecar).
  - Manual (REQUIRED, explicitly not claimed as automated-verified): the project owner runs
    `makepkg -si` on their own real Arch machine and reports back whether the build succeeds, the
    package installs, and the resulting app launches/spawns its sidecar/reaches the real UI --
    this story's acceptance criteria state this explicitly as a separate, owner-attested step, not
    something the automated pipeline claims to have tested end-to-end.

COMMIT REPRESENTS: Epic-complete on the confirmed scope (macOS + Arch, Windows architecturally
  possible but unbuilt) -- pending the owner's own manual makepkg confirmation.
```

## 3. Overlay Diagram

```
VERTICAL SLICE OVERLAY
────────────────────────────────────────────────────────────────────────────
              │ Slice 1        │ Slice 2       │ Slice 3        │ Slice 4    │
              │ (Flask jobs)   │ (PyInstaller) │ (Tauri macOS)  │ (Arch pkg) │
──────────────┼────────────────┼───────────────┼────────────────┼────────────┤
Flask job/    │ jobs.py,       │               │ (consumes      │            │
progress      │ /status,       │               │ /healthz)      │            │
layer         │ /healthz       │               │                │            │
──────────────┼────────────────┼───────────────┼────────────────┼────────────┤
PyInstaller   │                │ .spec, spike, │ (consumes the  │ (Linux-    │
build/spec    │                │ per-platform  │ macOS binary)  │ target     │
layer         │                │ binary        │                │ binary)    │
──────────────┼────────────────┼───────────────┼────────────────┼────────────┤
Rust/Tauri    │                │               │ full shell     │ (reuses    │
package tree  │                │               │ (new)          │ pattern)   │
──────────────┼────────────────┼───────────────┼────────────────┼────────────┤
macOS dist.   │                │               │ .app/.dmg      │            │
──────────────┼────────────────┼───────────────┼────────────────┼────────────┤
Arch dist.    │                │               │                │ PKGBUILD   │
────────────────────────────────────────────────────────────────────────────

Slices 1 and 2 have no dependency on each other or on anything downstream -- both can start
immediately and run in parallel. Slice 3 hard-depends on Slice 2 for full verification (soft/
scaffolding work can start early). Slice 4 hard-depends on Slice 3.
```

## 4. Deferred Items

```
DEFERRED (not in current slice plan):
  - Windows implementation (NSIS bundle target, Windows-specific process-lifecycle testing) --
    architecturally kept possible (platform-agnostic sidecar/health-check/job-status design), not
    built. No slice here needs to wait for or coordinate with it.
  - Code signing / notarization (macOS) and any equivalent Linux package-signing concern --
    deferred per decision 6, a config/secrets problem revisit-able without touching this epic's
    architecture.
  - Publishing the PKGBUILD to the public AUR -- permanently out of scope (decision 7), not a
    future slice.
  - Any new "About" screen or restyling of the existing Flask templates -- decision 5's floor
    (icon + window title only) is what ships; further branding is a future, separate concern.
  - A survey-route job wrapper reusing Slice 1's job-runner helper for `cleanup survey`'s
    equivalent du-shelling shape -- no UI route calls survey today, so there's nothing to wire it
    into yet; the helper is written reusably but not consumed a second time in this epic.

RATIONALE: each is either an explicit decision already made (5, 6, 7, Windows-out-of-scope) or has
zero dependency from Slices 1-4 as scoped -- safe to defer without blocking anything above.
```

## 5. Risk by Slice

```
RISK PER SLICE:
  Slice 1: Low-Medium -- a real but modest change to a codebase pattern (Flask routes/threading)
           this project already has plenty of precedent for; the concurrency shape (background
           thread + lock-guarded registry) is new but narrowly scoped and fully unit-testable.
  Slice 2: High -- import-scanning breakage (anthropic/httpx/pydantic's graph) and Pillow's
           compiled deps are named, real, budgeted risks; the onefile/onedir decision has a real
           tradeoff (simplicity vs. the bootloader-PID gotcha) that downstream Slice 3 depends on
           getting right.
  Slice 3: High -- first-ever Rust/Tauri code in this project, no internal precedent; the largest
           single story in the epic (complexity: high); crash/quit handling is explicitly
           hand-rolled since no polished official Tauri primitive exists yet (research brief §6).
  Slice 4: Medium -- most of the PKGBUILD is boilerplate/config-shaped once Slice 3's pattern is
           proven, but the total inability to run/verify on real Arch hardware during planning and
           execution is a structural verification gap this slice can only partially close.
```

## 6. Moldability Notes

- Slices 1 and 2 could ship as two small, independent, real epics on their own if this epic needed
  to be broken up further -- neither depends on the other or on anything downstream.
- If Slice 2's onefile/onedir spike surfaces a hard blocker with one mode, Slice 3's crash/quit
  design adjusts to match whichever mode Slice 2 settles on -- this is exactly why Slice 3 hard-
  depends on Slice 2 for full verification rather than assuming a mode upfront.
- Slice 4 cannot be meaningfully de-risked further during planning: the single biggest lever
  (parallelizing Arch packaging alongside macOS) was deliberately rejected per design-discussion
  §6's "prove once, replicate" reasoning, precisely to avoid two platforms debugging the same
  unproven pattern simultaneously.
- No slice can be dropped without dropping the epic's confirmed scope (macOS + Arch) -- this is
  already the minimum slice count for the two independent early layers plus the two genuinely
  sequential packaging layers.

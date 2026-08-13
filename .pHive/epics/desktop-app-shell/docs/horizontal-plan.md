# Horizontal Planning Scan: Desktop App Shell (Tauri + Python Sidecar)

**Input:** research-brief.md + design-discussion.md. Both already did the heavy layer-mapping
thinking (design-discussion §2's five-piece breakdown, §5's Scale Assessment) — this doc restates
it per-layer, per this epic's Large scope, rather than re-deriving it from scratch.

## 1. Layer Inventory

- **Rust/Tauri package tree** — entirely new to the repo: `src-tauri/` (Cargo.toml, `src/main.rs`
  or `lib.rs`), `tauri.conf.json` (`bundle.externalBin`, window/icon config), `src-tauri/
  capabilities/default.json` (scoped `shell:allow-execute`/`allow-spawn` grant), a bundled static
  loading-screen HTML/JS asset shipped with the Tauri frontend (not served by Flask). First Rust
  code this project has ever had — no internal precedent, cited external prior art only (research
  brief §1/§6, dieharders' example repos, zudo-tauri-wisdom docs).
- **PyInstaller build/spec layer** — new: a `.spec` file (or `--onefile`/`--onedir` CLI invocation)
  freezing `src/cleanup_tools/ui/`'s Flask app + its full dependency tree (Flask, Pillow, PyYAML,
  `anthropic`/httpx/pydantic) into one binary per target platform, with explicit `datas=` treatment
  for `templates/`/`static/`. Named per Rust target-triple convention so `externalBin` finds it.
- **Flask job/progress layer** — new module `src/cleanup_tools/ui/jobs.py` (in-memory, lock-guarded
  job registry, not persisted), a background-thread runner for `/plan/reclaim`, a new
  `GET /status/<job_id>` route, a new `GET /healthz` route, and `app.run(..., threaded=True)` added
  to `run_server` (`src/cleanup_tools/ui/app.py:79`). This is the one layer with zero Tauri/Rust
  involvement and no dependency on anything else in this epic (design-discussion §6).
- **macOS distribution layer** — new: Tauri bundler config for `.app`+`.dmg` targets, unsigned,
  an app icon (icns) satisfying Tauri's hard requirement to produce any bundle at all.
- **Arch distribution layer** — new: a local-only PKGBUILD (build-from-source,
  `makedepends`=cargo/nodejs/JS-package-manager), Tauri Linux bundle config (`webkit2gtk-4.1`,
  `gtk3`, `cairo` runtime deps), `options=('!strip')` or similar to protect the bundled PyInstaller
  binary.

`find-wallets`/`dedupe`/`corral-screenshots` UI integration, Windows implementation, code
signing/notarization, and publishing to the public AUR are explicitly out of this epic's layer
inventory (design-discussion §2f).

## 2. Per-Layer Requirements

```
## Layer: Rust/Tauri package tree

NEEDED:
  - tauri.conf.json: bundle.externalBin pointing at the PyInstaller binary (per-target-triple
    naming, e.g. binaries/cleanup-ui-aarch64-apple-darwin), window title = app name, app icon.
  - src-tauri/capabilities/default.json: shell:allow-execute (or allow-spawn) scoped to exactly
    the one sidecar binary name -- nothing broader.
  - setup() hook: spawns the sidecar in the background, returns immediately (never blocks).
  - A bundled, Tauri-static loading-screen HTML/JS asset (NOT served by Flask -- it must render
    before Flask is listening).
  - Frontend JS: polls GET http://127.0.0.1:<port>/healthz in a loop; on success, navigates the
    webview to the real UI.
  - Port-collision detection: before spawning the sidecar, hit /healthz on the expected port; if
    it responds, skip spawning and navigate straight to the existing instance instead.
  - Crash/quit handling: listen for the sidecar child's exit event, surface an explicit error
    state in the webview rather than a silent hang; on app quit, prefer a clean shutdown signal
    (SIGTERM, or process-group kill) over a bare kill() of a possibly-wrong PID (research brief
    §6's PyInstaller-bootloader-PID gotcha).
  - No tauri-plugin-updater, no analytics/telemetry/crash-reporting plugin (research brief §7) --
    an explicit non-goal, not an oversight.

NOT NEEDED (this epic):
  - Windows bundle targets/capabilities -- architecturally left open (platform-agnostic sidecar/
    health-check/job-status design), not built.
  - Any new "About" screen or restyling of the existing Flask templates (decision 5).

---

## Layer: PyInstaller build/spec layer

NEEDED:
  - A spec file (not bare --onefile) with datas= entries for
    src/cleanup_tools/ui/templates/*.html and static/keyboard.js.
  - An explicit --onefile vs --onedir spike: build both, kill the process and verify the bound
    port frees (or doesn't) for each, measure real binary size for this project's actual
    dependency tree.
  - Hidden-import verification: exercise every UI route (including /propose-ai, which pulls in
    anthropic/httpx/pydantic's import graph) against the frozen binary, not just a pip list review.
  - A build-environment note: Pillow's compiled libjpeg/libpng deps must be present on each
    platform's *build* machine, not just importable at dev time.
  - Entrypoint calls run_server(adapter, open_browser=False) -- src/cleanup_tools/ui/app.py:45-79
    already exposes this parameter; no new CLI flag or forked code path needed.

NOT NEEDED (this epic):
  - A Windows binary -- no build, no spec target for it.
  - Nuitka or a bundled-runtime alternative (research brief §2 Options B/C) -- PyInstaller is the
    settled choice, not re-litigated per-story.

---

## Layer: Flask job/progress layer

NEEDED:
  - src/cleanup_tools/ui/jobs.py: in-memory, lock-guarded dict[job_id, JobState], deliberately
    NOT persisted to disk (a job matters only for the life of one open window/tab).
  - A background-thread runner wrapping the existing _stage_reclaim_plan/reclaim_module.run work
    (routes.py:218-244), recording progress as it iterates candidates.
  - GET /status/<job_id> -> {status: running|done|error, current, total, result?, error?}.
  - GET /healthz -- a new, deliberately cheap route (none of the existing routes qualify; even
    GET / does real queue-loading work via _load_entries()/_group_entries(), routes.py:172-182).
  - app.run(..., threaded=True) added at src/cleanup_tools/ui/app.py:79, so /status polls don't
    queue up behind an in-flight job (Werkzeug's dev server defaults to one-request-at-a-time).

NOT NEEDED (this epic):
  - Server-Sent Events -- explicitly rejected in favor of polling (design-discussion §2c).
  - A survey-route job wrapper -- cleanup survey has the same du-shelling shape but no UI route
    calls it today; the job-runner helper is written reusably so a future survey route isn't a
    second bespoke implementation, but no new route is added for it in this epic.
  - Any change to the job registry's persistence model -- in-memory-only is a deliberate,
    permanent choice for this layer, not a v1 shortcut.

---

## Layer: macOS distribution layer

NEEDED:
  - Tauri bundle.targets: ["app", "dmg"], unsigned.
  - An app icon (icns), the one hard requirement Tauri imposes to produce any bundle at all.
  - Verification that the frozen macOS PyInstaller binary + the Tauri shell pattern work together
    end to end (spawn, healthz poll, navigate, quit/crash handling) BEFORE Arch packaging starts.

NOT NEEDED (this epic):
  - Code signing / notarization -- deferred, config/secrets problem not architecture (decision 6).

---

## Layer: Arch distribution layer

NEEDED:
  - A local-only PKGBUILD: pkgname/pkgver/pkgdesc/arch=('x86_64')/makedepends (cargo, nodejs, the
    JS package manager Tauri uses)/depends (webkit2gtk-4.1, gtk3, cairo).
  - options=('!strip') (or similar) so makepkg doesn't strip the bundled PyInstaller sidecar
    binary in a way that breaks it.
  - Tauri Linux bundle config producing at minimum a .deb (or whatever `tauri build -b` target the
    PKGBUILD's build() function invokes).
  - Automated-test acceptance criteria scoped honestly to what's checkable WITHOUT Arch hardware
    (PKGBUILD syntax/structure, Python-side platform-detection logic, shell-command review against
    documented makepkg/Arch conventions) -- separated explicitly from a final manual step requiring
    the project owner to run `makepkg -si` on a real Arch box and report back.

NOT NEEDED (this epic):
  - Publishing to the public AUR -- the PKGBUILD is real and used locally, never pushed to
    aur.archlinux.org (decision 7 / design-discussion §2f).
  - AppImage -- named as a viable future fallback/complement in the research brief, not built here.
```

## 3. Cross-Layer Dependencies

```
PyInstaller build/spec layer -> Rust/Tauri package tree (the frozen binary IS the file
  bundle.externalBin points at; full crash/quit verification needs the REAL binary, not a stand-in)
Flask job/progress layer -> Rust/Tauri package tree, ONE-WAY AND THIN: the frontend loading screen
  polls /healthz, that's the entire coupling. The full job/status polling machinery has no Tauri
  dependency at all and is independently useful to the existing browser-based `cleanup approve` UI.
Rust/Tauri package tree -> macOS distribution layer (macOS packaging wraps the already-proven shell
  pattern; doesn't re-invent it)
Rust/Tauri package tree + PyInstaller build/spec layer -> Arch distribution layer (same wrapping
  relationship, on the second platform, sequenced after macOS proves the pattern once)

The Flask job/progress layer is the one layer with NO upstream dependency on anything else in this
epic -- same "can land first, independently useful" role research-brief.md's Relevant Flag section
and design-discussion.md §6 both call out explicitly.
```

## 4. Layer Map Diagram

```
HORIZONTAL LAYER MAP
─────────────────────────────────────────────────────────────────────────────────
Flask job/       │ jobs.py registry  │ background-thread │ /status/<id> +   │
progress layer   │ (in-memory, lock) │ reclaim runner    │ /healthz routes  │
──────────────────┼───────────────────┼────────────────────┼──────────────────┤
PyInstaller       │ onefile/onedir    │ hidden-import +   │ per-platform     │
build/spec layer  │ spike + decision  │ datas= spike       │ frozen binary    │
──────────────────┼───────────────────┼────────────────────┼──────────────────┤
Rust/Tauri        │ sidecar config +  │ loading screen +  │ crash/quit +     │
package tree      │ capabilities      │ healthz poll       │ port-collision   │
──────────────────┼───────────────────┼────────────────────┼──────────────────┤
macOS             │ .app / .dmg       │ unsigned, icon     │ (proves pattern) │
distribution      │                   │                    │                  │
──────────────────┼───────────────────┼────────────────────┼──────────────────┤
Arch               │ PKGBUILD          │ webkit2gtk-4.1/    │ owner-run        │
distribution       │ (local-only)      │ gtk3/cairo deps    │ makepkg -si      │
─────────────────────────────────────────────────────────────────────────────────
```

## 5. Scope Summary

```
HORIZONTAL SCOPE:
  Layers affected: 5, all new (Rust/Tauri package tree, PyInstaller build/spec layer, Flask
    job/progress layer, macOS distribution layer, Arch distribution layer) -- matches
    design-discussion.md §5's Scale Assessment (5 subsystems, RECOMMENDATION: Large) exactly.
  Total items: ~1 new Python module + 2 new Flask routes + 1 modified app.run() call; 1 new
    src-tauri/ package tree with its own config/capabilities/loading-screen asset; 1 PyInstaller
    spec with an explicit onefile/onedir spike; 2 packaging configs (macOS .app/.dmg, Arch
    PKGBUILD).
  New vs modified: everything Rust/Tauri/PyInstaller-side is wholly new (no existing precedent to
    extend); the Flask layer is a real but modest extension of existing routes.py/app.py.
  Estimated total effort: Large (per design-discussion.md §5 -- reproduced there, not re-derived).

  LARGEST LAYER: Rust/Tauri package tree -- process lifecycle, capabilities, port-collision
    handling, and packaging plumbing all live here; the Tauri-shell story built on it is flagged
    high complexity for exactly this reason.
  RISKIEST LAYER: PyInstaller build/spec layer (import-scanning breakage, unverified binary size,
    the onefile/onedir bootloader-PID tradeoff) and the Rust/Tauri package tree (first-ever
    Rust/Tauri code, no internal precedent) are tied for riskiest -- see vertical-plan.md's
    risk-by-slice section.
```

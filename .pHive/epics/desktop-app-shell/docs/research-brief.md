# Research Brief: Tauri + Python Sidecar Desktop Packaging

**Status:** research only — no application/Rust/Tauri code written.
**Scope:** answer the 7 questions in the epic kickoff brief for `cleanup-tools`, packaging the existing
Flask review UI (`src/cleanup_tools/ui/`) + Python CLI as a Tauri desktop app, cross-platform
(macOS + Arch Linux now, Windows later).
**Date of research:** 2026-08-13. Tauri moves fast — re-verify version-specific details before
implementation if this brief is more than a few months old.

---

## 1. Tauri version and sidecar mechanism

**Current stable Tauri**, as of this research, is the **2.11.x line** — 2.11.5 released 2026-07-01
(2.11.4 on 2026-06-28, 2.11.3 on 2026-06-17, 2.11.0 on 2026-04-30). Tauri 2.0 went stable
2024-10-02; the epic should target **Tauri v2**, not v1 — v1 docs/APIs (`v1.tauri.app`) are legacy
and meaningfully different (permissions/capabilities model, config shape, plugin architecture all
changed in v2). ([Tauri Core Releases](https://tauri.app/release/core/))

**Sidecar mechanism**: Tauri's "sidecar" feature bundles an external binary into the app and lets
the Rust side spawn it as a managed child process, via the `tauri-plugin-shell` plugin.

- **Config**: add an `externalBin` array to the `bundle` object in `tauri.conf.json` (relative
  paths resolve from `src-tauri/`, the directory `tauri.conf.json` lives in):
  ```json
  { "bundle": { "externalBin": ["binaries/my-sidecar"] } }
  ```
- **Target-triple naming**: Tauri's bundler expects a separate binary per platform, named with the
  Rust target triple appended, e.g. for `externalBin: ["bin/python"]` it looks for
  `src-tauri/bin/python-x86_64-unknown-linux-gnu` on Linux x86, or
  `src-tauri/bin/python-aarch64-apple-darwin` on Apple Silicon. Discover the local triple via
  `rustc --print host-tuple`. This means **the sidecar binary must be built per target platform** —
  there is no cross-compiling a Linux binary from macOS in the general case for this kind of
  PyInstaller output (see §2).
- **Invoking**: from Rust, `app.shell().sidecar("my-sidecar")` (name = filename only, not the
  configured path) then `.spawn()`; from JS, `Command.sidecar('binaries/my-sidecar')`.
- **Permissions**: Tauri v2's capabilities system requires an explicit grant, e.g. in
  `src-tauri/capabilities/default.json`:
  ```json
  { "permissions": ["core:default",
    { "identifier": "shell:allow-execute",
      "allow": [{ "name": "binaries/app", "sidecar": true }] }] }
  ```
  (`shell:allow-spawn` if using `spawn()` instead of `execute()`). Arguments passed to the sidecar
  can be constrained via static values or regex validators in the same capability entry — relevant
  here since the sidecar will need to be told what port/config to use.
- **stdin/stdout**: the shell plugin exposes `CommandEvent::Stdout`/`Stderr` and a `write()` on the
  child for stdin — usable both for log capture and as a simple control channel (see §6 — one
  example project drives sidecar startup/shutdown over stdin/stdout).

Sources:
[Sidecar guide (v2.tauri.app)](https://v2.tauri.app/develop/sidecar/),
[tauri-docs sidecar.mdx source](https://github.com/tauri-apps/tauri-docs/blob/v2/src/content/docs/develop/sidecar.mdx),
[Bundling a CLI Binary as a Tauri v2 Sidecar (dev.to)](https://dev.to/chenxxpro/bundling-a-cli-binary-as-a-tauri-v2-sidecar-lessons-from-building-a-desktop-app-5po)

---

## 2. Bundling the Python backend (Flask + Pillow + PyYAML + anthropic SDK)

Goal: end users get a double-clickable app with no Python pre-req. Two real options surfaced:

**Option A — PyInstaller-built standalone binary as the sidecar (this is the dominant
community pattern for exactly this Tauri+Flask/FastAPI shape).** PyInstaller freezes the Python
interpreter, the app's code, and all its dependencies (Flask, Pillow, PyYAML, `anthropic`) into one
executable; that executable *is* the file Tauri's `externalBin` points at. This is what every
real-world Tauri+Python example found uses:
- [dieharders/example-tauri-python-server-sidecar](https://github.com/dieharders/example-tauri-python-server-sidecar)
  (Tauri v1) and its v2 counterpart
  [example-tauri-v2-python-server-sidecar](https://github.com/dieharders/example-tauri-v2-python-server-sidecar)
  (FastAPI, not Flask, but same shape — HTTP server as subprocess) both PyInstaller-compile the
  Python server into `/src-tauri/bin/api/` and invoke it as a sidecar.
- [aiechoes.substack.com writeup, "Building Production-Ready Desktop LLM Apps: Tauri, FastAPI, and
  PyInstaller"](https://aiechoes.substack.com/p/building-production-ready-desktop) — explicitly
  frames PyInstaller spec files as the thing you need to get right for real apps: hidden imports,
  binary deps, and bundled data files (relevant here — Pillow has binary deps, and this project's
  Jinja templates/static thumbnails under `ui/` count as "data files" that need explicit
  `--add-data` entries or spec-file `datas=` treatment, not just `--onefile`).
- Practical implication for this project specifically: `anthropic`'s SDK and its transitive deps
  (httpx, pydantic, etc.) plus Pillow's compiled wheel all need to resolve correctly under
  PyInstaller's import-scanning — this is usually fine but is the most likely source of "works
  from source, breaks in the frozen binary" bugs, worth budgeting real debugging time for.

**Option B — Nuitka** compiles Python to C and produces a binary; findings say Nuitka's
folder-mode output is roughly **2x PyInstaller's size**, though Nuitka's `--onefile` mode narrows
that gap; Nuitka has a reputation for **faster runtime** than PyInstaller/cx_Freeze at the cost of
**longer compile times**. No example specifically pairs Nuitka with a Tauri sidecar was found (all
real prior art uses PyInstaller) — Nuitka is a plausible alternative but is the road less traveled
for this exact pattern. ([Nuitka vs PyInstaller comparison](https://coderslegacy.com/nuitka-vs-pyinstaller/),
[empirical pyinstaller vs nuitka vs cx_Freeze](https://x321.org/empirical-pyinstaller-vs-nuitka-vs-cx_freeze/))

**Option C — bundle a full Python runtime** (e.g. python-build-standalone + venv + Tauri
`resources`) instead of freezing to one binary. Not seen used in any Tauri-sidecar example found;
adds more moving parts (Tauri would spawn `python` from the bundled runtime with a script path
rather than spawning a single opaque binary) and more surface area for the target-triple-naming
externalBin mechanism to fight with. Not recommended as the primary approach given Option A is
the well-trodden path.

**Recommendation for this project**: PyInstaller, one binary per target platform
(`--onefile` for macOS/Linux distribution simplicity, matching the target-triple naming Tauri's
bundler expects), built **on** each target platform (see cross-compilation caveat in §4 — AppImage
tooling in particular doesn't cross-compile ARM). This means the build pipeline needs an actual
macOS runner and an actual Arch/Linux runner (or at minimum a Linux x86_64 environment) — there is
no single-machine cross-build for the Python sidecar binaries.

Real tradeoffs to flag explicitly for the design-discussion phase: PyInstaller binaries bundling a
full CPython + Flask + Pillow + anthropic/httpx/pydantic stack commonly land in the **50-150MB**
range per platform (not verified against this project's actual dependency tree — worth a quick
`pyinstaller --onefile` spike early to get a real number before committing to a distribution
story); build time is not instant (adds a real step per platform to the release process); and any
native/compiled dependency (Pillow's libjpeg/libpng bindings) must be present in the *build*
environment per platform, not just importable at dev time.

---

## 3. macOS packaging

Tauri's macOS bundler output is a standard **`.app` bundle**, and Tauri can also produce a
**`.dmg`** as a distributable disk image (`tauri build` bundle targets include `app` and `dmg`).

**Signing/notarization are not a hard technical requirement to run the app** — but they are the
difference between "double-click and it opens" and "Gatekeeper says this is from an unidentified
developer / is damaged." Concretely:
- Apple requires a paid Developer ID Application certificate (code signing) **and** notarization
  for an app to launch without any Gatekeeper friction when distributed outside the App Store.
  Without both, Gatekeeper blocks the app on any machine other than the one that built it.
- The known **workaround for personal/unsigned distribution**: right-click the `.app` → "Open" →
  confirm in the dialog. This bypasses the standard double-click Gatekeeper block. This matches
  what the user already expects/described ("they'll need to right-click-open once") and is a
  legitimate, real option for a personal tool that only the user (and maybe a couple of
  known-to-them machines) will install — no App Store, no external distribution to strangers.
- If/when signing is wanted, **Tauri v2 automates it**: given a Developer ID identity and
  Apple credentials as environment variables, `tauri build` handles signing and notarization
  (including stapling the ticket) as part of the build — this is a config/secrets problem
  (Apple Developer Program membership, $99/yr), not an architecture problem, so it can be deferred
  without changing the app's shape later.

**Recommendation**: ship unsigned initially (consistent with "personal tool, not App Store"), rely
on right-click-Open, and treat signing/notarization as a clearly-flagged future story if/when
distributing beyond the user's own machines becomes a real goal.

Sources:
[macOS Code Signing (v2.tauri.app)](https://v2.tauri.app/distribute/sign/macos/),
[Shipping a Production macOS App with Tauri 2.0 (dev.to)](https://dev.to/0xmassi/shipping-a-production-macos-app-with-tauri-20-code-signing-notarization-and-homebrew-mc3),
[Code Signing a Tauri App for macOS — The Complete Flow (dev.to)](https://dev.to/hiyoyok/code-signing-a-tauri-app-for-macos-the-complete-flow-54jk)

---

## 4. Arch Linux packaging

Tauri's Linux bundler can target **Debian package (.deb), RPM, AppImage, Flatpak, Snap, and
Arch User Repository (AUR)** — Tauri v2 docs have a **dedicated AUR distribution guide**, which is
the idiomatic path for an Arch user (not an afterthought — this is a first-class documented Tauri
target). ([Distribute overview](https://v2.tauri.app/distribute/), [AUR guide](https://v2.tauri.app/distribute/aur/))

**AUR path (recommended for this user's Arch box)**: publish a `PKGBUILD` to the AUR.
- Standard PKGBUILD fields apply (`pkgname`, `pkgver`, `pkgdesc`, `arch=('x86_64' 'aarch64')`,
  `url`, `depends`, `source`, optional `.install` script for icon-cache/desktop-database refresh
  hooks).
- Runtime deps for a Tauri/WebKitGTK app on Arch include `cairo`, `gtk3`, `webkit2gtk-4.1`, etc.
- Two concrete AUR strategies seen in practice: (a) a PKGBUILD that downloads a **pre-built `.deb`
  from GitHub Releases and extracts it with `tar`**, skipping compilation entirely on the user's
  machine (fastest install, but ships a binary built elsewhere — needs trust/reproducibility
  thought); or (b) a PKGBUILD that **builds from source** on the user's machine via
  `pnpm tauri build -b deb` (or similar), with `makedepends` including `cargo`, `nodejs`, `pnpm` —
  slower install, fully rebuilt locally, more "properly Arch."
  For a personal tool this user controls, option (b) (build-from-source PKGBUILD) is more
  idiomatic Arch practice and avoids trusting a prebuilt binary artifact.
- Must generate `.SRCINFO` via `makepkg --printsrcinfo > .SRCINFO` before pushing to the AUR git
  remote (`aur.archlinux.org`, SSH-key auth) — standard AUR maintainer workflow, nothing
  Tauri-specific here.
- `options=('!strip' '!debug')`/`!emptydirs` commonly needed to avoid stripping the bundled Python
  sidecar binary in ways that break it.
- Since the AUR requires nothing be *published* publicly-discoverable to be usable (a personal
  PKGBUILD can also just be built locally with `makepkg -si` and never pushed to the public AUR at
  all, or kept in a private git remote) — worth deciding during planning whether this ever needs to
  actually go on the public AUR, or just needs a PKGBUILD the user runs locally on their own Arch
  box. The latter is simpler and sufficient for a personal tool.

**AppImage as a fallback/complement**: Tauri also produces AppImages directly. Caveat: **the
AppImage must be built on (or targeting) the *oldest* base system you want to support**, because it
bundles against that system's WebKitGTK 4.1 — Ubuntu 22.04 / Debian 12 are cited as reasonable
baselines. Also, **linuxdeploy (Tauri's AppImage tool) cannot cross-compile ARM AppImages** — ARM
builds must happen on real ARM hardware or an ARM emulator. Since this is genuinely relevant to a
"build on the actual target platform" story already established for the Python sidecar (§2), this
reinforces needing an actual Arch/Linux (and ARM, if the Arch box is ARM) build environment, not
just cross-compiling from macOS.

Sources:
[AUR distribution guide](https://v2.tauri.app/distribute/aur/),
[AppImage guide](https://v2.tauri.app/distribute/appimage/),
[Using Binary for Distribution / Arch Linux Packaging discussion (GitHub issue #9812)](https://github.com/tauri-apps/tauri/issues/9812),
[Arch Linux Forums — AppImage bundling errors](https://bbs.archlinux.org/viewtopic.php?id=295219)

---

## 5. Windows (lower priority, for future reference)

Tauri produces either:
- **MSI** installers via the WiX Toolset v3 — **can only be built on Windows**.
- **NSIS** setup executables (`*-setup.exe`), supported since Tauri v1.3 — **can be built
  cross-platform** and is the only target of the two that supports ARM64 Windows.

Both targets can be configured simultaneously in `bundle.targets`. Output lands in
`target/release/bundle/msi/` and `target/release/bundle/nsis/` respectively, including update
signature files if the updater is configured. For a future cross-compiled Windows build (e.g. from
CI without a Windows box), **NSIS is the practical choice** over MSI given the cross-compile
constraint. No Windows-specific action needed now — noted for when "eventually" arrives.

Sources:
[Windows Installer guide (v2.tauri.app)](https://v2.tauri.app/distribute/windows-installer/),
[NSIS bundle migration discussion (GitHub #6859)](https://github.com/tauri-apps/tauri/issues/6859)

---

## 6. Process lifecycle: spawn sidecar → confirm listening → point webview at it → handle crash/quit

This is well-trodden prior art, not something to invent from scratch — the "wrap a localhost dev
server / API server as a sidecar, then load it in the webview" pattern shows up repeatedly:

- **Do not block Tauri's `setup()` hook waiting for the sidecar to become ready.** Multiple
  sources independently flag this as *the* common mistake: if you synchronously wait inside
  `setup()` before creating the window, the whole app appears hung — no window, no Dock icon,
  nothing — for however long the backend takes to start. This is directly relevant to this
  project's flagged ~2-minute `survey`/`reclaim` `du`-shelling performance issue (see §7/below):
  even just *starting* the Flask sidecar should be fast (Flask itself starts in well under a
  second), but if any startup-time work is added later, the same "must not block setup()" rule
  applies doubly.
- **Recommended pattern**: create the webview window immediately with a lightweight local loading
  screen (a static HTML/JS "starting up…" page bundled directly with the Tauri app, not served by
  the Flask backend), spawn the sidecar in the background, and have the *frontend* (not Rust
  `setup()`) poll/await the sidecar's readiness (e.g. hitting `http://127.0.0.1:<port>/healthz` or
  similar in a loop, or using a small helper like the `wait-for-localhost` npm pattern) before
  navigating to the real UI. This directly solves the "avoid a race/blank-page issue" concern
  raised in the task brief.
- **Readiness check** should be an actual HTTP request to a known-cheap endpoint (health check),
  not just "did the process spawn" — a spawned-but-not-yet-`listen()`ing Flask dev server is a
  classic source of the blank-page/connection-refused race.
- **Crash handling**: no polished official Tauri primitive was found for this specifically (one
  GitHub feature request, [Sidecar Lifecycle Management Plugin
  (plugins-workspace#3062)](https://github.com/tauri-apps/plugins-workspace/issues/3062), proposes
  exactly this — SIGTERM-then-timeout-then-SIGKILL graceful shutdown plus crash recovery — and is
  open, not merged, meaning **this project will need to hand-roll basic sidecar health monitoring
  and restart-or-error-surface behavior in Rust**, e.g. listening for the child process's exit
  event and showing an error state in the webview rather than a silent hang.
- **Shutdown on app quit**: Tauri's built-in sidecar handling (`Command::new_sidecar` /
  `app.shell().sidecar()`) does attempt automatic cleanup of the spawned child on app exit. Two
  concrete gotchas surfaced repeatedly and are directly relevant here:
  1. If the sidecar binary is a **PyInstaller `--onefile` executable**, Tauri (and `kill()`) only
     has the PID of the PyInstaller *bootloader* process, not the real Python process it
     bootstraps and execs — naively killing the bootloader PID can leave the actual Flask process
     (and its bound port) alive as an orphan. This is called out explicitly in the
     [example-tauri-v2-python-server-sidecar](https://github.com/dieharders/example-tauri-v2-python-server-sidecar)
     project's docs. Mitigations: use PyInstaller's non-onefile (`--onedir`) mode where the
     bootloader more reliably forwards signals, and/or have the Python side handle SIGTERM itself
     and shut down Flask's server cleanly, and/or use a proper process-group kill (kill the whole
     group, not just the tracked PID) from the Rust side.
  2. If the Flask process itself spawns further children (not expected here, but worth confirming
     stays true — this project's `du` shell-outs during survey/reclaim are short-lived subprocesses
     already, not long-running children, so this is likely a non-issue, but flag it as something to
     re-check once the sidecar's actual process tree is observed in practice).
  3. Stdin/stdout-based control channel: at least one example wires the frontend to tell Tauri to
     start/stop the sidecar via commands piped over the sidecar's stdin/stdout rather than relying
     purely on OS-level process kill — worth considering as a cleaner, more deterministic shutdown
     signal than a bare process kill, given gotcha #1 above.

Sources:
[Loading Screen pattern (zudo-tauri-wisdom)](https://takazudomodular.com/pj/zudo-tauri/docs/architecture/loading-screen/),
[Process Lifecycle pattern (zudo-tauri-wisdom)](https://zudo-tauri-wisdom.takazudomodular.com/),
[HTTP Request to server running in sidecar (Tauri discussion #5391)](https://github.com/tauri-apps/tauri/discussions/5391),
[Sidecar process is still alive when the main process exits (Tauri issue #1896)](https://github.com/tauri-apps/tauri/issues/1896),
[example-tauri-v2-python-server-sidecar](https://github.com/dieharders/example-tauri-v2-python-server-sidecar),
[Sidecar Lifecycle Management Plugin proposal (plugins-workspace#3062)](https://github.com/tauri-apps/plugins-workspace/issues/3062)

---

## 7. Existing project constraint: "no ambient network calls / no telemetry"

This project's hard rule (`.pHive/CONTEXT.md` Conventions) is that the tool must stay fully local
except for explicit, user-triggered `ANTHROPIC_API_KEY`-gated AI calls. Findings relevant to
whether Tauri itself risks violating this:

- **No evidence Tauri's core runtime or CLI phones home by default.** A 2019 GitHub issue
  ([tauri-apps/tauri#166](https://github.com/tauri-apps/tauri/issues/166), "Opt-In Telemetry,
  Security Patching and Environment Dumps") proposed an *opt-in* telemetry mechanism for the Tauri
  *project itself* to understand adoption — this was a feature **request**, not a description of
  existing behavior, and no evidence was found (in official docs, philosophy page, or current
  release notes) that this shipped or that Tauri collects telemetry today. This should be treated
  as "not found to be a problem" rather than "confirmed clean" — same caveat this project applied
  to the Anthropic SDK before trusting it; worth a source-level check (Tauri's Rust crates are
  open source) before fully signing off, the same way the AI-provider epic verified the Anthropic
  SDK's behavior against actual SDK source rather than docs claims alone.
- **The updater is opt-in and requires explicit configuration** (signing keys generated via
  `tauri signer generate`, an `endpoints` list, a `pubkey` in `tauri.conf.json`) — if this project
  simply **does not add the updater plugin**, there is no update-check network call. This is an
  easy, concrete guardrail: **do not add `tauri-plugin-updater` to this app**, and the "no ambient
  network calls" property holds by omission, not by needing to configure an update server to be
  privacy-respecting.
- **Third-party analytics plugins exist** (`tauri-plugin-aptabase`, a community
  `tauri-plugin-telemetry`, Sentry-tauri for crash reporting) — none of these are default or
  ambient; they are opt-in dependencies a developer would have to deliberately add. As long as this
  project's `Cargo.toml`/`package.json` never pulls one in, the constraint holds.
- **Practical recommendation for the design-discussion phase**: treat "do not add any Tauri
  plugin whose purpose is analytics/telemetry/crash-reporting/auto-update" as an explicit rule
  alongside the existing AI-provider-layer rules, and note it in whatever becomes this epic's
  equivalent of the AI epic's SDK-telemetry verification — i.e. actually grep the vendored
  `Cargo.lock`/`package-lock.json` once real Tauri scaffolding exists, rather than relying on this
  research pass alone, since a transitive dependency pulling in something is a different risk than
  a directly-added plugin.

Sources:
[Analytics/Telemetry/Error Tracking Options for Offline Usage (Tauri discussion #5959)](https://github.com/tauri-apps/tauri/discussions/5959),
[Opt-In Telemetry issue #166](https://github.com/tauri-apps/tauri/issues/166),
[Updater plugin docs](https://v2.tauri.app/plugin/updater/),
[Tauri Philosophy](https://v2.tauri.app/about/philosophy/)

---

## Relevant flag (not to solve here): the ~2-minute `du`-shelling UX problem

Per the task brief, `cleanup survey`/`cleanup reclaim` can take ~2 minutes on a real machine
because they shell out to `du` per directory. This is not this brief's job to fix, but it is a
real constraint on the packaging architecture, not a separable concern:

- A native app window that just sits there for 2 minutes with no feedback reads as **broken**, in a
  way a CLI printing nothing while `du` grinds does not (a CLI's blinking cursor implicitly signals
  "still working"; a GUI with no spinner reads as hung, and users force-quit hung apps).
- This has a direct architectural implication the design-discussion phase should account for: the
  Flask backend's long-running endpoints (survey, reclaim) need **some** progress-reporting
  mechanism the Tauri webview can render — e.g. Server-Sent Events or long-polling from the
  existing Flask process, or a simple "job started / poll /status" pattern — rather than a single
  synchronous request that just hangs the browser tab (which is already mediocre in the current
  browser-tab UI, and becomes actively bad once it's a packaged native app with no browser chrome,
  no visible network-tab affordance, and an implicit expectation of native-app responsiveness).
- This is a good candidate to fix in the Flask layer *before or alongside* the desktop-shell work
  (a spinner/progress-bar UI needs a backend that can report progress, which the current
  synchronous-request architecture cannot do without a Flask-side change) rather than being purely
  a Tauri/frontend concern — flagging for the design-discussion phase to sequence correctly.

---

## Recommended approach (synthesis for the design-discussion phase)

- **Tauri v2** (2.11.x line current as of this research) — not v1. Use the current
  `tauri.conf.json` v2 shape and the v2 capabilities/permissions system throughout.
- **Sidecar bundling**: PyInstaller `--onefile` (or `--onedir`, pending a spike into the
  bootloader-PID-kill gotcha in §6) to freeze the existing Flask app (`src/cleanup_tools/ui/`) plus
  its full dependency set (Flask, Pillow, PyYAML, `anthropic`) into one binary per target platform,
  wired in via `tauri.conf.json`'s `bundle.externalBin`, named per Rust target-triple convention.
  Build this binary **on each real target platform** (macOS build machine/CI runner, Arch/Linux
  build machine/CI runner) — no viable single-machine cross-build path was found for either the
  PyInstaller binary or the AppImage tooling.
- **Per-platform packaging targets**:
  - **macOS**: `.app` + `.dmg`, **unsigned initially**, distributed with the expectation the user
    right-click-Opens once; revisit signing/notarization as a separate future story once/if
    distributing beyond the user's own machine(s) matters.
  - **Arch Linux**: a **PKGBUILD**, built-from-source style (`makedepends` on `cargo`/`nodejs`/the
    JS package manager in use, `pnpm tauri build -b deb` or equivalent), kept as a local/personal
    PKGBUILD to start (no obligation to publish to the public AUR unless the user wants that later)
    — this is the idiomatic Arch path and matches the user's explicit "Arch matters, not an
    afterthought" framing. AppImage is a viable fallback/complement if a build-and-copy-anywhere
    artifact is ever wanted, with the caveat that it must target an old-enough WebKitGTK baseline
    and cannot be ARM-cross-compiled.
  - **Windows** (future): NSIS over MSI, specifically because NSIS is the one of the two that
    supports cross-platform building and ARM64 — relevant once "eventually" arrives and there may
    not be a dedicated Windows build machine.
- **Process lifecycle**: spawn the sidecar in the background without blocking Tauri's `setup()`;
  show an immediately-available, Tauri-bundled (not Flask-served) static loading screen; have the
  frontend poll a cheap `/healthz`-style endpoint on `127.0.0.1:<port>` until it responds, then
  navigate the webview to the real UI; treat sidecar process-exit as an explicit error state
  surfaced in the webview rather than a silent hang; on app quit, prefer a clean signal (SIGTERM,
  or an explicit stdin shutdown command the Flask process handles) over a bare process kill, given
  the PyInstaller-bootloader-PID gotcha.
- **Telemetry/network posture**: do not add `tauri-plugin-updater` or any analytics/crash-reporting
  plugin; this alone keeps Tauri's own footprint at zero ambient network calls, consistent with the
  existing "no network, no telemetry" rule and its one sanctioned AI-call exception. Re-verify
  against actual `Cargo.lock`/`package-lock.json` contents once real scaffolding exists, the same
  way the AI-provider epic verified the Anthropic SDK against source rather than docs claims alone.
- **Sequencing flag for the design-discussion phase**: the `du`-shelling ~2-minute survey/reclaim
  slowness is a real UX blocker for a "real packaged product" and likely needs a Flask-side
  progress-reporting change (SSE/polling/job-status) before or alongside the Tauri shell work, not
  as an afterthought bolted onto a synchronous-request UI.

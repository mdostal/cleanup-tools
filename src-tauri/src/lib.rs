//! Tauri v2 desktop shell for cleanup-tools.
//!
//! Wraps the existing Flask approvals UI (`src/cleanup_tools/ui/`, frozen
//! by PyInstaller into a `--onedir` build, see
//! `packaging/pyinstaller/cleanup_ui.spec`) as a sidecar process and shows
//! it in a native window. See `frontend/loading.html` for the
//! poll-then-navigate half of this story; this file owns:
//!
//! - port-collision detection (is something already bound to
//!   127.0.0.1:5151, e.g. a terminal `cleanup approve`?) before ever
//!   spawning our own sidecar,
//! - spawning the sidecar in the background without blocking `setup()`,
//! - detecting an unexpected sidecar exit and surfacing an explicit error
//!   state in the webview (never a silent hang), and
//! - killing the sidecar cleanly on app quit.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Matches `LOCALHOST` in `src/cleanup_tools/ui/app.py` -- the Flask UI is
/// hard-bound to 127.0.0.1 only (see that file's `run_server` guard), so
/// this is the one host we ever probe or spawn against.
pub const SIDECAR_HOST: &str = "127.0.0.1";
/// Matches `DEFAULT_PORT` in `src/cleanup_tools/ui/app.py`.
pub const SIDECAR_PORT: u16 = 5151;
/// Must match the `externalBin`/capabilities entry name in
/// `tauri.conf.json` / `capabilities/default.json` (filename only, no
/// path, no target-triple suffix -- see `ShellExt::sidecar`'s doc comment
/// in tauri-plugin-shell).
const SIDECAR_NAME: &str = "cleanup-ui-sidecar";
/// Short and cheap on purpose: this fires on every app launch (including
/// the common case where nothing is listening yet and we're about to
/// spawn our own sidecar), so it must not make a normal cold start feel
/// slow while still giving a real, already-running instance enough time to
/// answer.
const HEALTHZ_PROBE_TIMEOUT: Duration = Duration::from_millis(800);

pub fn healthz_url(host: &str, port: u16) -> String {
    format!("http://{host}:{port}/healthz")
}

pub fn app_url(host: &str, port: u16) -> String {
    format!("http://{host}:{port}/")
}

/// Cheap liveness probe against the sidecar's `/healthz` route (see
/// `src/cleanup_tools/ui/routes.py`'s `healthz()` -- deliberately zero
/// queue/filesystem I/O on the Flask side).
///
/// Two independent callers of the same shape of check exist in this
/// story, on purpose:
/// - this Rust-side one, called once in `setup()` purely to decide
///   whether *we* need to spawn a sidecar at all (port-collision
///   detection), and
/// - `frontend/loading.html`'s own JS poll loop, which is what actually
///   decides when to navigate the webview to the real UI, and runs
///   regardless of which of the two of us spawned the process it's
///   waiting on.
///
/// Returns `true` only for an HTTP 2xx response received within
/// `timeout`; connection-refused, timeout, and non-2xx all mean "not up"
/// (or, pre-spawn, "nothing already listening on this port").
pub fn check_healthz(url: &str, timeout: Duration) -> bool {
    let config = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build();
    let agent: ureq::Agent = config.into();
    matches!(agent.get(url).call(), Ok(response) if response.status().is_success())
}

/// Shared handle to the sidecar child process, if this app instance
/// spawned one (it may not have -- see the port-collision path in
/// `spawn_sidecar_in_background`, which deliberately leaves this `None`
/// when an existing instance is already answering `/healthz`).
#[derive(Default, Clone)]
pub struct SidecarState {
    child: Arc<Mutex<Option<CommandChild>>>,
    /// Set right before we deliberately kill the child ourselves (app
    /// quit) so the `CommandEvent::Terminated` handler can tell "we did
    /// this on purpose" apart from "it crashed on its own" -- only the
    /// latter should navigate the webview to an error state.
    shutting_down: Arc<AtomicBool>,
}

impl SidecarState {
    pub fn store(&self, child: CommandChild) {
        *self.child.lock().unwrap() = Some(child);
    }

    pub fn begin_shutdown(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
    }

    /// Takes ownership of the tracked child (if any), leaving `None`
    /// behind. `CommandChild::kill` consumes `self` (it's not `&self`),
    /// so a shared reference alone can't kill it -- the caller needs
    /// ownership, taken out of the `Mutex` here.
    pub fn take(&self) -> Option<CommandChild> {
        self.child.lock().unwrap().take()
    }
}

/// Forces the webview to a bundled (not Flask-served) error state.
///
/// Deliberately uses `WebviewWindow::navigate` from the Rust side rather
/// than emitting a Tauri event for `frontend/loading.html`'s JS to listen
/// for: by the time a crash can happen, the webview has very likely
/// already navigated away from `loading.html` to the real Flask UI at
/// `http://127.0.0.1:5151/` (a different origin, where Tauri's JS APIs/
/// event listeners are not injected by default) -- so a JS-side listener
/// would silently stop working at exactly the moment it's needed.
/// `navigate()` works regardless of the webview's current page/origin.
fn navigate_to_error(app: &AppHandle, reason: &str) {
    if let Some(window) = app.get_webview_window("main") {
        let target = format!("tauri://localhost/loading.html?error={reason}");
        match tauri::Url::parse(&target) {
            Ok(url) => {
                if let Err(err) = window.navigate(url) {
                    eprintln!("cleanup-tools: failed to navigate to error state: {err}");
                }
            }
            Err(err) => eprintln!("cleanup-tools: failed to build error url: {err}"),
        }
    }
}

/// Port-collision check, then (only if needed) spawn the sidecar -- all on
/// a background OS thread so `setup()` returns immediately. This is
/// explicitly the most common Tauri mistake per this epic's research
/// brief: blocking `setup()` on sidecar readiness leaves the whole app
/// looking hung (no window, no Dock icon) for however long startup takes.
fn spawn_sidecar_in_background(app: AppHandle, state: SidecarState) {
    std::thread::spawn(move || {
        let healthz = healthz_url(SIDECAR_HOST, SIDECAR_PORT);

        if check_healthz(&healthz, HEALTHZ_PROBE_TIMEOUT) {
            // Something -- most likely a terminal `cleanup approve` -- is
            // already bound to 127.0.0.1:5151 and answering /healthz.
            // Do NOT spawn a second Flask process: it would fail to bind
            // the port anyway (see app.py's plain `app.run`), and
            // frontend/loading.html's own poll loop will see the healthy
            // endpoint and navigate straight to the existing instance on
            // its own, with no help needed from us here.
            eprintln!(
                "cleanup-tools: existing instance already answering on \
                 {SIDECAR_HOST}:{SIDECAR_PORT}, not spawning a sidecar"
            );
            return;
        }

        let sidecar_command = match app.shell().sidecar(SIDECAR_NAME) {
            Ok(cmd) => cmd,
            Err(err) => {
                eprintln!("cleanup-tools: failed to resolve sidecar {SIDECAR_NAME}: {err}");
                navigate_to_error(&app, "spawn_failed");
                return;
            }
        };

        let spawn_result = sidecar_command
            .args([SIDECAR_PORT.to_string(), SIDECAR_HOST.to_string()])
            .spawn();

        let (mut rx, child) = match spawn_result {
            Ok(pair) => pair,
            Err(err) => {
                eprintln!("cleanup-tools: failed to spawn sidecar: {err}");
                navigate_to_error(&app, "spawn_failed");
                return;
            }
        };

        eprintln!("cleanup-tools: spawned sidecar, pid={}", child.pid());
        state.store(child);

        // Drain the sidecar's stdout/stderr/lifecycle events for as long
        // as the process lives. This is also the crash-detection path:
        // an unexpected `CommandEvent::Terminated` (one we didn't cause
        // via `SidecarState::begin_shutdown`) means the sidecar died on
        // its own while the app was open, which must surface as an
        // explicit error state, never a silent hang or blank page.
        let app_for_events = app.clone();
        let state_for_events = state.clone();
        tauri::async_runtime::spawn(async move {
            while let Some(event) = rx.recv().await {
                match event {
                    CommandEvent::Terminated(payload) => {
                        if !state_for_events.is_shutting_down() {
                            eprintln!(
                                "cleanup-tools: sidecar exited unexpectedly \
                                 (code={:?}, signal={:?})",
                                payload.code, payload.signal
                            );
                            navigate_to_error(&app_for_events, "exited");
                        }
                        break;
                    }
                    CommandEvent::Error(err) => {
                        eprintln!("cleanup-tools: sidecar reported an error: {err}");
                    }
                    CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                        if let Ok(text) = String::from_utf8(bytes) {
                            eprintln!("cleanup-ui-sidecar: {text}");
                        }
                    }
                    _ => {}
                }
            }
        });
    });
}

// ─── icon picker ────────────────────────────────────────────────────────
//
// Lets the user swap the app's icon at runtime (Settings page in the
// Flask UI, `POST /settings/icon` -> `window.__TAURI__.core.invoke(...)`
// -> `apply_icon_choice` below). Two things happen on every choice:
//
// 1. Cross-platform: the *running* window/Dock/taskbar icon updates
//    immediately via `WebviewWindow::set_icon`. This alone is the entire
//    Windows/Linux implementation -- deliberately. A persistent Windows
//    Start-Menu-shortcut rewrite or a Linux `.desktop` `Icon=` rewrite
//    would need real test hardware this project doesn't have (the
//    existing Arch PKGBUILD is already flagged "reviewed, never
//    build-tested" for the same reason), so neither is attempted here.
// 2. macOS only: the actual installed `.app` bundle's Finder icon is
//    rewritten too, via `swap_macos_bundle_icon` below -- with a plain
//    filesystem copy first, falling back to an admin-prompted
//    `osascript`/`do shell script` only if that's denied (e.g. installed
//    under /Applications).
//
// `icon_choice` is persisted by the Python side in
// `~/.config/cleanup-tools/config.yaml` (see `src/cleanup_tools/config.py`)
// -- the one piece of state both languages need to read, unlike the
// theme picker (pure browser `localStorage`, no server/Rust visibility
// needed at all).

/// Valid icon-choice slugs. Must match `icon_choice`'s allowed values in
/// `src/cleanup_tools/config.py` and the folder names under
/// `icons/alternates/` (bundled via `tauri.conf.json`'s `bundle.resources`)
/// -- same cross-language "must match by hand" convention as
/// `SIDECAR_PORT` above.
const ICON_CHOICES: &[&str] = &[
    "broom-folder",
    "broom-sparkle",
    "tidy-folder-check",
    "recycle-folder",
];
/// Matches `Config.icon_choice`'s default in `src/cleanup_tools/config.py`
/// -- also what's baked into the build via `bundle.icon` in
/// `tauri.conf.json`, so re-applying it at startup would be a harmless
/// no-op skipped for that reason, not a correctness requirement.
const DEFAULT_ICON_CHOICE: &str = "broom-folder";

fn is_valid_icon_choice(choice: &str) -> bool {
    ICON_CHOICES.contains(&choice)
}

/// Resolves a file inside the bundled `icons/alternates/<choice>/`
/// resource tree for an already-validated choice.
fn alternate_icon_path(
    app: &AppHandle,
    choice: &str,
    filename: &str,
) -> Result<std::path::PathBuf, String> {
    app.path()
        .resource_dir()
        .map(|dir| {
            dir.join("icons")
                .join("alternates")
                .join(choice)
                .join(filename)
        })
        .map_err(|err| format!("failed to resolve resource dir: {err}"))
}

/// Cross-platform live icon update -- see the module-level doc comment
/// above for why this is the entire Windows/Linux implementation.
fn apply_live_icon(app: &AppHandle, choice: &str) -> Result<(), String> {
    let icon_path = alternate_icon_path(app, choice, "icon.png")?;
    let image = tauri::image::Image::from_path(&icon_path)
        .map_err(|err| format!("failed to load {}: {err}", icon_path.display()))?;
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    window
        .set_icon(image)
        .map_err(|err| format!("failed to set window icon: {err}"))
}

/// POSIX single-quote shell-argument quoting: wraps in `'...'`, escaping
/// any embedded `'` as `'\''`. Used only to build the one `cp <src> <dst>`
/// string handed to `osascript ... do shell script` in
/// `swap_macos_bundle_icon` -- kept as a small pure/testable function
/// rather than inlined so its escaping behavior has a unit test.
fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', r"'\''"))
}

/// Parses `PlistBuddy -c "Print :CFBundleIconFile"`'s stdout (just the
/// value, e.g. `"icon.icns\n"`) into the plain filename, or `None` for
/// empty/unreadable output. Pure and OS-independent on purpose -- it has
/// no macOS-specific behavior of its own, only its caller does.
fn parse_cfbundle_icon_file(plistbuddy_stdout: &str) -> Option<String> {
    let trimmed = plistbuddy_stdout.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Reads and validates `icon_choice` from
/// `~/.config/cleanup-tools/config.yaml` (see `src/cleanup_tools/config.py`
/// for the Python-side reader/writer). Tolerant of every other key in the
/// file -- deserializes into a loose `serde_yaml::Value` rather than a
/// full struct mirroring Python's `Config`, so this stays decoupled from
/// that schema's other fields. `None` for a missing file, unreadable YAML,
/// a missing/non-string `icon_choice` key, or a value outside
/// `ICON_CHOICES` -- all treated as "nothing to re-apply", not an error
/// worth surfacing.
fn read_icon_choice_from_config() -> Option<String> {
    let home = std::env::var("HOME").ok()?;
    let path = std::path::Path::new(&home)
        .join(".config")
        .join("cleanup-tools")
        .join("config.yaml");
    let contents = std::fs::read_to_string(path).ok()?;
    let value: serde_yaml::Value = serde_yaml::from_str(&contents).ok()?;
    let choice = value.get("icon_choice")?.as_str()?.to_string();
    is_valid_icon_choice(&choice).then_some(choice)
}

/// macOS-only: rewrites the *installed* `.app` bundle's Finder icon, not
/// just the live in-memory window icon `apply_live_icon` already handled.
/// Tries a plain filesystem copy first (works whenever the bundle is
/// user-writable, e.g. installed under `~/Applications`); falls back to
/// an admin-prompted `osascript ... do shell script ... with
/// administrator privileges` copy only if that's denied (e.g. installed
/// under `/Applications`, owned by another user). `touch`es the bundle
/// afterward to nudge Finder's icon cache -- a Finder relaunch may still
/// be needed for it to show up everywhere immediately; this deliberately
/// never force-quits the user's Finder to avoid that wait.
#[cfg(target_os = "macos")]
async fn swap_macos_bundle_icon(app: &AppHandle, choice: &str) -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|err| format!("current_exe: {err}"))?;
    let bundle_root = exe
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
        .ok_or_else(|| "could not resolve .app bundle root from current_exe".to_string())?;
    if bundle_root.extension().and_then(|ext| ext.to_str()) != Some("app") {
        // Not actually running from an installed .app bundle (e.g. `cargo
        // tauri dev`) -- nothing to swap, not an error.
        return Ok(());
    }

    let info_plist = bundle_root.join("Contents").join("Info.plist");
    let plist_output = app
        .shell()
        .command("/usr/libexec/PlistBuddy")
        .args([
            "-c",
            "Print :CFBundleIconFile",
            &info_plist.to_string_lossy(),
        ])
        .output()
        .await
        .map_err(|err| format!("PlistBuddy failed: {err}"))?;
    let icon_filename = parse_cfbundle_icon_file(&String::from_utf8_lossy(&plist_output.stdout))
        .ok_or_else(|| "could not read CFBundleIconFile from Info.plist".to_string())?;

    let dest = bundle_root
        .join("Contents")
        .join("Resources")
        .join(&icon_filename);
    let source = alternate_icon_path(app, choice, "icon.icns")?;

    if std::fs::copy(&source, &dest).is_err() {
        // Most likely PermissionDenied -- retry with an OS admin prompt.
        let cp_cmd = format!(
            "cp {} {}",
            shell_quote(&source.to_string_lossy()),
            shell_quote(&dest.to_string_lossy())
        );
        let script = format!(
            "do shell script {} with administrator privileges",
            shell_quote(&cp_cmd)
        );
        let result = app
            .shell()
            .command("osascript")
            .args(["-e", &script])
            .output()
            .await
            .map_err(|err| format!("osascript failed: {err}"))?;
        if !result.status.success() {
            return Err(format!(
                "elevated icon copy failed: {}",
                String::from_utf8_lossy(&result.stderr)
            ));
        }
    }

    let _ = app
        .shell()
        .command("touch")
        .args([bundle_root.to_string_lossy().to_string()])
        .output()
        .await;

    Ok(())
}

#[tauri::command]
async fn apply_icon_choice(app: AppHandle, choice: String) -> Result<(), String> {
    if !is_valid_icon_choice(&choice) {
        return Err(format!("unknown icon choice: {choice}"));
    }
    apply_live_icon(&app, &choice)?;
    #[cfg(target_os = "macos")]
    {
        swap_macos_bundle_icon(&app, &choice).await?;
    }
    Ok(())
}

// ─── update checker ─────────────────────────────────────────────────────
//
// Checks GitHub Releases (via `plugins.updater.endpoints` in
// tauri.conf.json, pointed at the stable `releases/latest/download/
// latest.json` alias so this never needs updating per release) for a
// newer signed build. Deliberately NOT silent/automatic end-to-end: a
// background check (on launch, then every `UPDATE_RECHECK_INTERVAL`)
// only ever *finds* an update and emits an `update-available` event for
// the UI to show a dismissible banner -- downloading and installing only
// happens from an explicit "Update now" click
// (`download_and_install_update`), never on its own.
//
// Custom commands wrapping the plugin's Rust API, not the
// `@tauri-apps/plugin-updater` JS package -- this project's frontend has
// no JS bundler at all (see `settings.js`/`apply_icon_choice` for the
// same reasoning), so every Tauri-side capability is exposed as a plain
// `#[tauri::command]` called via `window.__TAURI__.core.invoke(...)`.

use tauri_plugin_updater::UpdaterExt;

const UPDATE_RECHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);
const UPDATE_AVAILABLE_EVENT: &str = "update-available";

/// Holds the `Update` handle a successful check found, so a later
/// `download_and_install_update` call (a separate IPC round trip) can act
/// on it without re-checking. Mirrors `SidecarState`'s shape/purpose.
#[derive(Default)]
struct UpdaterState {
    pending: Mutex<Option<tauri_plugin_updater::Update>>,
}

#[derive(Clone, serde::Serialize)]
struct UpdateInfo {
    version: String,
    notes: Option<String>,
    pub_date: Option<String>,
}

impl From<&tauri_plugin_updater::Update> for UpdateInfo {
    fn from(update: &tauri_plugin_updater::Update) -> Self {
        UpdateInfo {
            version: update.version.clone(),
            notes: update.body.clone(),
            pub_date: update.date.map(|d| d.to_string()),
        }
    }
}

/// The actual check, shared by the command below and the background
/// launch/periodic loop. On a hit: stores the `Update` in `UpdaterState`
/// (overwriting any earlier one -- only the most recent check's result is
/// ever actionable) and returns its info; on a miss, clears any stale
/// pending update from a previous check that's no longer the latest.
async fn run_update_check(app: &AppHandle) -> Result<Option<UpdateInfo>, String> {
    let updater = app
        .updater()
        .map_err(|err| format!("updater unavailable: {err}"))?;
    let found = updater
        .check()
        .await
        .map_err(|err| format!("update check failed: {err}"))?;

    let info = found.as_ref().map(UpdateInfo::from);
    *app.state::<UpdaterState>().pending.lock().unwrap() = found;
    Ok(info)
}

#[tauri::command]
async fn check_for_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    run_update_check(&app).await
}

/// Returns whatever the most recent background/foreground check already
/// found, without making a new network call -- called once per page load
/// so a page opened *after* a background check already fired still shows
/// the banner (the `update-available` event itself only reaches listeners
/// that were already registered when it fired).
#[tauri::command]
fn get_pending_update(app: AppHandle) -> Option<UpdateInfo> {
    app.state::<UpdaterState>()
        .pending
        .lock()
        .unwrap()
        .as_ref()
        .map(UpdateInfo::from)
}

#[tauri::command]
async fn download_and_install_update(app: AppHandle) -> Result<(), String> {
    let update = app
        .state::<UpdaterState>()
        .pending
        .lock()
        .unwrap()
        .take()
        .ok_or_else(|| {
            "no update was found by a prior check -- call check_for_update first".to_string()
        })?;

    update
        .download_and_install(|_chunk_len, _total_len| {}, || {})
        .await
        .map_err(|err| format!("download/install failed: {err}"))?;

    // Re-enters setup()'s existing sidecar-spawn/port-collision logic on
    // relaunch exactly like a normal launch -- no special-casing needed
    // here.
    app.restart();
}

/// Background launch-time check plus a repeating re-check every
/// `UPDATE_RECHECK_INTERVAL` while the app stays open, each run on its
/// own OS thread so it never blocks `setup()` or the UI -- same pattern
/// as `spawn_sidecar_in_background`. A short initial delay lets the
/// sidecar/webview finish loading before the very first check's event
/// has anywhere to be heard.
fn spawn_update_check_loop(app: AppHandle) {
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(5));
        loop {
            let result = tauri::async_runtime::block_on(run_update_check(&app));
            match result {
                Ok(Some(info)) => {
                    if let Err(err) = app.emit(UPDATE_AVAILABLE_EVENT, info) {
                        eprintln!("cleanup-tools: failed to emit update-available event: {err}");
                    }
                }
                Ok(None) => {}
                Err(err) => eprintln!("cleanup-tools: update check failed: {err}"),
            }
            std::thread::sleep(UPDATE_RECHECK_INTERVAL);
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let sidecar_state = SidecarState::default();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .manage(sidecar_state.clone())
        .manage(UpdaterState::default())
        .invoke_handler(tauri::generate_handler![
            apply_icon_choice,
            check_for_update,
            get_pending_update,
            download_and_install_update
        ])
        .setup(move |app| {
            // Must return immediately -- see spawn_sidecar_in_background's
            // doc comment. The window is created before/around this
            // closure running; the loading screen (frontend/loading.html)
            // starts polling on its own timeline, independent of when (or
            // whether) this background thread finishes.
            spawn_sidecar_in_background(app.handle().clone(), sidecar_state.clone());

            // Launch-time + periodic update check -- see
            // spawn_update_check_loop's doc comment. Only ever finds and
            // announces an update; downloading/installing needs an
            // explicit "Update now" click in the UI.
            spawn_update_check_loop(app.handle().clone());

            // Re-apply a previously saved icon choice so the Dock/taskbar
            // icon matches it from the first frame, not just after the
            // next manual pick -- only the live-icon step needs to re-run
            // per launch, the macOS bundle-Resources copy already
            // persisted physically the last time it changed.
            if let Some(choice) = read_icon_choice_from_config() {
                if choice != DEFAULT_ICON_CHOICE {
                    if let Err(err) = apply_live_icon(&app.handle().clone(), &choice) {
                        eprintln!(
                            "cleanup-tools: failed to apply saved icon choice at startup: {err}"
                        );
                    }
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |app_handle, event| {
            // Empirically verified on this machine (real `cleanup-desktop`
            // build, quit via Cmd+Q, `ps`/`lsof` observed directly): a
            // normal macOS app quit does NOT emit `RunEvent::ExitRequested`
            // at all -- only a bare `RunEvent::Exit` fires, going straight
            // from `WindowEvent::Focused` events to `Exit` with nothing in
            // between. `ExitRequested` is (per Tauri's own docs) for the
            // "all windows just closed, should the app exit?" case, which
            // is a different path than the OS-level terminate a Cmd+Q/Dock
            // "Quit" sends. Relying on `ExitRequested` alone left the
            // sidecar running as an orphan after every normal quit (caught
            // by this project's own manual verification, not a
            // hypothetical) -- so shutdown is handled on BOTH events here;
            // `SidecarState::take()` makes a second call a harmless no-op
            // if both somehow fire for the same quit.
            let is_quit = matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            );
            if is_quit {
                // Clean shutdown on quit. For this project's chosen
                // PyInstaller `--onedir` sidecar build (see
                // packaging/pyinstaller/cleanup_ui.spec's docstring for
                // the measured rationale), the pid CommandChild::kill()
                // targets IS the real Flask/Werkzeug process -- there is
                // no PyInstaller-bootloader-vs-child indirection to worry
                // about (a `--onefile` sidecar would need a different,
                // more careful mitigation here; onedir does not).
                let state = app_handle.state::<SidecarState>();
                state.begin_shutdown();
                if let Some(child) = state.take() {
                    let pid = child.pid();
                    match child.kill() {
                        Ok(()) => {
                            eprintln!("cleanup-tools: sent kill to sidecar pid {pid} on quit")
                        }
                        Err(err) => eprintln!(
                            "cleanup-tools: failed to kill sidecar pid {pid} on quit: {err}"
                        ),
                    }
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    #[test]
    fn healthz_url_matches_flask_route() {
        assert_eq!(
            healthz_url("127.0.0.1", 5151),
            "http://127.0.0.1:5151/healthz"
        );
    }

    #[test]
    fn app_url_matches_flask_root() {
        assert_eq!(app_url("127.0.0.1", 5151), "http://127.0.0.1:5151/");
    }

    /// Spins up a throwaway TCP listener that answers exactly one request
    /// with a real (if minimal) HTTP response, so `check_healthz` can be
    /// exercised against something real without needing an actual Flask
    /// process or a Tauri window -- this is the "mock the healthz check"
    /// path called out for this story's Rust-side unit tests.
    fn spawn_fake_healthz_server(status_line: &'static str) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let addr = listener.local_addr().expect("local addr");
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = [0u8; 1024];
                let _ = stream.read(&mut buf);
                let body = b"{\"status\":\"ok\"}";
                let response = format!(
                    "{status_line}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes());
                let _ = stream.write_all(body);
                let _ = stream.flush();
            }
        });
        format!("http://{addr}/healthz")
    }

    #[test]
    fn check_healthz_true_on_200() {
        let url = spawn_fake_healthz_server("HTTP/1.1 200 OK");
        assert!(check_healthz(&url, Duration::from_secs(2)));
    }

    #[test]
    fn check_healthz_false_on_non_2xx() {
        let url = spawn_fake_healthz_server("HTTP/1.1 500 Internal Server Error");
        assert!(!check_healthz(&url, Duration::from_secs(2)));
    }

    #[test]
    fn check_healthz_false_on_connection_refused() {
        // Bind then immediately drop the listener to get a real ephemeral
        // port that is guaranteed to have nothing listening on it, rather
        // than hand-picking a fixed port number that might collide with
        // something else on the test machine.
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let addr = listener.local_addr().expect("local addr");
        drop(listener);

        let url = format!("http://{addr}/healthz");
        assert!(!check_healthz(&url, Duration::from_millis(500)));
    }

    #[test]
    fn sidecar_state_tracks_shutdown_intent() {
        let state = SidecarState::default();
        assert!(!state.is_shutting_down());
        state.begin_shutdown();
        assert!(state.is_shutting_down());
    }

    #[test]
    fn sidecar_state_take_returns_none_when_empty() {
        let state = SidecarState::default();
        assert!(state.take().is_none());
    }

    #[test]
    fn valid_icon_choices_are_accepted() {
        for choice in ICON_CHOICES {
            assert!(is_valid_icon_choice(choice));
        }
    }

    #[test]
    fn unknown_icon_choice_is_rejected() {
        assert!(!is_valid_icon_choice("not-a-real-choice"));
        assert!(!is_valid_icon_choice(""));
    }

    #[test]
    fn default_icon_choice_is_itself_valid() {
        assert!(is_valid_icon_choice(DEFAULT_ICON_CHOICE));
    }

    #[test]
    fn parse_cfbundle_icon_file_trims_plistbuddy_output() {
        assert_eq!(
            parse_cfbundle_icon_file("icon.icns\n"),
            Some("icon.icns".to_string())
        );
        assert_eq!(
            parse_cfbundle_icon_file("  icon  \n"),
            Some("icon".to_string())
        );
    }

    #[test]
    fn parse_cfbundle_icon_file_none_on_empty() {
        assert_eq!(parse_cfbundle_icon_file(""), None);
        assert_eq!(parse_cfbundle_icon_file("   \n"), None);
    }

    #[test]
    fn shell_quote_wraps_plain_paths() {
        assert_eq!(
            shell_quote("/Applications/Foo.app"),
            "'/Applications/Foo.app'"
        );
    }

    #[test]
    fn shell_quote_escapes_embedded_single_quotes() {
        // A pathologically-named path -- verifies the escaping is correct
        // even though real bundle/resource paths in this project never
        // contain a literal `'`.
        assert_eq!(shell_quote("it's/here"), r"'it'\''s/here'");
    }
}

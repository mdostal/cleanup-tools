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

use tauri::{AppHandle, Manager};
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let sidecar_state = SidecarState::default();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(sidecar_state.clone())
        .setup(move |app| {
            // Must return immediately -- see spawn_sidecar_in_background's
            // doc comment. The window is created before/around this
            // closure running; the loading screen (frontend/loading.html)
            // starts polling on its own timeline, independent of when (or
            // whether) this background thread finishes.
            spawn_sidecar_in_background(app.handle().clone(), sidecar_state.clone());
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
}

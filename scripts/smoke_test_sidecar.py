#!/usr/bin/env python3
"""Real, subprocess-driven smoke test for a frozen PyInstaller sidecar binary.

Unlike ``tests/test_ui_routes.py`` (which drives the Flask app in-process
via ``app.test_client()``), this script starts the ACTUAL frozen binary as
a real OS subprocess, talks to it over real HTTP on a real bound port, and
tears it down again -- the only way to catch PyInstaller-specific failure
modes that a pure in-process test client can never see: a missing
``datas=`` entry (``TemplateNotFound``/404 on a route that works fine from
source), a missing ``hiddenimports=`` entry (an ``ImportError`` deep in the
``anthropic``/``httpx``/``pydantic`` dependency tree that only surfaces
when that code path actually runs), or a process-lifecycle surprise (the
onefile bootloader-forks-a-child gotcha -- see ``kill_and_confirm_port_free``
below).

Not a plain ``pytest`` test because it needs a pre-built binary path, which
doesn't exist until someone has run PyInstaller (see
``packaging/pyinstaller/cleanup_ui.spec``). Usable two ways:

    python scripts/smoke_test_sidecar.py <path-to-binary> [--port N]

or, once a binary exists, opt into a real ``pytest`` run of the same
checks via the ``CLEANUP_TOOLS_SIDECAR_BINARY`` env var (see
``tests/test_smoke_sidecar.py``, which shells out to this script rather
than importing it, so no PyInstaller-only import path leaks into the
regular test-collection process).

Isolation: every subprocess launched here gets ``HOME`` pointed at a fresh
``tempfile.TemporaryDirectory()`` (via ``_isolated_env``), never the real
user's home -- ``OSAdapter.resolve_home()`` is ``Path.home()``, which
respects ``$HOME`` on POSIX, so this is enough to keep the frozen binary's
real approval queue / real Downloads folder / real AI credentials file
completely out of the loop, with zero code changes needed in
``entrypoint.py`` or ``app.py``. ``ANTHROPIC_API_KEY`` is explicitly
scrubbed from (or added to, depending on the scenario) each subprocess's
env rather than trusting whatever happens to be set in this script's own
environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5951  # deliberately not DEFAULT_PORT (5151) -- never collide
# with a real `cleanup approve` dev server someone might have running.
STARTUP_TIMEOUT_SECONDS = 30.0
TERMINATE_TIMEOUT_SECONDS = 10.0
FAKE_ANTHROPIC_API_KEY = "sk-ant-smoke-test-not-a-real-key-000000000000"


class SmokeTestFailure(AssertionError):
    """Raised (as a plain AssertionError subclass) for any smoke-test check
    that fails -- kept distinct so callers can tell "the sidecar itself is
    broken" apart from a bug in this script."""


# ---------------------------------------------------------------------------
# Small HTTP helpers. Deliberately stdlib-only (urllib) -- this script must
# run against a bare frozen binary with nothing else guaranteed installed,
# so it shouldn't need `requests` or any other third-party dependency.
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def location(self) -> str | None:
        return self.headers.get("Location")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every route this script asserts on that redirects (the 302s all
    over routes.py) needs to be inspected AS a 302 with its own Location
    header -- urllib's default opener silently follows redirects and hands
    back the final (200) response instead, which would make every 302
    assertion below false-positive-fail against a perfectly correct
    sidecar (see this file's own history: this was a real bug caught by
    actually running the script, not a hypothetical)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method: str, url: str, data: bytes | None = None, json_body: bool = False) -> _Response:
    headers = {}
    if json_body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return _Response(resp.status, resp.headers, resp.read())
    except urllib.error.HTTPError as exc:
        # A non-2xx/3xx status still carries a real response body/headers we
        # want to assert on (e.g. the 400 undo-nothing-to-revert case, or a
        # 302 now that redirects aren't auto-followed) -- urllib raises for
        # these rather than returning them, so unwrap.
        return _Response(exc.code, exc.headers, exc.read())


def _get(base_url: str, path: str) -> _Response:
    return _request("GET", base_url + path)


def _post(base_url: str, path: str, form: dict | None = None, json_data: dict | None = None) -> _Response:
    if json_data is not None:
        return _request("POST", base_url + path, data=json.dumps(json_data).encode(), json_body=True)
    data = urllib.parse.urlencode(form or {}, doseq=True).encode()
    return _request("POST", base_url + path, data=data)


# ---------------------------------------------------------------------------
# Port / process lifecycle helpers.
# ---------------------------------------------------------------------------


def _port_listening(port: int, host: str = DEFAULT_HOST) -> bool:
    """Whether something is bound+listening on ``host:port``, checked via a
    REAL connect attempt (mirrors what ``lsof -iTCP:<port> -sTCP:LISTEN``
    reports -- see this project's manual verification notes in the story
    report) rather than trusting subprocess/psutil process-list state,
    which is exactly what the onefile bootloader-PID gotcha would fool.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _lsof_listeners(port: int) -> str:
    """Best-effort ``lsof`` listing for ``port``, for diagnostics in failure
    messages. Never raises -- returns an explanatory string if ``lsof``
    isn't on PATH (e.g. some minimal Linux CI images)."""
    lsof = shutil.which("lsof")
    if not lsof:
        return "(lsof not available on PATH)"
    try:
        result = subprocess.run(
            [lsof, f"-iTCP:{port}", "-P"], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or "(no lsof output -- port appears free)"
    except Exception as exc:  # pragma: no cover - diagnostics only
        return f"(lsof failed: {exc})"


def wait_for_port(port: int, host: str = DEFAULT_HOST, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_listening(port, host):
            return
        time.sleep(0.2)
    raise SmokeTestFailure(
        f"Sidecar did not start accepting connections on {host}:{port} within {timeout}s"
    )


def kill_and_confirm_port_free(
    proc: subprocess.Popen, port: int, timeout: float = TERMINATE_TIMEOUT_SECONDS
) -> None:
    """Terminate ``proc`` and verify -- via a real connect probe, not just
    ``proc.poll()`` -- that the port it was bound to actually frees.

    This is the check that catches the PyInstaller onefile-bootloader
    gotcha: ``proc.pid`` for a onefile binary is the BOOTLOADER's pid, not
    the real Python/Flask process's pid. This project's manual
    investigation (see the story report) found that plain SIGTERM
    happens to get forwarded to the real child on this bootloader version/
    platform, but SIGKILL does NOT (SIGKILL is, by definition, never
    forwardable by any process) -- so a supervisor that escalates to
    SIGKILL against a onefile sidecar's reported pid can orphan the real
    server, leaving the port bound indefinitely. This function tries
    ``terminate()`` (SIGTERM) first, gives it ``timeout`` seconds, and
    only escalates to ``kill()`` (SIGKILL) if that didn't work -- then
    asserts the port is actually free, surfacing an ``lsof`` listing in
    the failure message if not (so an orphaned-child regression is loud,
    not a silent hang).
    """
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_listening(port):
            return
        time.sleep(0.2)

    raise SmokeTestFailure(
        f"Port {port} did not free within {timeout}s after terminating pid {proc.pid} "
        f"(possible orphaned child process -- see the onefile bootloader-PID gotcha in "
        f"this script's docstring). Current listeners:\n{_lsof_listeners(port)}"
    )


def _isolated_env(home_dir: Path, anthropic_api_key: str | None) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env.pop("ANTHROPIC_API_KEY", None)
    if anthropic_api_key is not None:
        env["ANTHROPIC_API_KEY"] = anthropic_api_key
    return env


def _seed_home(home_dir: Path) -> None:
    """Populate a fresh fake ``$HOME`` with exactly the files this smoke
    test's route sweep needs -- mirrors the fake-home pattern
    ``tests/test_ui_routes.py`` uses, just against a real filesystem
    instead of a fake adapter subclass."""
    from PIL import Image

    downloads = home_dir / "Downloads"
    downloads.mkdir(parents=True)
    documents = home_dir / "Documents"
    documents.mkdir(parents=True)

    # sort:photos -- single-entry approve/undo/thumbnail target. Filename
    # deliberately doesn't contain "screenshot" so it buckets as "photos",
    # not "screenshots".
    Image.new("RGB", (64, 48), (120, 40, 200)).save(downloads / "single_photo.jpg", format="JPEG")

    # sort:pdfs -- single-entry reject target, kept in its own bucket so it
    # can never be swept up by the sort:docs bulk-approve test below.
    (downloads / "single_reject.pdf").write_bytes(b"%PDF-1.4 fake pdf content")

    # sort:docs -- bulk-approve-by-group_key target (JSON body).
    (downloads / "bulk_doc_a.txt").write_text("doc a")
    (downloads / "bulk_doc_b.txt").write_text("doc b")

    # sort:other -- bulk-reject-by-group_key target (form body).
    (downloads / "bulk_other_a.dat").write_bytes(b"other a")
    (downloads / "bulk_other_b.dat").write_bytes(b"other b")

    # reclaim:os_junk -- single-entry approve target via /plan/reclaim.
    (documents / ".DS_Store").write_bytes(b"junk")

    # corral-screenshots -- single-entry staging target via
    # /plan/corral-screenshots. Deliberately placed under Desktop, not
    # Downloads/Documents: corral_screenshots' default roots are
    # Desktop+Downloads+Documents (see corral_screenshots.py's
    # _resolve_roots), but only Downloads is scanned by /plan/sort and
    # only Documents+Desktop's OS-junk patterns (.DS_Store/~$*/Thumbs.db)
    # are scanned by /plan/reclaim -- a `screenshot*.png` under Desktop
    # cannot be picked up by either of those and perturb their own staged
    # counts asserted elsewhere in this sweep.
    desktop = home_dir / "Desktop"
    desktop.mkdir(parents=True)
    (desktop / "screenshot-smoke-test.png").write_bytes(b"fake-screenshot-bytes")


def _poll_job(base_url: str, job_id: str, timeout: float = 30.0) -> dict:
    """Poll ``GET /status/<job_id>`` (see ``cleanup_tools.ui.jobs``) until it
    reports a terminal ``"done"``/``"error"`` status, mirroring
    ``tests/test_ui_routes.py``'s own ``_poll_job_until_terminal`` helper --
    ``/plan/reclaim`` now kicks off a background job and returns
    immediately rather than blocking, so this is the real-HTTP equivalent
    of that in-process polling loop.
    """
    deadline = time.monotonic() + timeout
    payload = None
    while time.monotonic() < deadline:
        resp = _get(base_url, f"/status/{job_id}")
        _check(resp.status == 200, f"GET /status/{job_id} expected 200 while polling, got {resp.status}")
        payload = json.loads(resp.text())
        if payload["status"] in ("done", "error"):
            return payload
        time.sleep(0.05)
    raise SmokeTestFailure(f"job {job_id} did not reach a terminal status within {timeout}s: {payload}")


def _entry_ids_by_src_suffix(queue_html: str) -> dict[str, str]:
    """Parse ``/queue``'s rendered HTML into ``{basename: entry_id}``.

    Mirrors the split-by-card technique ``tests/test_ui_routes.py`` uses
    (``html.split(f'data-entry-id="{id}"')[1].split("entry-card")[0]``),
    just walked in the other direction: given the HTML, find every
    ``data-entry-id`` and the ``src: <path>`` line that follows it within
    the same card. Non-greedy ``.*?`` with ``re.DOTALL`` stops at the
    FIRST "src: " after each id, which is always that same card's own src
    line (each card's src always appears before the next card even starts
    in queue.html's template), never a later card's.
    """
    pattern = re.compile(r'data-entry-id="([0-9a-f]+)".*?src: (?P<src>[^<\n]+)', re.DOTALL)
    result = {}
    for match in pattern.finditer(queue_html):
        entry_id = match.group(1)
        src = match.group("src").strip()
        result[Path(src).name] = entry_id
    return result


class Sidecar:
    """Context manager around one running frozen-binary subprocess."""

    def __init__(self, binary: Path, port: int, home_dir: Path, anthropic_api_key: str | None = None):
        self.binary = binary
        self.port = port
        self.home_dir = home_dir
        self.anthropic_api_key = anthropic_api_key
        self.proc: subprocess.Popen | None = None
        self.base_url = f"http://{DEFAULT_HOST}:{port}"
        self._log_path = home_dir / "sidecar_output.log"

    def __enter__(self) -> "Sidecar":
        env = _isolated_env(self.home_dir, self.anthropic_api_key)
        self._log_file = open(self._log_path, "wb")
        self.proc = subprocess.Popen(
            [str(self.binary), str(self.port)],
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            wait_for_port(self.port)
        except SmokeTestFailure:
            self._dump_log()
            # __exit__ is never called if __enter__ raises (the `with`
            # statement never considers itself entered), so this is the
            # only chance to reap a subprocess that started but never
            # opened its port -- otherwise a broken binary would leak a
            # hung process on every failed smoke-test run.
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            self._log_file.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                kill_and_confirm_port_free(self.proc, self.port)
            except SmokeTestFailure:
                self._dump_log()
                raise
            finally:
                self._log_file.close()
        elif self.proc is not None:
            self._log_file.close()

    def _dump_log(self) -> None:
        try:
            print("----- sidecar stdout/stderr -----", file=sys.stderr)
            print(self._log_path.read_text(errors="replace"), file=sys.stderr)
            print("----------------------------------", file=sys.stderr)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The route sweep itself.
# ---------------------------------------------------------------------------


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestFailure(message)


def run_main_route_sweep(binary: Path, port: int) -> None:
    """Exercise every route in the app (except the AI-key-present path for
    /propose-ai, covered separately by ``run_propose_ai_with_fake_key``)
    against one real running instance of the frozen binary, with
    ANTHROPIC_API_KEY deliberately unset so /propose-ai takes its
    CredentialsError degrade path -- a legitimate real code path, not a
    skip. Asserts against the same response shapes
    ``tests/test_ui_routes.py`` documents for the source-run app.
    """
    with tempfile.TemporaryDirectory(prefix="cleanup_tools_smoke_home_") as tmp:
        home_dir = Path(tmp)
        _seed_home(home_dir)

        with Sidecar(binary, port, home_dir, anthropic_api_key=None) as sidecar:
            base = sidecar.base_url

            # GET / -- empty queue before anything is staged.
            resp = _get(base, "/")
            _check(resp.status == 200, f"GET / expected 200, got {resp.status}")
            _check(b"Queue is empty" in resp.body, "GET / missing 'Queue is empty' on a fresh queue")

            # GET /static/keyboard.js -- proves the static datas= entry
            # made it into the frozen binary (not in the route list the
            # story explicitly enumerates, but the exact same
            # easy-to-forget datas= risk as the templates, so checked here
            # too).
            resp = _get(base, "/static/keyboard.js")
            _check(resp.status == 200, f"GET /static/keyboard.js expected 200, got {resp.status}")
            _check(b"keydown" in resp.body, "GET /static/keyboard.js body missing expected content")

            # GET /healthz -- deliberately cheap liveness check, no queue I/O.
            resp = _get(base, "/healthz")
            _check(resp.status == 200, f"GET /healthz expected 200, got {resp.status}")
            _check(resp.text().strip() == '{"status":"ok"}' or '"status"' in resp.text(), f"GET /healthz unexpected body: {resp.text()}")

            # GET /settings + POST /settings/icon -- proves settings.html,
            # static/settings.js, and static/icon-choices/*.png all made it
            # into the frozen binary's datas= (see cleanup_ui.spec), the
            # same class of easy-to-forget-a-file risk /static/keyboard.js
            # above already guards against.
            resp = _get(base, "/settings")
            _check(resp.status == 200, f"GET /settings expected 200, got {resp.status}")
            _check(b"icon-choice-grid" in resp.body, "GET /settings missing icon-choice-grid")
            _check(b"broom-folder" in resp.body, "GET /settings missing the broom-folder icon choice")

            resp = _get(base, "/static/settings.js")
            _check(resp.status == 200, f"GET /static/settings.js expected 200, got {resp.status}")

            resp = _get(base, "/static/icon-choices/broom-folder.png")
            _check(resp.status == 200, f"GET /static/icon-choices/broom-folder.png expected 200, got {resp.status}")

            resp = _post(base, "/settings/icon", json_data={"choice": "recycle-folder"})
            _check(resp.status == 200, f"POST /settings/icon expected 200, got {resp.status}")
            _check(
                json.loads(resp.text()).get("choice") == "recycle-folder",
                f"POST /settings/icon unexpected body: {resp.text()}",
            )

            resp = _post(base, "/settings/icon", json_data={"choice": "not-a-real-choice"})
            _check(resp.status == 400, f"POST /settings/icon (invalid) expected 400, got {resp.status}")

            # GET /plan/sort -- kicks off a background job (see
            # cleanup_tools.ui.jobs) and returns {"job_id": ...} immediately
            # rather than blocking/redirecting; stages 6 move entries from
            # the seeded Downloads. Mirrors /plan/reclaim's polling below.
            resp = _get(base, "/plan/sort")
            _check(resp.status == 200, f"GET /plan/sort expected 200 (job_id), got {resp.status}")
            job_id = json.loads(resp.text())["job_id"]
            _check(bool(job_id), "GET /plan/sort returned an empty job_id")

            job_payload = _poll_job(base, job_id)
            _check(job_payload["status"] == "done", f"/plan/sort background job did not finish cleanly: {job_payload}")
            _check(job_payload["result"]["count"] == 6, f"/plan/sort job result expected count=6, got {job_payload['result']}")

            # GET /plan/reclaim -- kicks off a background job (see
            # cleanup_tools.ui.jobs) and returns {"job_id": ...} immediately
            # rather than blocking/redirecting; poll /status/<job_id> until
            # it reaches a terminal state, mirroring
            # tests/test_ui_routes.py's _poll_job_until_terminal.
            resp = _get(base, "/plan/reclaim")
            _check(resp.status == 200, f"GET /plan/reclaim expected 200 (job_id), got {resp.status}")
            job_id = json.loads(resp.text())["job_id"]
            _check(bool(job_id), "GET /plan/reclaim returned an empty job_id")

            job_payload = _poll_job(base, job_id)
            _check(job_payload["status"] == "done", f"/plan/reclaim background job did not finish cleanly: {job_payload}")
            _check(job_payload["result"]["count"] == 1, f"/plan/reclaim job result expected count=1, got {job_payload['result']}")

            # GET /status/<unknown> -- 404, not 500.
            resp = _get(base, "/status/this-job-id-does-not-exist")
            _check(resp.status == 404, f"GET /status/<unknown> expected 404, got {resp.status}")

            # GET /queue -- all 7 entries pending; parse ids by src basename.
            resp = _get(base, "/queue")
            _check(resp.status == 200, f"GET /queue expected 200, got {resp.status}")
            _check("(7 pending)" in resp.text(), f"GET /queue expected 7 pending entries in heading")
            ids = _entry_ids_by_src_suffix(resp.text())
            for expected in (
                "single_photo.jpg",
                "single_reject.pdf",
                "bulk_doc_a.txt",
                "bulk_doc_b.txt",
                "bulk_other_a.dat",
                "bulk_other_b.dat",
                ".DS_Store",
            ):
                _check(expected in ids, f"/queue did not list an entry for seeded file {expected!r}")

            photo_id = ids["single_photo.jpg"]
            reject_id = ids["single_reject.pdf"]
            reclaim_id = ids[".DS_Store"]

            # POST /queue/<id>/approve
            resp = _post(base, f"/queue/{photo_id}/approve")
            _check(resp.status == 302, f"approve expected 302, got {resp.status}")

            # POST /queue/<id>/undo -- freshly-staged entry has empty
            # status_history, so there's nothing to revert to yet: 400, not
            # 500 (mirrors test_undo_with_nothing_to_revert_returns_400_not_500).
            resp = _post(base, f"/queue/{photo_id}/undo")
            _check(resp.status == 400, f"undo-nothing-to-revert expected 400, got {resp.status}")

            # GET /thumbnail/<id> -- the approved photo's file is still on
            # disk (staging never moves files), and still classified as an
            # image entry regardless of its approved status.
            resp = _get(base, f"/thumbnail/{photo_id}")
            _check(resp.status == 200, f"GET /thumbnail expected 200, got {resp.status}")
            _check(
                resp.headers.get("Content-Type", "").startswith("image/jpeg"),
                f"GET /thumbnail expected image/jpeg, got {resp.headers.get('Content-Type')}",
            )

            # POST /queue/<id>/reject
            resp = _post(base, f"/queue/{reject_id}/reject")
            _check(resp.status == 302, f"reject expected 302, got {resp.status}")

            # POST /queue/bulk-approve (JSON body, group_key) -- sort:docs.
            resp = _post(base, "/queue/bulk-approve", json_data={"group_key": "sort:docs"})
            _check(resp.status == 200, f"bulk-approve(json) expected 200, got {resp.status}")
            body = json.loads(resp.text())
            _check(body.get("count") == 2, f"bulk-approve(json) expected count=2, got {body}")
            _check(body.get("status") == "approved", f"bulk-approve(json) unexpected status: {body}")

            # POST /queue/bulk-reject (form body, group_key) -- sort:other.
            resp = _post(base, "/queue/bulk-reject", form={"group_key": "sort:other"})
            _check(resp.status == 302, f"bulk-reject(form) expected 302, got {resp.status}")

            # Only the reclaim entry should still be pending.
            resp = _get(base, "/queue")
            _check("(1 pending)" in resp.text(), f"expected exactly 1 pending entry remaining, got: {resp.text()[:200]}")

            resp = _post(base, f"/queue/{reclaim_id}/approve")
            _check(resp.status == 302, f"reclaim approve expected 302, got {resp.status}")

            # POST /propose-ai with NO API key configured anywhere --
            # CredentialsError degrade path, a real code path (not a skip).
            resp = _post(base, "/propose-ai")
            _check(resp.status == 302, f"propose-ai (no key) expected 302, got {resp.status}")
            location = resp.location() or ""
            _check("plan_error" in location, f"propose-ai (no key) expected plan_error in Location, got {location}")
            _check(
                "No+Anthropic+API+key" in location or "No Anthropic API key" in urllib.parse.unquote(location),
                f"propose-ai (no key) Location didn't carry the expected CredentialsError message: {location}",
            )

            # GET /plan/corral-screenshots -- same background-job shape as
            # /plan/sort above (see routes.py's plan_corral_screenshots).
            # Stages the single seeded Desktop screenshot from _seed_home;
            # run down here, after every other route's own staged/pending
            # -count assertions, so this doesn't perturb them.
            resp = _get(base, "/plan/corral-screenshots")
            _check(resp.status == 200, f"GET /plan/corral-screenshots expected 200 (job_id), got {resp.status}")
            job_id = json.loads(resp.text())["job_id"]
            _check(bool(job_id), "GET /plan/corral-screenshots returned an empty job_id")

            job_payload = _poll_job(base, job_id)
            _check(
                job_payload["status"] == "done",
                f"/plan/corral-screenshots background job did not finish cleanly: {job_payload}",
            )
            _check(
                job_payload["result"]["count"] == 1,
                f"/plan/corral-screenshots job result expected count=1, got {job_payload['result']}",
            )

    print("[smoke] main route sweep: PASS (all routes exercised, no key configured)")


def run_propose_ai_with_fake_key(binary: Path, port: int) -> None:
    """Separate subprocess run, dedicated to /propose-ai with a (fake, but
    present) ANTHROPIC_API_KEY -- this is the check that actually exercises
    the full anthropic -> httpx -> pydantic import/call chain PyInstaller's
    static import scanner is most likely to have missed something for (see
    the story report's self-review). The key is a syntactically-plausible
    but definitely-invalid string, so the real Anthropic API rejects it
    with a genuine 401 over a genuine HTTPS request -- proving every layer
    (TLS via httpx/httpcore/certifi, request construction, and response
    parsing via pydantic) imported and ran successfully under the frozen
    binary, without needing a real API key or mocking anything.

    If ANY hidden import were missing, this would surface as a raw 500 (an
    unhandled ImportError propagating out of get_provider()/propose_bucket()
    -- neither is wrapped in a broad except), not the expected 302 -- so
    the status-code assertion below IS the real assertion, not a formality.
    """
    with tempfile.TemporaryDirectory(prefix="cleanup_tools_smoke_home_ai_") as tmp:
        home_dir = Path(tmp)
        downloads = home_dir / "Downloads"
        downloads.mkdir(parents=True)
        (downloads / "ambiguous_thing.xyz").write_bytes(b"content that matches no bucket rule")

        with Sidecar(binary, port, home_dir, anthropic_api_key=FAKE_ANTHROPIC_API_KEY) as sidecar:
            base = sidecar.base_url
            resp = _post(base, "/propose-ai")
            _check(
                resp.status == 302,
                f"propose-ai (fake key) expected 302 (proves anthropic/httpx/pydantic imported and ran "
                f"under the frozen binary), got {resp.status}. Body: {resp.text()[:2000]}",
            )
            location = resp.location() or ""
            _check(
                "AI proposal(s) failed" in urllib.parse.unquote(location) or "plan_error" in location,
                f"propose-ai (fake key) expected an auth-failure plan_error in Location, got: {location}",
            )

    print("[smoke] propose-ai with fake ANTHROPIC_API_KEY: PASS (anthropic/httpx/pydantic import chain resolved)")


def run_smoke_test(binary: str | Path, port: int = DEFAULT_PORT) -> None:
    binary = Path(binary).resolve()
    _check(binary.exists(), f"binary not found: {binary}")
    _check(os.access(binary, os.X_OK), f"binary is not executable: {binary}")

    run_main_route_sweep(binary, port)
    # Distinct port for the second subprocess so there's no chance of
    # racing the first one's port-free teardown check.
    run_propose_ai_with_fake_key(binary, port + 1)

    print(f"[smoke] ALL CHECKS PASSED for {binary}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", help="Path to the frozen PyInstaller binary to test")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Base port to use (default {DEFAULT_PORT})")
    args = parser.parse_args()

    try:
        run_smoke_test(args.binary, args.port)
    except SmokeTestFailure as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

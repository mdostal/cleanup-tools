"""Flask app factory and localhost-only server runner for the approvals UI.

``create_app`` builds a plain, testable Flask app (no server binding) so
:mod:`tests.test_ui_routes` can drive it entirely through
``app.test_client()``. ``run_server`` is the thing the ``cleanup approve``
CLI subcommand actually calls: it wraps ``create_app`` and then binds the
real Werkzeug dev server -- to ``127.0.0.1`` ONLY, never ``0.0.0.0`` or any
other interface, since this UI can approve destructive filesystem actions
and must never be reachable from another machine on the network. ``host`` is
hardcoded and additionally guarded by an explicit check, so a future edit
that adds a ``--host`` flag (or otherwise threads a caller-supplied host
through) can't silently widen the bind without also touching this check.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from flask import Flask

from ..adapters.base import OSAdapter
from .routes import bp as ui_blueprint

DEFAULT_PORT = 5151
LOCALHOST = "127.0.0.1"


def create_app(adapter: OSAdapter, queue_path: Path | None = None) -> Flask:
    """Build the Flask app, stashing ``adapter``/``queue_path`` for routes.py.

    ``queue_path`` of ``None`` means "use ``queue.default_queue_path(adapter)``,
    resolved fresh on every request" (see routes.py's ``_queue_path``) rather
    than a path frozen at app-creation time -- this matters for tests, which
    construct the app once but may want every request to see the current
    default path for a given fake-home adapter.
    """
    app = Flask(__name__)
    app.config["CLEANUP_ADAPTER"] = adapter
    app.config["CLEANUP_QUEUE_PATH"] = queue_path
    app.register_blueprint(ui_blueprint)
    return app


def run_server(
    adapter: OSAdapter,
    queue_path: Path | None = None,
    host: str = LOCALHOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> dict:
    """Start the approvals UI, bound to ``127.0.0.1`` only, and block.

    Raises ``ValueError`` if ``host`` is anything other than ``127.0.0.1`` --
    this function is the single choke point between "an OSAdapter and a
    port number" and an actual open socket, so the localhost-only guarantee
    lives here rather than trusting every future caller to pass the right
    value.

    Opens the default web browser to the UI's URL (via the stdlib
    ``webbrowser`` module) before starting the server, unless
    ``open_browser`` is False -- callers that just want the server bound
    (e.g. the lsof/netstat verification in tests, or a headless CI run) can
    opt out without a real browser window popping up.

    Blocks for as long as the server runs (i.e. until interrupted); returns
    a small dict describing where it was bound, once it stops.
    """
    if host != LOCALHOST:
        raise ValueError(
            f"The approvals UI must bind to {LOCALHOST!r} only; got {host!r}."
        )

    app = create_app(adapter, queue_path)

    if open_browser:
        webbrowser.open(f"http://{host}:{port}/")

    app.run(host=host, port=port, debug=False, use_reloader=False)

    return {"host": host, "port": port}

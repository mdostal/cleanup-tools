"""PyInstaller entrypoint for the approvals UI Flask sidecar.

This is what ``Analysis()`` in ``cleanup_ui.spec`` targets as the frozen
binary's ``__main__``. Its only job is to build a real :class:`OSAdapter`
(via the existing ``get_adapter()`` factory, same as the ``cleanup`` CLI
uses) and start the approvals UI server with ``open_browser=False`` -- the
native desktop shell (a separate piece of work) is responsible for opening
its own window/webview pointed at the bound host:port, so this sidecar must
never pop a real OS browser window itself.

``port``/``host`` are optionally overridable via CLI args (``sys.argv[1]``/
``sys.argv[2]``) purely so multiple frozen binaries (e.g. an onefile build
and an onedir build under test) can be run side by side without a port
collision -- the native shell is not expected to pass these in normal
operation and can rely on the defaults.
"""

from __future__ import annotations

import sys

from cleanup_tools.adapters import get_adapter
from cleanup_tools.ui.app import DEFAULT_PORT, LOCALHOST, run_server


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    host = sys.argv[2] if len(sys.argv) > 2 else LOCALHOST

    adapter = get_adapter()
    run_server(adapter, host=host, port=port, open_browser=False)


if __name__ == "__main__":
    main()

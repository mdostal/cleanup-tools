"""Helper for make-latest-json.sh -- see that script for the full "why".

Not meant to be run directly; called with exactly 5 positional args by the
shell wrapper, which resolves paths and finds the .sig file first.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Must match tauri-plugin-updater's OS-ARCH platform-key convention. This
# project only ships a macOS Apple Silicon build today (see
# tauri.conf.json's bundle.targets and README's "Not in this release"
# callout on v0.1.0) -- add an entry here once a second platform target
# actually exists, not before.
PLATFORM_KEY = "darwin-aarch64"


def main() -> None:
    repo_root, sig_path, asset_url, notes, output_path = sys.argv[1:6]

    conf_path = Path(repo_root) / "src-tauri" / "tauri.conf.json"
    version = json.loads(conf_path.read_text())["version"]

    signature = Path(sig_path).read_text().strip()

    manifest = {
        "version": version,
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            PLATFORM_KEY: {
                "signature": signature,
                "url": asset_url,
            }
        },
    }

    Path(output_path).write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"make-latest-json: wrote {output_path} for version {version}")


if __name__ == "__main__":
    main()

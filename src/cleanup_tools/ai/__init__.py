"""AI-provider factory.

Standalone module, not wired into the queue/UI/CLI (see ``base.py`` for the
full "why"). Callers should go through :func:`get_provider` rather than
constructing :class:`AnthropicProvider` directly, mirroring the
``adapters.get_adapter()`` factory pattern used elsewhere in this codebase.

Key resolution order
---------------------
1. ``ANTHROPIC_API_KEY`` environment variable, if set to a non-empty value.
2. A dedicated credentials file at ``~/.config/cleanup-tools/credentials``.

That credentials file is deliberately its own file -- NEVER ``config.yaml``
(``cleanup_tools.config``) and NEVER the approval-queue file
(``cleanup_tools.queue``). Those two exist for unrelated concerns (bucket
rules / search roots, and the proposed-actions queue, respectively) and
secrets don't belong in either: both are plain YAML meant to be read,
diffed, and occasionally hand-edited, and a stray "add my API key here too"
edit to one of them would be an easy way to leak a secret into a file that
isn't permission-locked down and might get committed, backed up, or synced
by accident.

Credentials file format: plain UTF-8 text; the entire (whitespace-trimmed)
file content is the API key. No YAML, no key=value parsing -- one secret,
one file, nothing to accidentally serialize alongside it.

Permission enforcement
-----------------------
The credentials file MUST be 0600 (owner read/write only). Decision: if it
exists but has looser permissions, we **correct the mode automatically and
emit a warning** rather than refusing outright. This is a personal,
single-user tool (see the project's own north star), not a shared or
multi-tenant system -- the realistic threat here is "another local account
on the same Mac can read the file," which correcting the mode fixes
immediately, and refusing to run would just block the user's own workflow
over a metadata slip (e.g. a permissive umask, or the file having been
restored from a backup that didn't preserve modes) that has an obvious,
safe, one-line fix. The warning still makes the correction visible instead
of silently rewriting permissions with no trace.
"""

from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path

from .anthropic_provider import AnthropicProvider
from .base import AIProvider, ProposalKind, ProposalResult

__all__ = [
    "AIProvider",
    "AnthropicProvider",
    "ProposalKind",
    "ProposalResult",
    "CredentialsError",
    "default_credentials_path",
    "get_provider",
]

CREDENTIALS_FILENAME = "credentials"
_REQUIRED_MODE = 0o600


class CredentialsError(RuntimeError):
    """Raised when no usable Anthropic credential can be resolved."""


def default_credentials_path() -> Path:
    """Return ``~/.config/cleanup-tools/credentials``.

    Intentionally resolves ``Path.home()`` directly rather than routing
    through ``cleanup_tools.adapters.OSAdapter`` -- this module is meant to
    stand alone, and pulling in the adapter/OS-detection machinery here
    would add a dependency this factory doesn't need (nothing below reads
    or writes files any way ``OSAdapter.resolve_home()`` wouldn't).
    """
    return Path.home() / ".config" / "cleanup-tools" / CREDENTIALS_FILENAME


def _enforce_credentials_file_permissions(path: Path) -> None:
    """Ensure ``path`` is mode 0600, correcting and warning if it is not.

    See the module docstring's "Permission enforcement" section for why
    correct-then-warn was chosen over refusing outright.
    """
    current_mode = stat.S_IMODE(path.stat().st_mode)
    if current_mode != _REQUIRED_MODE:
        os.chmod(path, _REQUIRED_MODE)
        warnings.warn(
            f"Credentials file {path} had permissions {oct(current_mode)}; "
            f"corrected to {oct(_REQUIRED_MODE)}. Store secrets in files only "
            "your user account can read.",
            stacklevel=3,
        )


def _read_api_key_from_credentials_file(path: Path) -> str:
    _enforce_credentials_file_permissions(path)
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise CredentialsError(f"Credentials file {path} is empty")
    return content


def _resolve_api_key(credentials_path: Path | None = None) -> str:
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    path = credentials_path if credentials_path is not None else default_credentials_path()
    if not path.exists():
        raise CredentialsError(
            "No Anthropic API key found. Set the ANTHROPIC_API_KEY environment "
            f"variable, or create {path} (mode 0600) containing the key."
        )
    return _read_api_key_from_credentials_file(path)


def get_provider(
    model: str | None = None,
    *,
    credentials_path: Path | None = None,
) -> AIProvider:
    """Return a configured :class:`AIProvider`.

    Resolves the API key per the module-level "Key resolution order" above.
    Raises :class:`CredentialsError` if no key can be found anywhere.
    ``credentials_path`` overrides the default file location (primarily for
    tests); ``model`` overrides the provider's default model.
    """
    api_key = _resolve_api_key(credentials_path)
    kwargs: dict = {}
    if model is not None:
        kwargs["model"] = model
    return AnthropicProvider(api_key=api_key, **kwargs)

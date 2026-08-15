"""Bucket-rule and persisted configuration for cleanup-tools.

``DEFAULT_BUCKET_RULES`` reproduces the ``bucket()`` case statement from
``scripts/sort-downloads.sh`` exactly, so behavior stays identical when the
shell script's logic gets ported into Python call sites.

Configuration can optionally be persisted to a YAML file (see
``load_config``/``save_config``) so that user-supplied bucket rules,
search roots, and tracked "master path" backups survive across runs.
"""

from __future__ import annotations

import yaml

from dataclasses import dataclass, field
from pathlib import Path

from .adapters._util import matches_pattern
from .adapters.base import OSAdapter


@dataclass(frozen=True)
class BucketRule:
    """A single "these extensions (and optionally this filename glob) go to
    this bucket" rule."""

    extensions: frozenset[str]
    bucket: str
    filename_pattern: str | None = None


@dataclass(frozen=True)
class MasterPath:
    """A path tracked as an authoritative copy, plus whether it has been
    backed up elsewhere."""

    path: str
    backed_up: bool = False


@dataclass
class Config:
    bucket_rules: list[BucketRule]
    search_roots: list[str] = field(default_factory=list)
    master_paths: list[MasterPath] = field(default_factory=list)
    # One of the slugs in ``cleanup_tools.ui.routes.ICON_CHOICES`` -- that
    # module owns validating the allowed set (and the desktop shell's
    # ``ICON_CHOICES`` in ``src-tauri/src/lib.rs`` mirrors it by hand, same
    # "must match" convention as that file's ``SIDECAR_PORT``). This field
    # is just a plain string: config.py doesn't know or enforce the set.
    icon_choice: str = "broom-folder"


# Order matters: rules are checked in sequence and the first match wins, so
# the "screenshot*" rule must precede the general photos rule for the same
# extensions.
DEFAULT_BUCKET_RULES: list[BucketRule] = [
    BucketRule(
        extensions=frozenset({"png", "jpg", "jpeg", "heic", "gif", "webp", "tiff"}),
        bucket="screenshots",
        filename_pattern="screenshot*",
    ),
    BucketRule(
        extensions=frozenset({"png", "jpg", "jpeg", "heic", "gif", "webp", "tiff"}),
        bucket="photos",
    ),
    BucketRule(
        extensions=frozenset({"dmg", "pkg"}),
        bucket="installers",
    ),
    BucketRule(
        extensions=frozenset({"mp4", "mov", "m4v", "avi", "mkv"}),
        bucket="videos",
    ),
    BucketRule(
        extensions=frozenset({"pdf"}),
        bucket="pdfs",
    ),
    BucketRule(
        extensions=frozenset({"zip", "tar", "gz", "7z", "rar"}),
        bucket="archives",
    ),
    BucketRule(
        extensions=frozenset({"csv", "json", "xlsx", "xml", "tsv", "numbers"}),
        bucket="data",
    ),
    BucketRule(
        extensions=frozenset({"doc", "docx", "ppt", "pptx", "txt", "md", "pages", "key"}),
        bucket="docs",
    ),
]


def resolve_bucket(filename: str, rules: list[BucketRule]) -> str:
    """Return the bucket ``filename`` sorts into per ``rules``.

    Walks ``rules`` in order and returns the first match's ``bucket``. A
    rule matches when the file's lowercased extension is in
    ``rule.extensions`` and, if ``rule.filename_pattern`` is set, the
    lowercased filename also matches that pattern. Falls through to
    ``"other"`` if nothing matches.

    Extension extraction mirrors bash's ``ext="${b##*.}"`` from
    ``scripts/sort-downloads.sh`` exactly: when the filename contains no
    ``.``, that parameter expansion is a no-op and leaves ``ext`` as the
    *whole* filename, not an empty string. So an extension-less file named
    e.g. ``csv`` is treated as having "extension" ``csv`` (and therefore
    still lands in the ``data`` bucket), matching the shell script's
    behavior bug-for-bug.
    """
    name_lower = filename.lower()
    if "." in name_lower:
        _, _, ext = name_lower.rpartition(".")
    else:
        ext = name_lower

    for rule in rules:
        if ext not in rule.extensions:
            continue
        if rule.filename_pattern is not None:
            if not matches_pattern(name_lower, rule.filename_pattern.lower()):
                continue
        return rule.bucket

    return "other"


DEFAULT_CONFIG = Config(bucket_rules=DEFAULT_BUCKET_RULES)

CONFIG_FILENAME = "config.yaml"


def default_config_path(adapter: OSAdapter) -> Path:
    """Return the default config file path: ``~/.config/cleanup-tools/config.yaml``."""
    return adapter.resolve_home() / ".config" / "cleanup-tools" / CONFIG_FILENAME


def _bucket_rule_from_dict(data: dict) -> BucketRule:
    return BucketRule(
        extensions=frozenset(data["extensions"]),
        bucket=data["bucket"],
        filename_pattern=data.get("filename_pattern"),
    )


def _master_path_from_dict(data: dict) -> MasterPath:
    return MasterPath(
        path=data["path"],
        backed_up=data.get("backed_up", False),
    )


def load_config(adapter: OSAdapter, path: Path | None = None) -> Config:
    """Load ``Config`` from ``path`` (default: ``default_config_path``).

    If the file does not exist, returns ``DEFAULT_CONFIG`` unchanged. If it
    exists but is not valid YAML (or does not contain a mapping at the top
    level), raises a ``ValueError`` naming the file and the parse problem
    rather than silently falling back to defaults. A null value for
    ``bucket_rules``/``search_roots``/``master_paths`` (an empty YAML key,
    e.g. ``search_roots:`` with nothing after it) is treated the same as the
    key being absent entirely, rather than crashing while iterating ``None``.
    A ``bucket_rules``/``master_paths`` entry missing a required key (e.g. a
    rule with no ``bucket``) raises the same file-naming ``ValueError`` as
    malformed YAML, instead of a raw ``KeyError``.

    User-supplied ``bucket_rules`` are prepended *before* the defaults, so
    user rules get first-match priority while the defaults still apply as a
    fallback. ``search_roots`` and ``master_paths`` from the file replace
    the (empty) defaults directly.

    If the loaded ``bucket_rules`` already end with the full
    ``DEFAULT_BUCKET_RULES`` sequence (i.e. this file was produced by
    ``save_config``, which always writes user rules followed by the
    defaults), that trailing default block is stripped back off before
    re-appending ``DEFAULT_BUCKET_RULES``. Without this, a load/save round
    trip would re-prepend the same defaults as "user rules" every cycle and
    the rule list would grow without bound.
    """
    if path is None:
        path = default_config_path(adapter)

    if not path.exists():
        return DEFAULT_CONFIG

    raw = adapter.read_file(path)
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse config file {path}: {exc}") from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Failed to parse config file {path}: expected a YAML mapping at "
            f"the top level, got {type(parsed).__name__}"
        )

    try:
        parsed_rules = [
            _bucket_rule_from_dict(r) for r in (parsed.get("bucket_rules") or [])
        ]
    except KeyError as exc:
        raise ValueError(
            f"Failed to parse config file {path}: bucket_rules entry missing "
            f"required key {exc}"
        ) from exc

    num_defaults = len(DEFAULT_BUCKET_RULES)
    if (
        len(parsed_rules) >= num_defaults
        and parsed_rules[len(parsed_rules) - num_defaults :] == DEFAULT_BUCKET_RULES
    ):
        # The file already contains the full default set (most likely
        # written by save_config); don't treat it as user-authored rules
        # to prepend again.
        user_rules = parsed_rules[: len(parsed_rules) - num_defaults]
    else:
        user_rules = parsed_rules

    bucket_rules = user_rules + DEFAULT_BUCKET_RULES

    search_roots = list(parsed.get("search_roots") or [])

    try:
        master_paths = [
            _master_path_from_dict(m) for m in (parsed.get("master_paths") or [])
        ]
    except KeyError as exc:
        raise ValueError(
            f"Failed to parse config file {path}: master_paths entry missing "
            f"required key {exc}"
        ) from exc

    icon_choice = parsed.get("icon_choice") or "broom-folder"

    return Config(
        bucket_rules=bucket_rules,
        search_roots=search_roots,
        master_paths=master_paths,
        icon_choice=icon_choice,
    )


def _bucket_rule_to_dict(rule: BucketRule) -> dict:
    data = {"extensions": sorted(rule.extensions), "bucket": rule.bucket}
    if rule.filename_pattern is not None:
        data["filename_pattern"] = rule.filename_pattern
    return data


def _master_path_to_dict(master_path: MasterPath) -> dict:
    return {"path": master_path.path, "backed_up": master_path.backed_up}


def config_to_dict(config: Config) -> dict:
    """JSON/YAML-safe dict representation of ``config`` -- the exact shape
    ``save_config`` persists to ``config.yaml``, factored out as its own
    function so other consumers (e.g. Settings > Advanced's read-only
    effective-config view) render the identical shape without hand-copying
    it and risking drift between what's shown and what's actually saved.
    Never includes AI-provider credentials -- those live entirely outside
    ``Config``/``config.yaml``, in a separate 0600 credentials file (see
    ``ai/__init__.py``).
    """
    return {
        "bucket_rules": [_bucket_rule_to_dict(r) for r in config.bucket_rules],
        "search_roots": list(config.search_roots),
        "master_paths": [_master_path_to_dict(m) for m in config.master_paths],
        "icon_choice": config.icon_choice,
    }


def save_config(adapter: OSAdapter, config: Config, path: Path | None = None) -> None:
    """Serialize ``config`` to YAML and write it via ``adapter.write_file``."""
    if path is None:
        path = default_config_path(adapter)

    path.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_file(path, yaml.safe_dump(config_to_dict(config), sort_keys=False))

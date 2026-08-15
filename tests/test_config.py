"""Tests for cleanup_tools.config: bucket resolution and YAML persistence.

Uses the real MacOSAdapter (already covered by tests/test_adapters.py) for
all load_config/save_config calls, but always points default_config_path/
path arguments at a tmp_path fixture so the real ~/.config/cleanup-tools is
never touched.
"""

from __future__ import annotations

import re

import pytest
import yaml

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.config import (
    DEFAULT_BUCKET_RULES,
    DEFAULT_CONFIG,
    BucketRule,
    Config,
    MasterPath,
    config_to_dict,
    default_config_path,
    load_config,
    resolve_bucket,
    save_config,
)


@pytest.fixture
def adapter() -> MacOSAdapter:
    return MacOSAdapter()


# ---------------------------------------------------------------------------
# 1. resolve_bucket() against every extension in the default rules.
# ---------------------------------------------------------------------------

# Plain (non-"screenshot*") filenames for every extension covered by a
# non-photo default rule, plus the plain-photo case (which must land in
# "photos", not "screenshots", since the filename doesn't start with
# "screenshot").
PLAIN_BUCKET_CASES = [
    # photos (screenshot rule precedes this one but requires the filename
    # pattern, which a plain name doesn't satisfy)
    ("photo.png", "photos"),
    ("photo.jpg", "photos"),
    ("photo.jpeg", "photos"),
    ("photo.heic", "photos"),
    ("photo.gif", "photos"),
    ("photo.webp", "photos"),
    ("photo.tiff", "photos"),
    # installers
    ("app.dmg", "installers"),
    ("app.pkg", "installers"),
    # videos
    ("clip.mp4", "videos"),
    ("clip.mov", "videos"),
    ("clip.m4v", "videos"),
    ("clip.avi", "videos"),
    ("clip.mkv", "videos"),
    # pdfs
    ("doc.pdf", "pdfs"),
    # archives
    ("bundle.zip", "archives"),
    ("bundle.tar", "archives"),
    ("bundle.gz", "archives"),
    ("bundle.7z", "archives"),
    ("bundle.rar", "archives"),
    # data
    ("sheet.csv", "data"),
    ("sheet.json", "data"),
    ("sheet.xlsx", "data"),
    ("sheet.xml", "data"),
    ("sheet.tsv", "data"),
    ("sheet.numbers", "data"),
    # docs
    ("file.doc", "docs"),
    ("file.docx", "docs"),
    ("file.ppt", "docs"),
    ("file.pptx", "docs"),
    ("file.txt", "docs"),
    ("file.md", "docs"),
    ("file.pages", "docs"),
    ("file.key", "docs"),
]

# The extension set shared by both the "screenshots" and "photos" rules.
PHOTO_LIKE_EXTENSIONS = ["png", "jpg", "jpeg", "heic", "gif", "webp", "tiff"]


@pytest.mark.parametrize("filename,expected_bucket", PLAIN_BUCKET_CASES)
def test_resolve_bucket_plain_filenames(filename, expected_bucket):
    assert resolve_bucket(filename, DEFAULT_BUCKET_RULES) == expected_bucket


@pytest.mark.parametrize("ext", PHOTO_LIKE_EXTENSIONS)
def test_resolve_bucket_screenshot_prefixed_filenames(ext):
    assert resolve_bucket(f"screenshot.{ext}", DEFAULT_BUCKET_RULES) == "screenshots"


def test_resolve_bucket_screenshot_pattern_is_case_insensitive_with_suffix():
    # "Screenshot 2024.png": mixed-case prefix, extra text before extension.
    assert resolve_bucket("Screenshot 2024.png", DEFAULT_BUCKET_RULES) == "screenshots"


def test_resolve_bucket_screenshot_pattern_is_case_insensitive_extension():
    # "screenshot.PNG": lowercase prefix, uppercase extension.
    assert resolve_bucket("screenshot.PNG", DEFAULT_BUCKET_RULES) == "screenshots"


def test_resolve_bucket_unmatched_extension_returns_other():
    assert resolve_bucket("archive.xyz", DEFAULT_BUCKET_RULES) == "other"


def test_resolve_bucket_no_extension_returns_other():
    assert resolve_bucket("README", DEFAULT_BUCKET_RULES) == "other"


# ---------------------------------------------------------------------------
# 1b. resolve_bucket() for extension-less filenames must match bash's
#     ext="${b##*.}" behavior: when there's no "." at all, parameter
#     expansion no-ops and `ext` is left as the *whole* (lowercased)
#     filename, not an empty string. So a file literally named "csv" (no
#     extension) still buckets as "data", not "other".
# ---------------------------------------------------------------------------

EXTENSIONLESS_BUCKET_CASES = [
    ("csv", "data"),
    ("pdf", "pdfs"),
    ("mp4", "videos"),
    # Mixed case, no extension: still lowercased and matched as a whole.
    ("CSV", "data"),
]


@pytest.mark.parametrize("filename,expected_bucket", EXTENSIONLESS_BUCKET_CASES)
def test_resolve_bucket_extensionless_filename_matches_bash_behavior(
    filename, expected_bucket
):
    assert resolve_bucket(filename, DEFAULT_BUCKET_RULES) == expected_bucket


# ---------------------------------------------------------------------------
# 2. load_config() with no file present.
# ---------------------------------------------------------------------------


def test_load_config_missing_file_returns_default_config(adapter, tmp_path):
    missing_path = tmp_path / "does-not-exist" / "config.yaml"
    assert not missing_path.exists()

    result = load_config(adapter, path=missing_path)

    assert result is DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# 3. load_config() with a custom bucket rule: custom wins first-match,
#    defaults still apply as fallback.
# ---------------------------------------------------------------------------


def test_load_config_custom_bucket_rule_takes_priority_over_defaults(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "bucket_rules": [
                    {"extensions": ["png"], "bucket": "custom-png"},
                ],
                "search_roots": [],
                "master_paths": [],
            }
        )
    )

    config = load_config(adapter, path=config_path)

    # Custom rule matches first, ahead of the default screenshots/photos
    # rules for the same extension.
    assert resolve_bucket("photo.png", config.bucket_rules) == "custom-png"
    assert resolve_bucket("screenshot.png", config.bucket_rules) == "custom-png"

    # Defaults still apply as a fallback for everything the custom rule
    # doesn't cover.
    assert resolve_bucket("doc.pdf", config.bucket_rules) == "pdfs"
    assert resolve_bucket("clip.mp4", config.bucket_rules) == "videos"
    assert resolve_bucket("archive.xyz", config.bucket_rules) == "other"

    # User rule is prepended, defaults follow, in order.
    assert config.bucket_rules[0] == BucketRule(
        extensions=frozenset({"png"}), bucket="custom-png"
    )
    assert config.bucket_rules[1:] == DEFAULT_BUCKET_RULES


# ---------------------------------------------------------------------------
# 4. load_config() with malformed YAML raises a clear error naming the file.
# ---------------------------------------------------------------------------


def test_load_config_malformed_yaml_raises_clear_error(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("bucket_rules: [1, 2\n  - broken")

    with pytest.raises(ValueError, match=re.escape(str(config_path))):
        load_config(adapter, path=config_path)


def test_load_config_non_mapping_yaml_raises_clear_error(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- just\n- a\n- list\n")

    with pytest.raises(ValueError, match=re.escape(str(config_path))):
        load_config(adapter, path=config_path)


# ---------------------------------------------------------------------------
# 4b. A bucket_rules/master_paths entry missing a required key raises the
#     same clear, file-naming ValueError as malformed YAML, not a raw
#     KeyError.
# ---------------------------------------------------------------------------


def test_load_config_bucket_rule_missing_bucket_key_raises_clear_error(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"bucket_rules": [{"extensions": ["png"]}]})
    )

    with pytest.raises(ValueError, match=re.escape(str(config_path))):
        load_config(adapter, path=config_path)


def test_load_config_master_path_missing_path_key_raises_clear_error(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"master_paths": [{"backed_up": True}]})
    )

    with pytest.raises(ValueError, match=re.escape(str(config_path))):
        load_config(adapter, path=config_path)


# ---------------------------------------------------------------------------
# 4c. Null YAML keys (key present but with no value, parsing to None) are
#     treated the same as the key being absent, not a crash.
# ---------------------------------------------------------------------------


def test_load_config_null_bucket_rules_treated_as_absent(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("bucket_rules:\nsearch_roots: []\nmaster_paths: []\n")

    config = load_config(adapter, path=config_path)

    assert config.bucket_rules == DEFAULT_BUCKET_RULES


def test_load_config_null_search_roots_treated_as_absent(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("bucket_rules: []\nsearch_roots:\nmaster_paths: []\n")

    config = load_config(adapter, path=config_path)

    assert config.search_roots == []


def test_load_config_null_master_paths_treated_as_absent(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("bucket_rules: []\nsearch_roots: []\nmaster_paths:\n")

    config = load_config(adapter, path=config_path)

    assert config.master_paths == []


def test_load_config_all_keys_null_treated_as_absent(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("bucket_rules:\nsearch_roots:\nmaster_paths:\n")

    config = load_config(adapter, path=config_path)

    assert config.bucket_rules == DEFAULT_BUCKET_RULES
    assert config.search_roots == []
    assert config.master_paths == []


# ---------------------------------------------------------------------------
# 5. save_config() then load_config() round-trips master_paths (including
#    backed_up) and search_roots.
# ---------------------------------------------------------------------------


def test_save_then_load_round_trips_search_roots_and_master_paths(adapter, tmp_path):
    config_path = tmp_path / "cleanup-tools" / "config.yaml"

    original = Config(
        bucket_rules=[],
        search_roots=["/Users/mdostal/Downloads", "/Users/mdostal/Desktop"],
        master_paths=[
            MasterPath(path="/Users/mdostal/Documents/master-photos", backed_up=True),
            MasterPath(path="/Users/mdostal/Documents/master-videos", backed_up=False),
        ],
    )

    save_config(adapter, original, path=config_path)
    assert config_path.exists()

    loaded = load_config(adapter, path=config_path)

    assert loaded.search_roots == original.search_roots
    assert loaded.master_paths == original.master_paths
    assert loaded.master_paths[0].backed_up is True
    assert loaded.master_paths[1].backed_up is False


def test_config_to_dict_matches_the_shape_save_config_actually_persists(tmp_path):
    """config_to_dict is the single source of truth save_config's own YAML
    write goes through -- this pins down that save_config's persisted YAML
    parses back to exactly config_to_dict's output, so a consumer (e.g.
    Settings > Advanced's read-only view) can never drift from what's
    actually on disk.
    """
    config = Config(
        bucket_rules=[BucketRule(extensions=frozenset({"log"}), bucket="logs")],
        search_roots=["/some/root"],
        master_paths=[MasterPath(path="/some/master", backed_up=True)],
        icon_choice="recycle-folder",
    )

    as_dict = config_to_dict(config)
    assert as_dict == {
        "bucket_rules": [{"extensions": ["log"], "bucket": "logs"}],
        "search_roots": ["/some/root"],
        "master_paths": [{"path": "/some/master", "backed_up": True}],
        "icon_choice": "recycle-folder",
        "ui_mode": "standard",
    }

    class TmpHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    adapter = TmpHomeAdapter()
    config_path = tmp_path / "config.yaml"
    save_config(adapter, config, path=config_path)

    persisted = yaml.safe_load(config_path.read_text())
    assert persisted == as_dict


def test_config_to_dict_never_includes_credential_material():
    """AI-provider credentials live entirely outside Config/config.yaml (a
    separate 0600 credentials file, see ai/__init__.py) -- this pins down
    that config_to_dict's keys are exactly the four Config fields, nothing
    that could accidentally carry a secret through the Advanced JSON view.
    """
    config = Config(bucket_rules=[], search_roots=[], master_paths=[])
    assert set(config_to_dict(config).keys()) == {
        "bucket_rules",
        "search_roots",
        "master_paths",
        "icon_choice",
        "ui_mode",
    }


def test_save_config_uses_default_config_path_under_tmp_path_home(tmp_path):
    # Exercise default_config_path()/the no-path-argument branches of
    # save_config/load_config, but with the adapter's home redirected to
    # tmp_path so the real ~/.config/cleanup-tools is never touched.
    class TmpHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    adapter = TmpHomeAdapter()
    expected_path = tmp_path / ".config" / "cleanup-tools" / "config.yaml"
    assert default_config_path(adapter) == expected_path

    config = Config(
        bucket_rules=[],
        search_roots=["/some/root"],
        master_paths=[MasterPath(path="/some/master", backed_up=True)],
    )

    save_config(adapter, config)
    assert expected_path.exists()

    loaded = load_config(adapter)
    assert loaded.search_roots == ["/some/root"]
    assert loaded.master_paths == [MasterPath(path="/some/master", backed_up=True)]


def test_ui_mode_round_trips_through_save_and_load(tmp_path):
    class TmpHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    adapter = TmpHomeAdapter()
    config_path = tmp_path / "config.yaml"

    save_config(adapter, Config(bucket_rules=[], ui_mode="console"), path=config_path)

    loaded = load_config(adapter, path=config_path)
    assert loaded.ui_mode == "console"


def test_ui_mode_defaults_to_standard_when_absent_from_config_file(tmp_path):
    class TmpHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    adapter = TmpHomeAdapter()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("search_roots: []\n")  # no ui_mode key at all

    loaded = load_config(adapter, path=config_path)
    assert loaded.ui_mode == "standard"


def test_ui_mode_defaults_to_standard_with_no_config_file_at_all():
    assert DEFAULT_CONFIG.ui_mode == "standard"


# ---------------------------------------------------------------------------
# 6. load -> save -> load -> save -> load must be idempotent: bucket_rules
#    must NOT grow on every round trip. Previously, save_config persisted
#    the full merged rule list (user rules + defaults) every time, and
#    load_config unconditionally re-prepended whatever it read as "user
#    rules" ahead of a fresh copy of the defaults -- so three cycles grew
#    9 -> 17 -> 25 rules instead of staying flat.
# ---------------------------------------------------------------------------


def test_load_save_round_trip_is_idempotent_default_config_only(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"

    config = load_config(adapter, path=config_path)  # no file yet -> DEFAULT_CONFIG
    rule_counts = [len(config.bucket_rules)]

    for _ in range(4):
        save_config(adapter, config, path=config_path)
        config = load_config(adapter, path=config_path)
        rule_counts.append(len(config.bucket_rules))

    assert rule_counts == [len(DEFAULT_BUCKET_RULES)] * len(rule_counts)
    assert config.bucket_rules == DEFAULT_BUCKET_RULES


def test_load_save_round_trip_is_idempotent_with_custom_rule(adapter, tmp_path):
    config_path = tmp_path / "config.yaml"

    config = Config(
        bucket_rules=[BucketRule(extensions=frozenset({"png"}), bucket="custom-png")]
        + DEFAULT_BUCKET_RULES,
        search_roots=["/Users/mdostal/Downloads"],
        master_paths=[MasterPath(path="/Users/mdostal/master", backed_up=True)],
    )

    save_config(adapter, config, path=config_path)
    loaded = load_config(adapter, path=config_path)
    expected_count = len(loaded.bucket_rules)
    assert expected_count == 1 + len(DEFAULT_BUCKET_RULES)

    rule_counts = [expected_count]
    for _ in range(4):
        save_config(adapter, loaded, path=config_path)
        loaded = load_config(adapter, path=config_path)
        rule_counts.append(len(loaded.bucket_rules))

    assert rule_counts == [expected_count] * len(rule_counts)
    assert loaded.bucket_rules[0] == BucketRule(
        extensions=frozenset({"png"}), bucket="custom-png"
    )
    assert loaded.bucket_rules[1:] == DEFAULT_BUCKET_RULES

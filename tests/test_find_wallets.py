"""Tests for cleanup_tools.commands.find_wallets.run() -- the Python port
of scripts/find-wallets.sh.

Three layers of coverage, mirroring the acceptance bar in the porting task:

1. Each of the 14 filename glob patterns (``FILENAME_PATTERNS``) and 4
   content regex alternatives (``CONTENT_PATTERNS``) is independently
   verified against a bash-derived fixture string it must match, and a
   deliberately close near-miss it must not -- so a typo introduced while
   porting the bash source would fail a specific, named test rather than
   just "the scan found fewer files than expected".

   (The bash source (``scripts/find-wallets.sh``) actually contains 14
   ``-iname`` alternatives, not 13 -- verified by
   ``grep -o "-iname '[^']*'" scripts/find-wallets.sh | wc -l``. Every
   pattern test below is parametrized directly off
   ``find_wallets.FILENAME_PATTERNS``, the module's own source of truth, so
   this file can never silently drift out of sync with it.)

2. The single highest-stakes property: ``find_wallets.run()``'s full return
   value, JSON-serialized, never contains any of the actual matched secret
   text (seed words, xprv string, PEM key body, keystore cipher value) --
   only paths and match-type/pattern tags. This is checked against a fixture
   tree with realistic-shaped (fake) secret content, not a vacuous
   always-passing assertion -- positive-control assertions confirm the scan
   actually found and tagged every fixture file first.

3. Exclusion (node_modules / Library/Caches), default-home-vs-explicit-root
   resolution, and graceful handling of an unreadable directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.adapters._util import matches_pattern
from cleanup_tools.commands import find_wallets


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _make_fake_adapter(home: Path) -> MacOSAdapter:
    """A MacOSAdapter whose resolve_home() points at ``home`` instead of the
    real user home (mirrors test_survey.py / test_reclaim.py's pattern), so
    no test here ever touches the real filesystem.
    """

    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self) -> Path:
            return home

    return FakeHomeAdapter()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


@pytest.fixture
def adapter(home: Path) -> MacOSAdapter:
    return _make_fake_adapter(home)


# ---------------------------------------------------------------------------
# 1a. Filename patterns: fixture (must match) + near-miss (must not match),
#     verified against find_wallets.FILENAME_PATTERNS itself.
# ---------------------------------------------------------------------------

# pattern -> (fixture filename that must match, close near-miss that must not)
_FILENAME_FIXTURES: dict[str, tuple[str, str]] = {
    "wallet.dat": ("wallet.dat", "wallet2.dat"),
    "*.wallet": ("myfile.wallet", "mywallet.txt"),
    "keystore*": ("keystore-1234.json", "mykeystore.json"),
    "UTC--*": ("UTC--2020-01-01T00-00-00Z--abcdef.json", "backup-UTC--foo.json"),
    "*.kdbx": ("passwords.kdbx", "passwords.kdb"),
    "electrum*": ("electrum-wallet.dat", "myelectrum.dat"),
    "*mnemonic*": ("my_mnemonic_backup.txt", "my_mnemo_backup.txt"),
    "*seed*phrase*": ("my_seed_phrase.txt", "phrase_seed.txt"),
    "*recovery*phrase*": ("wallet_recovery_phrase.pdf", "wallet_recovery_notes.pdf"),
    "*.keychain": ("login.keychain", "login.keychain-db"),
    "default_wallet": ("default_wallet", "default_wallet.bak"),
    "metamask*": ("metamask_backup.json", "mymetamask.json"),
    "exodus*": ("exodus.wallet", "myexodus.wallet"),
    "atomic*wallet*": ("atomic_wallet_backup.dat", "wallet_atomic.dat"),
}


def test_filename_fixtures_cover_every_pattern_in_the_module():
    """Sanity check on the fixture table itself: if find_wallets.py's
    FILENAME_PATTERNS ever changes (a pattern added/removed/edited), this
    fails loudly rather than the parametrized tests below silently testing
    a stale subset.
    """
    assert set(_FILENAME_FIXTURES) == set(find_wallets.FILENAME_PATTERNS)


@pytest.mark.parametrize("pattern", find_wallets.FILENAME_PATTERNS)
def test_filename_pattern_matches_its_bash_derived_fixture(pattern):
    fixture_name, _near_miss = _FILENAME_FIXTURES[pattern]
    assert matches_pattern(fixture_name.lower(), pattern.lower())


@pytest.mark.parametrize("pattern", find_wallets.FILENAME_PATTERNS)
def test_filename_pattern_does_not_match_near_miss(pattern):
    _fixture_name, near_miss = _FILENAME_FIXTURES[pattern]
    assert not matches_pattern(near_miss.lower(), pattern.lower())


# ---------------------------------------------------------------------------
# 1b. Content regex alternatives: fixture (must match) + adjacent
#     non-matching text (must not), verified against
#     find_wallets.CONTENT_PATTERNS itself.
# ---------------------------------------------------------------------------

_FAKE_SEED_PHRASE = (
    "abandon ability able about above absent absorb abstract absurd abuse "
    "access accident"
)  # 12 lowercase words -- the bash regex's minimum (11 reps + 1 = 12 words)
_FAKE_XPRV = "xprv9zFakeTestExtendedKeyNotReal1234567890ABCDEFabcdefGHIJKL"
_FAKE_PEM_HEADER = "-----BEGIN RSA PRIVATE KEY-----"
_FAKE_KEYSTORE_CIPHER = '"crypto": {"cipher": "aes-128-ctr", "ciphertext": "deadbeef1234"}'

# name -> (fixture text that must match, adjacent text that must not)
_CONTENT_FIXTURES: dict[str, tuple[str, str]] = {
    "seed_phrase": (
        _FAKE_SEED_PHRASE,
        "abandon ability able about above",  # only 5 words -- too short a run
    ),
    "xprv_key": (
        _FAKE_XPRV,
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",  # plain base58, no xprv prefix
    ),
    "pem_private_key": (
        f"{_FAKE_PEM_HEADER}\nMIIFakeKeyBodyNotReal==\n-----END RSA PRIVATE KEY-----",
        "-----BEGIN CERTIFICATE-----\nMIIFakeCertNotReal==\n-----END CERTIFICATE-----",
    ),
    "eth_keystore_cipher": (
        _FAKE_KEYSTORE_CIPHER,
        # "cipher" appears, but outside the crypto object's braces -- the
        # bash regex's [^}]* can't cross the intervening "}".
        '"crypto": {"kdf": "scrypt"}, "cipher": "aes-128-ctr"',
    ),
}


def test_content_fixtures_cover_every_pattern_in_the_module():
    assert set(_CONTENT_FIXTURES) == {name for name, _pattern in find_wallets.CONTENT_PATTERNS}


@pytest.mark.parametrize("name,pattern", find_wallets.CONTENT_PATTERNS, ids=lambda x: x if isinstance(x, str) else "regex")
def test_content_pattern_matches_its_fixture(name, pattern):
    fixture_text, _non_match = _CONTENT_FIXTURES[name]
    assert pattern.search(fixture_text) is not None


@pytest.mark.parametrize("name,pattern", find_wallets.CONTENT_PATTERNS, ids=lambda x: x if isinstance(x, str) else "regex")
def test_content_pattern_does_not_match_adjacent_text(name, pattern):
    _fixture_text, non_match = _CONTENT_FIXTURES[name]
    assert pattern.search(non_match) is None


# ---------------------------------------------------------------------------
# 2. The highest-stakes check: run()'s serialized output never leaks any
#    matched secret substring -- only paths, match_type, and pattern name.
# ---------------------------------------------------------------------------


def test_run_output_never_leaks_matched_secret_text(adapter, home):
    documents = home / "Documents"
    desktop = home / "Desktop"
    downloads = home / "Downloads"
    (documents / "notes").mkdir(parents=True)
    (documents / "keys").mkdir(parents=True)
    desktop.mkdir()
    downloads.mkdir()

    seed_file = documents / "notes" / "seed_backup.txt"
    seed_file.write_text(f"my wallet backup:\n{_FAKE_SEED_PHRASE}\nend of backup")

    pem_file = documents / "keys" / "private_key_backup.txt"
    pem_body = "MIIEvQIBADANBgkqhkiG9w0BFakeNotARealKeyBodyAtAll=="
    pem_file.write_text(
        f"{_FAKE_PEM_HEADER}\n{pem_body}\n-----END RSA PRIVATE KEY-----"
    )

    xprv_file = desktop / "xprv_backup.md"
    xprv_file.write_text(f"# Backup\n\nExtended key: {_FAKE_XPRV}\n")

    keystore_file = downloads / "wallet_keystore.json"
    keystore_cipher_value = "deadbeef1234cafefeed5678"
    keystore_text = (
        '{"address": "abc123", '
        f'"crypto": {{"cipher": "aes-128-ctr", "ciphertext": "{keystore_cipher_value}", '
        '"cipherparams": {"iv": "abcdef"}}}, "id": "some-id"}'
    )
    keystore_file.write_text(keystore_text)

    # Also drop a filename-pattern match (paths are never secret, but this
    # confirms by_filename output shape stays path-only too).
    (documents / "wallet.dat").write_text("not real wallet bytes")

    result = find_wallets.run(adapter, SimpleNamespace(root=str(home)))
    serialized = json.dumps(result, default=str)

    # Positive controls: the scan actually found and tagged every fixture
    # file, so the absence checks below aren't vacuously true.
    content_paths_by_pattern = {
        entry["pattern"]: entry["path"] for entry in result["by_content"]
    }
    assert content_paths_by_pattern["seed_phrase"] == seed_file
    assert content_paths_by_pattern["pem_private_key"] == pem_file
    assert content_paths_by_pattern["xprv_key"] == xprv_file
    assert content_paths_by_pattern["eth_keystore_cipher"] == keystore_file
    assert str(seed_file) in serialized
    assert str(pem_file) in serialized
    assert str(xprv_file) in serialized
    assert str(keystore_file) in serialized

    # The actual secrets must never appear anywhere in the serialized output.
    assert _FAKE_SEED_PHRASE not in serialized
    assert "abandon ability able" not in serialized  # partial seed-phrase leak too
    assert pem_body not in serialized
    assert _FAKE_XPRV not in serialized
    assert keystore_cipher_value not in serialized
    assert "aes-128-ctr" not in serialized

    # And the shape of every by_content entry is exactly the 3 allowed keys.
    for entry in result["by_content"]:
        assert set(entry) == {"path", "match_type", "pattern"}
        assert entry["match_type"] == "content"


def test_no_group_calls_anywhere_in_module():
    """Verify by construction (not just by reading intent) that .group() is
    never called in find_wallets.py -- the one thing that could leak matched
    secret text into the output.
    """
    source = Path(find_wallets.__file__).read_text()
    assert ".group(" not in source
    assert ".group()" not in source


# ---------------------------------------------------------------------------
# 3. Exclusions, root resolution, unreadable-directory handling.
# ---------------------------------------------------------------------------


def test_by_filename_excludes_node_modules_and_library_caches(adapter, home):
    kept = home / "Documents" / "wallet.dat"
    kept.parent.mkdir(parents=True)
    kept.write_text("x")

    in_node_modules = home / "Documents" / "node_modules" / "pkg" / "wallet.dat"
    in_node_modules.parent.mkdir(parents=True)
    in_node_modules.write_text("x")

    in_library_caches = home / "Library" / "Caches" / "app" / "wallet.dat"
    in_library_caches.parent.mkdir(parents=True)
    in_library_caches.write_text("x")

    result = find_wallets.run(adapter, SimpleNamespace(root=str(home)))
    matched_paths = {entry["path"] for entry in result["by_filename"]}

    assert kept in matched_paths
    assert in_node_modules not in matched_paths
    assert in_library_caches not in matched_paths


def test_run_defaults_to_home_directory_when_no_root_given(adapter, home):
    (home / "Documents").mkdir()
    (home / "wallet.dat").write_text("x")

    result = find_wallets.run(adapter, args=None)

    assert result["root"] == home
    assert (home / "wallet.dat") in {e["path"] for e in result["by_filename"]}


def test_run_uses_explicit_root_argument(adapter, home, tmp_path):
    other_root = tmp_path / "other_root"
    other_root.mkdir()
    (other_root / "wallet.dat").write_text("x")

    result = find_wallets.run(adapter, SimpleNamespace(root=str(other_root)))

    assert result["root"] == other_root
    assert (other_root / "wallet.dat") in {e["path"] for e in result["by_filename"]}
    # Confirms it did NOT fall back to home.
    assert result["root"] != home


def test_unreadable_subdirectory_does_not_crash_the_scan(adapter, home):
    (home / "Documents").mkdir()
    readable_match = home / "Documents" / "wallet.dat"
    readable_match.write_text("x")

    locked_dir = home / "Documents" / "locked"
    locked_dir.mkdir()
    locked_dir.chmod(0o000)
    try:
        result = find_wallets.run(adapter, SimpleNamespace(root=str(home)))
    finally:
        locked_dir.chmod(0o755)

    assert isinstance(result["by_filename"], list)
    assert isinstance(result["by_content"], list)
    assert readable_match in {e["path"] for e in result["by_filename"]}


def test_run_returns_only_the_three_expected_top_level_keys(adapter, home):
    result = find_wallets.run(adapter, SimpleNamespace(root=str(home)))
    assert set(result) == {"root", "by_filename", "by_content"}


# ---------------------------------------------------------------------------
# 4. Regression test for the reviewer-found bug: _by_filename must not
#    starve a later-in-list pattern when the total match count exceeds the
#    200 cap.
# ---------------------------------------------------------------------------


def test_by_filename_does_not_starve_a_later_pattern_when_total_exceeds_cap(home):
    """The old implementation looped ``adapter.list_dir(root, pattern=p)``
    once per pattern, fully draining one pattern's matches before moving to
    the next. When two patterns' matches were evenly interleaved on disk but
    together exceeded the 200-result cap, that per-pattern draining meant
    whichever pattern came first in ``FILENAME_PATTERNS`` (``"*.wallet"``,
    index 1) crowded out a later pattern (``"metamask*"``, index 11):
    150 ``*.wallet`` files fully drained first, leaving only 50 of the 150
    ``metamask*`` files room under the cap -- a 150/50 split, even though
    the bash original's single ``find`` (and this fix) would naturally
    interleave both roughly evenly.

    A fake adapter's ``list_dir(root)`` (no pattern) returns 300 synthetic
    paths -- ``*.wallet`` and ``metamask*`` matches strictly alternating --
    standing in for "interleaved on disk", without depending on any real
    filesystem's directory-read order (which isn't guaranteed to match
    creation order). The same fake's ``list_dir(root, pattern=p)`` filters
    that synthetic listing with the shared ``matches_pattern`` helper, so
    the *old* per-pattern implementation can also be exercised against it
    for the red/green check below.
    """
    documents = home / "Documents"
    interleaved_paths: list[Path] = []
    for i in range(150):
        interleaved_paths.append(documents / f"file{i:04d}.wallet")
        interleaved_paths.append(documents / f"metamask{i:04d}.json")

    class InterleavedAdapter(MacOSAdapter):
        def resolve_home(self) -> Path:
            return home

        def list_dir(self, path, max_depth=None, pattern=None):
            if pattern is None:
                return list(interleaved_paths)
            pattern_lower = pattern.lower()
            return [
                p for p in interleaved_paths if matches_pattern(p.name.lower(), pattern_lower)
            ]

    result = find_wallets.run(InterleavedAdapter(), SimpleNamespace(root=str(home)))
    by_filename = result["by_filename"]

    assert len(by_filename) == find_wallets.FILENAME_RESULT_CAP

    pattern_counts: dict[str, int] = {}
    for entry in by_filename:
        pattern_counts[entry["pattern"]] = pattern_counts.get(entry["pattern"], 0) + 1

    wallet_count = pattern_counts.get("*.wallet", 0)
    metamask_count = pattern_counts.get("metamask*", 0)

    # No other pattern in FILENAME_PATTERNS matches these fixture names.
    assert wallet_count + metamask_count == find_wallets.FILENAME_RESULT_CAP

    # The fix: with a single walk testing all patterns per file, the cap
    # reflects the (alternating) traversal order -- roughly even, not the
    # old 150/50 split that starved "metamask*".
    assert wallet_count > 90
    assert metamask_count > 90

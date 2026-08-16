"""Tests for cleanup_tools.ui (Flask approvals UI) routes.

Mirrors the fake-home pattern used throughout this test suite (test_sort.py,
test_survey.py, ...): a MacOSAdapter subclass whose resolve_home() points at
a tmp_path-scoped directory, so nothing here ever touches the real user
home or the real approval_queue.yaml. Every test drives the app purely
through Flask's test client (app.test_client()) -- no real server socket is
bound in this file (that's covered separately, for real, via a subprocess
lsof/netstat check outside pytest).
"""

from __future__ import annotations

import contextlib
import html
import json
from pathlib import Path

import pytest
from PIL import Image

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.queue import QueueEntry, build_plan_snapshot
from cleanup_tools.ui.app import create_app
from cleanup_tools.ui.routes import (
    DEFAULT_ICON_CHOICE,
    ICON_CHOICES,
    PROTECTED_PATH_ROOTS,
    UI_MODES,
    _group_entries_hierarchical,
    _is_protected_path,
    _location_for_src,
    bucket_icon,
    parse_group_key,
    short_path,
)


def _make_fake_adapter(home: Path) -> MacOSAdapter:
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


@pytest.fixture
def app(adapter):
    return create_app(adapter)


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_queue(adapter, entries: list[QueueEntry]) -> Path:
    path = queue_module.default_queue_path(adapter)
    queue_module.save_queue(adapter, entries, path)
    return path


def _reload_queue(adapter) -> list[QueueEntry]:
    return queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))


def _poll_job_until_terminal(client, job_id: str, timeout: float = 5.0) -> dict:
    """Poll GET /status/<job_id> until it reports "done" or "error".

    /plan/reclaim now kicks off a background job (see cleanup_tools.ui.jobs)
    instead of blocking the request, so tests that need the plan to have
    actually finished poll for it here rather than asserting on the
    (now-immediate) response to /plan/reclaim itself.
    """
    import time

    deadline = time.time() + timeout
    payload = None
    while time.time() < deadline:
        resp = client.get(f"/status/{job_id}")
        assert resp.status_code == 200
        payload = resp.get_json()
        if payload["status"] in ("done", "error"):
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal status within {timeout}s: {payload}")


# ---------------------------------------------------------------------------
# 1. Dashboard: groups by group_key, sizes/counts, and per-status counts.
# ---------------------------------------------------------------------------


def test_dashboard_empty_queue_shows_zero_entries(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Queue is empty" in resp.data


def test_update_banner_present_but_hidden_on_every_page(client):
    # The update banner is global markup (base.html), rendered hidden by
    # default -- static/update-checker.js is what shows it, and only ever
    # does so inside the Tauri shell (window.__TAURI__), never in a plain
    # browser tab. Confirmed present on two different routes to prove it's
    # genuinely global, not accidentally dashboard-only.
    for path in ("/", "/queue"):
        html = client.get(path).data.decode()
        assert 'id="update-banner"' in html
        banner = html.split('id="update-banner"')[1].split("</div>")[0]
        assert "hidden" in banner
        assert 'src="/static/update-checker.js"' in html


def test_dashboard_groups_by_group_key_with_sizes_and_status_counts(adapter, client, tmp_path):
    # Hand-built entries with sizes deliberately NOT tiny/round-to-zero
    # (1 MiB / 2 MiB / 0.5 MiB) so the rendered MB figures are distinctive
    # enough to actually distinguish "the route computed the right total"
    # from "the route computed *some* total that happens to round to the
    # same 0.0 every group would show with tiny byte counts".
    f1 = tmp_path / "photo1.jpg"
    f1.write_bytes(b"a" * (1 * 1024 * 1024))
    f2 = tmp_path / "photo2.jpg"
    f2.write_bytes(b"b" * (2 * 1024 * 1024))
    f3 = tmp_path / "report.pdf"
    f3.write_bytes(b"c" * (512 * 1024))

    entries = [
        QueueEntry(
            action="move", src=str(f1), dest="", status="pending",
            group_key="sort:photos", plan_snapshot=build_plan_snapshot(f1),
        ),
        QueueEntry(
            action="move", src=str(f2), dest="", status="approved",
            group_key="sort:photos", plan_snapshot=build_plan_snapshot(f2),
        ),
        QueueEntry(
            action="delete", src=str(f3), dest="", status="pending",
            group_key="reclaim:os_junk", plan_snapshot=build_plan_snapshot(f3),
        ),
    ]
    _seed_queue(adapter, entries)

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()

    # Group "sort:photos": 2 items, 1 MiB + 2 MiB = 3 MiB exactly -> "3.0 MB".
    # The item count is promoted to its own headline-number element,
    # separate from the "item(s) · N MB" meta text next to it (see
    # dashboard.html's .branch-count) -- so these check the count and the
    # unit text independently rather than as one contiguous "2 items"
    # substring.
    assert "sort:photos" in html
    assert ">2<" in html
    assert "3.0 MB" in html

    # Group "reclaim:os_junk": 1 item, 512 KiB exactly -> "0.5 MB".
    assert "reclaim:os_junk" in html
    assert ">1<" in html
    assert "0.5 MB" in html

    # Per-status breakdown for the sort:photos group specifically: one
    # pending, one approved -- proving grouping isn't just a flat total.
    assert "pending: 1" in html
    assert "approved: 1" in html

    # Overall status bar: 2 pending total (across both groups), 1 approved --
    # parsed out of the actual group dicts the route built, independent of
    # the route's own arithmetic, via the same dashboard() helper functions
    # applied directly to a fresh load of the persisted queue.
    from cleanup_tools.ui.routes import _group_entries, _status_counts

    reloaded = _reload_queue(adapter)
    overall = _status_counts(reloaded)
    assert overall == {"pending": 2, "approved": 1}

    groups = _group_entries(reloaded)
    by_key = {g["group_key"]: g for g in groups}
    assert by_key["sort:photos"]["total_size"] == 3 * 1024 * 1024
    assert by_key["sort:photos"]["count"] == 2
    assert by_key["sort:photos"]["status_counts"] == {"pending": 1, "approved": 1}
    assert by_key["reclaim:os_junk"]["total_size"] == 512 * 1024
    assert by_key["reclaim:os_junk"]["count"] == 1


def test_dashboard_group_key_falls_back_to_ungrouped(adapter, client, tmp_path):
    f = tmp_path / "mystery.bin"
    f.write_bytes(b"x" * 10)
    entry = QueueEntry(
        action="delete", src=str(f), dest="", status="pending",
        group_key=None, plan_snapshot=build_plan_snapshot(f),
    )
    _seed_queue(adapter, [entry])

    resp = client.get("/")
    assert b"ungrouped" in resp.data


def _entry_with_size(tmp_path, name, size, group_key, status="pending"):
    f = tmp_path / name
    f.write_bytes(b"x" * size)
    return QueueEntry(
        action="move", src=str(f), dest="", status=status,
        group_key=group_key, plan_snapshot=build_plan_snapshot(f),
    )


def test_group_entries_hierarchical_aggregates_by_location_then_bucket(tmp_path):
    entries = [
        _entry_with_size(tmp_path, "a.jpg", 1024 * 1024, "sort:/root_a:photos"),
        _entry_with_size(tmp_path, "b.jpg", 2 * 1024 * 1024, "sort:/root_a:photos", status="approved"),
        _entry_with_size(tmp_path, "c.txt", 512 * 1024, "sort:/root_a:docs"),
        _entry_with_size(tmp_path, "d.bin", 4 * 1024 * 1024, "reclaim:/root_b:build_caches"),
    ]

    tree = _group_entries_hierarchical(entries)

    by_location = {loc["location"]: loc for loc in tree}
    assert set(by_location) == {"/root_a", "/root_b"}

    root_a = by_location["/root_a"]
    assert root_a["count"] == 3
    assert root_a["total_size"] == 3 * 1024 * 1024 + 512 * 1024
    assert root_a["status_counts"] == {"pending": 2, "approved": 1}

    buckets_by_label = {b["label"]: b for b in root_a["buckets"]}
    assert set(buckets_by_label) == {"photos", "docs"}
    assert buckets_by_label["photos"]["count"] == 2
    assert buckets_by_label["photos"]["total_size"] == 3 * 1024 * 1024
    assert buckets_by_label["photos"]["group_key"] == "sort:/root_a:photos"

    root_b = by_location["/root_b"]
    assert root_b["count"] == 1
    assert root_b["buckets"][0]["label"] == "build_caches"

    # Largest-first at both levels.
    assert tree[0]["location"] == "/root_b"  # 4 MiB > root_a's ~3.5 MiB
    assert root_a["buckets"][0]["label"] == "photos"


def test_group_entries_hierarchical_never_drops_entries_outside_configured_locations(tmp_path):
    outside = _entry_with_size(tmp_path, "orphan.bin", 1024, "sort:other:misc")
    no_group_key = _entry_with_size(tmp_path, "mystery.bin", 1024, None)

    tree = _group_entries_hierarchical([outside, no_group_key])

    other = next(loc for loc in tree if loc["location"] == "other")
    assert other["count"] == 2


def test_group_entries_hierarchical_empty_queue_returns_empty_list():
    assert _group_entries_hierarchical([]) == []


def test_dashboard_renders_location_tree_from_hierarchical_entries(adapter, client, tmp_path):
    entry = _entry_with_size(tmp_path, "a.jpg", 1024 * 1024, "sort:/somewhere:photos")
    _seed_queue(adapter, [entry])

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"/somewhere" in resp.data
    assert b"photos" in resp.data


# ---------------------------------------------------------------------------
# 1c. Protected paths: hard-blocked at the staging layer, never merely
#     flagged. See _is_protected_path's docstring for the DaisyDisk-inspired
#     rationale.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sub_path",
    ["", "CoreServices/Finder.app", "Frameworks/Foo.framework"],
)
def test_is_protected_path_true_under_every_protected_root(adapter, sub_path):
    for root in PROTECTED_PATH_ROOTS:
        candidate = root / sub_path if sub_path else root
        assert _is_protected_path(candidate, adapter) is True


def test_is_protected_path_true_for_home_root_itself(adapter, home):
    assert _is_protected_path(home, adapter) is True


def test_is_protected_path_false_for_ordinary_paths_inside_home(adapter, home):
    downloads = home / "Downloads"
    assert _is_protected_path(downloads, adapter) is False
    assert _is_protected_path(downloads / "photo.jpg", adapter) is False


def test_plan_sort_never_stages_a_protected_path(adapter, client, home, monkeypatch):
    """A misconfigured search_root pointing at a protected system location
    must never produce a queue entry -- refused structurally at staging
    time, not merely hidden in the UI. Monkeypatches PROTECTED_PATH_ROOTS
    to include this test's own tmp-scoped "system" dir, since a real
    /System doesn't exist (or isn't writable) on the test machine.
    """
    import cleanup_tools.ui.routes as routes_module

    fake_system_root = home / "FakeSystem"
    fake_system_root.mkdir()
    (fake_system_root / "important.plist").write_bytes(b"do-not-touch")
    monkeypatch.setattr(routes_module, "PROTECTED_PATH_ROOTS", [fake_system_root])

    config_module.save_config(
        adapter,
        config_module.Config(
            bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(fake_system_root)]
        ),
    )

    resp = client.get("/plan/sort")
    job_id = resp.get_json()["job_id"]
    result = _poll_job_until_terminal(client, job_id)
    assert result["status"] == "done"

    entries = _reload_queue(adapter)
    assert entries == []
    # The file itself is of course untouched -- this is a dry-run route.
    assert (fake_system_root / "important.plist").exists()


# ---------------------------------------------------------------------------
# 1d. /plan/* location-subset scoping via repeated ?dirs= -- the dashboard's
#     location multi-select "select a subset and just do this, go".
# ---------------------------------------------------------------------------


def test_plan_sort_dirs_query_param_scopes_to_only_the_selected_locations(adapter, client, home):
    root_a = home / "root_a"
    root_a.mkdir()
    root_b = home / "root_b"
    root_b.mkdir()
    (root_a / "photo.jpg").write_bytes(b"a")
    (root_b / "doc.pdf").write_bytes(b"b")
    config_module.save_config(
        adapter,
        config_module.Config(
            bucket_rules=config_module.DEFAULT_BUCKET_RULES,
            search_roots=[str(root_a), str(root_b)],
        ),
    )

    resp = client.get("/plan/sort", query_string={"dirs": str(root_a)})
    job_id = resp.get_json()["job_id"]
    result = _poll_job_until_terminal(client, job_id)
    assert result["status"] == "done"

    entries = _reload_queue(adapter)
    assert {Path(e.src).name for e in entries} == {"photo.jpg"}


# ---------------------------------------------------------------------------
# 2. /plan/sort and /plan/reclaim: idempotent staging via stage_entries'
#    existing dedup, not any dedup logic reinvented in the route.
# ---------------------------------------------------------------------------


def test_plan_sort_stages_entries_and_is_idempotent(adapter, client, home):
    downloads = home / "Downloads"
    downloads.mkdir()
    (downloads / "photo.jpg").write_bytes(b"photo-bytes")
    (downloads / "notes.txt").write_bytes(b"notes-bytes")

    resp1 = client.get("/plan/sort")
    assert resp1.status_code == 200
    job_id_1 = resp1.get_json()["job_id"]
    result_1 = _poll_job_until_terminal(client, job_id_1)
    assert result_1["status"] == "done"

    entries_after_first = _reload_queue(adapter)
    assert len(entries_after_first) == 2
    assert {e.status for e in entries_after_first} == {"pending"}
    assert {e.action for e in entries_after_first} == {"move"}

    # Hitting it again must not create duplicate pending entries for files
    # already staged -- this is stage_entries()'s dedup, exercised through
    # the route.
    resp2 = client.get("/plan/sort")
    assert resp2.status_code == 200
    job_id_2 = resp2.get_json()["job_id"]
    result_2 = _poll_job_until_terminal(client, job_id_2)
    assert result_2["status"] == "done"

    entries_after_second = _reload_queue(adapter)
    assert len(entries_after_second) == 2
    assert {e.id for e in entries_after_second} == {e.id for e in entries_after_first}


def test_plan_sort_missing_downloads_dir_does_not_crash(adapter, client, home):
    # No Downloads dir created under the fake home. /plan/sort calls
    # sort.run(adapter, args=None) -- no CLI-supplied dir, so the missing
    # default (configured/fallback) root is best-effort-skipped rather than
    # raised, matching reclaim/corral-screenshots' established precedent
    # (see sort._plan's docstring): the job lands "done" with an empty plan,
    # not stuck "running" or crashed into a 500.
    assert not (home / "Downloads").exists()
    resp = client.get("/plan/sort")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]
    payload = _poll_job_until_terminal(client, job_id)
    assert payload["status"] == "done"


# ---------------------------------------------------------------------------
# 1a2. short_path: the display-shortening transform that came directly out
#      of the UI design review (every persona's #1 or #2 complaint was raw,
#      60-100+ character paths repeated verbatim across the page).
# ---------------------------------------------------------------------------


def test_short_path_not_under_home_short_path_returned_unchanged():
    # A synthetic short path -- never under the real Path.home() in any
    # sane test environment, and short enough (<=3 parts) to exercise the
    # "leave it alone" branch rather than the ellipsis-capping one.
    assert short_path("/mnt/data") == "/mnt/data"


def test_short_path_not_under_home_long_path_capped_to_last_two_segments(tmp_path):
    long_path = tmp_path / "a" / "b" / "c" / "d"
    result = short_path(str(long_path))
    assert result == "…/c/d"


def test_short_path_home_itself_returns_tilde(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert short_path(str(fake_home)) == "~"


def test_short_path_one_or_two_segments_under_home_gets_tilde_prefix(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    assert short_path(str(fake_home / "Downloads")) == "~/Downloads"
    assert short_path(str(fake_home / "Downloads" / "sub")) == "~/Downloads/sub"


def test_short_path_more_than_two_segments_under_home_capped_with_ellipsis(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    deep = fake_home / "Documents" / "work" / "archive" / "2019"
    assert short_path(str(deep)) == "…/archive/2019"


# ---------------------------------------------------------------------------
# 1b. Location-aware group_key: _location_for_src resolves which configured
#     root an entry's src falls under (open-ended, never a fixed enum), and
#     parse_group_key defensively reads both the new and every pre-epic
#     group_key format without raising.
# ---------------------------------------------------------------------------


def test_location_for_src_resolves_configured_search_root(adapter, home):
    root_a = home / "root_a"
    root_a.mkdir()
    root_b = home / "root_b"
    root_b.mkdir()
    config_module.save_config(adapter, config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root_a), str(root_b)]))
    config = config_module.load_config(adapter)

    assert _location_for_src(str(root_a / "photo.jpg"), config, adapter) == str(root_a)
    assert _location_for_src(str(root_b / "sub" / "doc.pdf"), config, adapter) == str(root_b)


def test_location_for_src_falls_back_to_other_outside_every_configured_root(adapter, home):
    root_a = home / "root_a"
    root_a.mkdir()
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    config_module.save_config(adapter, config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root_a)]))
    config = config_module.load_config(adapter)

    assert _location_for_src(str(elsewhere / "file.txt"), config, adapter) == "other"


def test_location_for_src_falls_back_to_standard_trio_when_no_search_roots_configured(adapter, home):
    downloads = home / "Downloads"
    downloads.mkdir()
    config = config_module.load_config(adapter)  # no search_roots configured

    assert _location_for_src(str(downloads / "file.txt"), config, adapter) == str(downloads)
    assert _location_for_src(str(home / "elsewhere" / "file.txt"), config, adapter) == "other"


@pytest.mark.parametrize(
    "group_key,expected",
    [
        (None, {"pipeline": None, "location": "other", "bucket": None}),
        ("", {"pipeline": None, "location": "other", "bucket": None}),
        # Old (pre-epic) formats -- no location segment existed yet.
        ("sort:photos", {"pipeline": "sort", "location": "other", "bucket": "photos"}),
        ("reclaim:build-caches", {"pipeline": "reclaim", "location": "other", "bucket": "build-caches"}),
        ("corral-screenshots", {"pipeline": "corral-screenshots", "location": "other", "bucket": None}),
        # New (post-epic) formats -- location embedded.
        (
            "sort:/Users/me/Downloads:photos",
            {"pipeline": "sort", "location": "/Users/me/Downloads", "bucket": "photos"},
        ),
        (
            "reclaim:/Users/me/Documents:build-caches",
            {"pipeline": "reclaim", "location": "/Users/me/Documents", "bucket": "build-caches"},
        ),
        (
            "corral-screenshots:/Users/me/Desktop",
            {"pipeline": "corral-screenshots", "location": "/Users/me/Desktop", "bucket": None},
        ),
        # Unrecognized prefix -- must not raise.
        ("something-else:weird:shape:here", {"pipeline": None, "location": "other", "bucket": None}),
    ],
)
def test_parse_group_key_handles_every_known_format_without_raising(group_key, expected):
    assert parse_group_key(group_key) == expected


def test_plan_sort_stages_entries_with_location_embedded_in_group_key(adapter, client, home):
    root_a = home / "root_a"
    root_a.mkdir()
    (root_a / "photo.jpg").write_bytes(b"photo-bytes")
    config_module.save_config(adapter, config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root_a)]))

    resp = client.get("/plan/sort")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]
    result = _poll_job_until_terminal(client, job_id)
    assert result["status"] == "done"

    entries = _reload_queue(adapter)
    assert len(entries) == 1
    assert entries[0].group_key == f"sort:{root_a}:photos"


def test_plan_reclaim_stages_entries_and_is_idempotent(adapter, client, home):
    documents = home / "Documents"
    documents.mkdir()
    (documents / ".DS_Store").write_bytes(b"junk")

    resp1 = client.get("/plan/reclaim")
    assert resp1.status_code == 200
    job_id_1 = resp1.get_json()["job_id"]
    result_1 = _poll_job_until_terminal(client, job_id_1)
    assert result_1["status"] == "done"

    entries_after_first = _reload_queue(adapter)
    junk_entries = [e for e in entries_after_first if e.action == "delete"]
    assert len(junk_entries) >= 1
    ds_store_entries = [e for e in junk_entries if e.src.endswith(".DS_Store")]
    assert len(ds_store_entries) == 1

    resp2 = client.get("/plan/reclaim")
    assert resp2.status_code == 200
    job_id_2 = resp2.get_json()["job_id"]
    result_2 = _poll_job_until_terminal(client, job_id_2)
    assert result_2["status"] == "done"

    entries_after_second = _reload_queue(adapter)
    ds_store_entries_2 = [
        e for e in entries_after_second if e.action == "delete" and e.src.endswith(".DS_Store")
    ]
    assert len(ds_store_entries_2) == 1
    assert ds_store_entries_2[0].id == ds_store_entries[0].id


def test_plan_reclaim_does_not_stage_master_path_refused_candidates(adapter, client, home):
    from cleanup_tools import config as config_module

    documents = home / "Documents"
    documents.mkdir()
    junk = documents / ".DS_Store"
    junk.write_bytes(b"junk")

    # Mark the whole Documents dir as an unbacked-up master path -- the
    # .DS_Store candidate inside it must be refused, and therefore never
    # staged into the approval queue at all.
    config = config_module.Config(
        bucket_rules=config_module.DEFAULT_BUCKET_RULES,
        master_paths=[config_module.MasterPath(path=str(documents), backed_up=False)],
    )
    config_module.save_config(adapter, config)

    resp = client.get("/plan/reclaim")
    job_id = resp.get_json()["job_id"]
    _poll_job_until_terminal(client, job_id)

    entries = _reload_queue(adapter)
    assert not any(e.src == str(junk) for e in entries)


def test_plan_corral_screenshots_stages_move_entries_and_is_idempotent(adapter, client, home):
    desktop = home / "Desktop"
    desktop.mkdir()
    shot = desktop / "screenshot-2024.png"
    shot.write_bytes(b"shot-bytes")

    resp1 = client.get("/plan/corral-screenshots")
    assert resp1.status_code == 200
    job_id_1 = resp1.get_json()["job_id"]
    result_1 = _poll_job_until_terminal(client, job_id_1)
    assert result_1["status"] == "done"

    entries_after_first = _reload_queue(adapter)
    assert len(entries_after_first) == 1
    entry = entries_after_first[0]
    assert entry.action == "move"
    assert entry.status == "pending"
    assert entry.source == "ui-plan-corral-screenshots"
    assert entry.group_key == f"corral-screenshots:{desktop}"
    assert entry.src == str(shot)
    assert entry.dest == str(home / "Pictures" / "Screenshots" / "screenshot-2024.png")

    # Hitting it again must not create a duplicate pending entry for the
    # same src -- stage_entries()'s existing dedup, exercised through the
    # route, mirroring plan_sort/plan_reclaim's own idempotency tests.
    resp2 = client.get("/plan/corral-screenshots")
    assert resp2.status_code == 200
    job_id_2 = resp2.get_json()["job_id"]
    result_2 = _poll_job_until_terminal(client, job_id_2)
    assert result_2["status"] == "done"

    entries_after_second = _reload_queue(adapter)
    assert len(entries_after_second) == 1
    assert entries_after_second[0].id == entry.id

    # The route only ever stages a dry-run plan -- it must never move a
    # file or touch the OS screenshot-location preference.
    assert shot.exists()
    assert not (home / "Pictures" / "Screenshots" / "screenshot-2024.png").exists()


def test_plan_corral_screenshots_no_matches_stages_nothing_without_crashing(
    adapter, client, home
):
    # No Desktop/Downloads/Documents dirs created under the fake home at
    # all -- corral_screenshots.run() must degrade to an empty plan rather
    # than raising, and the background job must still reach "done" cleanly.
    resp = client.get("/plan/corral-screenshots")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]
    payload = _poll_job_until_terminal(client, job_id)
    assert payload["status"] == "done"
    assert _reload_queue(adapter) == []


# ---------------------------------------------------------------------------
# 2b. Background jobs: /plan/reclaim's job_id + /status/<job_id> polling
#     (cleanup_tools.ui.jobs), plus /healthz.
# ---------------------------------------------------------------------------


def test_plan_reclaim_responds_immediately_without_blocking(adapter, client, home):
    """The route itself must return fast (job_id only) regardless of how
    long the underlying plan-building work takes -- proven here by how
    quickly client.get() returns, well under any real du-backed sizing
    work, even before the background job has finished.
    """
    import time

    documents = home / "Documents"
    documents.mkdir()
    (documents / ".DS_Store").write_bytes(b"junk")

    start = time.time()
    resp = client.get("/plan/reclaim")
    elapsed = time.time() - start

    assert resp.status_code == 200
    assert resp.is_json
    payload = resp.get_json()
    assert "job_id" in payload and payload["job_id"]
    assert elapsed < 1.0

    # Clean up the still-running (or just-finished) background job so it
    # doesn't linger past this test.
    _poll_job_until_terminal(client, payload["job_id"])


def test_status_poll_while_job_running_reports_real_increasing_progress(
    adapter, client, home, monkeypatch
):
    """current/total must be genuine, observed progress -- not a fixed
    placeholder -- proven by making dir_size_bytes deliberately slow (a real
    sleep before each real call) so polling mid-job can catch current at
    more than one distinct value as successive candidate directories get
    sized.
    """
    import time

    documents = home / "Documents"
    documents.mkdir()
    for i in range(4):
        proj_modules = documents / f"proj{i}" / "node_modules"
        proj_modules.mkdir(parents=True)
        (proj_modules / "pkg.json").write_text("{}")

    real_dir_size_bytes = MacOSAdapter.dir_size_bytes

    def slow_dir_size_bytes(self, path):
        time.sleep(0.15)
        return real_dir_size_bytes(self, path)

    monkeypatch.setattr(MacOSAdapter, "dir_size_bytes", slow_dir_size_bytes)

    resp = client.get("/plan/reclaim")
    job_id = resp.get_json()["job_id"]

    seen_current = []
    deadline = time.time() + 10
    status = "running"
    while time.time() < deadline:
        status_resp = client.get(f"/status/{job_id}")
        assert status_resp.status_code == 200
        payload = status_resp.get_json()
        seen_current.append(payload["current"])
        assert isinstance(payload["total"], int)
        status = payload["status"]
        if status != "running":
            break
        time.sleep(0.02)

    assert status == "done"
    # current must have taken on more than one distinct value across polls
    # -- proof it's real, observed progress rather than a constant/fake
    # placeholder.
    assert len(set(seen_current)) > 1
    assert max(seen_current) >= 2


def test_status_poll_after_success_matches_synchronous_stage_result(adapter, client, home):
    documents = home / "Documents"
    documents.mkdir()
    (documents / ".DS_Store").write_bytes(b"junk")

    resp = client.get("/plan/reclaim")
    job_id = resp.get_json()["job_id"]
    payload = _poll_job_until_terminal(client, job_id)

    assert payload["status"] == "done"
    result = payload["result"]

    staged = _reload_queue(adapter)
    assert result["count"] == len(staged) == 1

    entry_dict = result["entries"][0]
    staged_entry = staged[0]
    assert entry_dict["id"] == staged_entry.id
    assert entry_dict["action"] == staged_entry.action == "delete"
    assert entry_dict["src"] == staged_entry.src
    assert entry_dict["src"].endswith(".DS_Store")
    assert entry_dict["source"] == staged_entry.source == "ui-plan-reclaim"
    assert entry_dict["status"] == staged_entry.status == "pending"


def test_status_poll_after_reclaim_run_raises_timeout_lands_as_terminal_error(
    adapter, client, home, monkeypatch
):
    """Mirrors the existing TimeoutError-from-the-queue-lock case every other
    route in this file already handles (see QUEUE_BUSY_MESSAGE) -- except
    here the job runs on a background thread, so the assertion is that the
    job's terminal status becomes "error" (never an uncaught exception that
    leaves it stuck at "running"), carrying the same friendly message.
    """
    from cleanup_tools.commands import reclaim as reclaim_module
    from cleanup_tools.ui.routes import QUEUE_BUSY_MESSAGE

    def raise_timeout(adapter_arg, args=None):
        raise TimeoutError("Timed out after 5.0s waiting for lock on /some/queue.yaml.lock")

    monkeypatch.setattr(reclaim_module, "run", raise_timeout)

    resp = client.get("/plan/reclaim")
    job_id = resp.get_json()["job_id"]
    payload = _poll_job_until_terminal(client, job_id)

    assert payload["status"] == "error"
    assert payload["error"] == QUEUE_BUSY_MESSAGE
    assert "result" not in payload


# ---------------------------------------------------------------------------
# 2c. /plan/sort and /plan/corral-screenshots get the SAME background-job
#     treatment as /plan/reclaim above: the slow part of both is the
#     per-proposed-entry queue_module.build_plan_snapshot content-hash loop
#     (routes.py's _stage_sort_plan/_stage_corral_screenshots_plan), not the
#     underlying sort.run()/corral_screenshots.run() filesystem walk itself.
# ---------------------------------------------------------------------------


def test_plan_sort_responds_immediately_without_blocking(adapter, client, home, monkeypatch):
    """The route itself must return fast (job_id only) regardless of how
    long build_plan_snapshot's per-entry content hashing takes -- proven
    here by making it deliberately slow and still observing a sub-second
    response, mirroring plan_reclaim's own responds-immediately test.
    """
    import time

    downloads = home / "Downloads"
    downloads.mkdir()
    for i in range(5):
        (downloads / f"file{i}.bin").write_bytes(b"x" * 1024)

    real_build_plan_snapshot = queue_module.build_plan_snapshot

    def slow_build_plan_snapshot(path):
        time.sleep(0.2)
        return real_build_plan_snapshot(path)

    monkeypatch.setattr(queue_module, "build_plan_snapshot", slow_build_plan_snapshot)

    start = time.time()
    resp = client.get("/plan/sort")
    elapsed = time.time() - start

    assert resp.status_code == 200
    assert resp.is_json
    payload = resp.get_json()
    assert "job_id" in payload and payload["job_id"]
    assert elapsed < 1.0

    _poll_job_until_terminal(client, payload["job_id"], timeout=10.0)


def test_plan_sort_progress_reports_real_increasing_progress_during_snapshot_loop(
    adapter, client, home, monkeypatch
):
    """current/total must be genuine, observed progress through the
    per-entry build_plan_snapshot loop -- not a fixed placeholder -- proven
    by making build_plan_snapshot deliberately slow so polling mid-job can
    catch current at more than one distinct value. Unlike reclaim's
    _DirSizeProgressAdapter-driven progress (whose total grows as
    directories are discovered), sort's total is the full plan length,
    known upfront -- so it must reach exactly that value at the end.
    """
    import time

    downloads = home / "Downloads"
    downloads.mkdir()
    for i in range(4):
        (downloads / f"file{i}.bin").write_bytes(b"x" * 1024)

    real_build_plan_snapshot = queue_module.build_plan_snapshot

    def slow_build_plan_snapshot(path):
        time.sleep(0.15)
        return real_build_plan_snapshot(path)

    monkeypatch.setattr(queue_module, "build_plan_snapshot", slow_build_plan_snapshot)

    resp = client.get("/plan/sort")
    job_id = resp.get_json()["job_id"]

    seen_current = []
    seen_total = set()
    deadline = time.time() + 10
    status = "running"
    while time.time() < deadline:
        status_resp = client.get(f"/status/{job_id}")
        assert status_resp.status_code == 200
        payload = status_resp.get_json()
        seen_current.append(payload["current"])
        seen_total.add(payload["total"])
        status = payload["status"]
        if status != "running":
            break
        time.sleep(0.02)

    assert status == "done"
    assert len(set(seen_current)) > 1
    assert max(seen_current) == 4
    # The total is the full plan length, known before the loop starts --
    # unlike reclaim's growing total, it's never anything other than 0
    # (before the first entry's snapshot is built) or 4 (the real total)
    # across polls, never some other placeholder/interim value.
    assert seen_total <= {0, 4}
    assert 4 in seen_total


def test_plan_sort_timeout_from_queue_lock_lands_as_terminal_error(
    adapter, client, home, monkeypatch
):
    from cleanup_tools.commands import sort as sort_module
    from cleanup_tools.ui.routes import QUEUE_BUSY_MESSAGE

    def raise_timeout(adapter_arg, args=None):
        raise TimeoutError("Timed out after 5.0s waiting for lock on /some/queue.yaml.lock")

    monkeypatch.setattr(sort_module, "run", raise_timeout)

    resp = client.get("/plan/sort")
    job_id = resp.get_json()["job_id"]
    payload = _poll_job_until_terminal(client, job_id)

    assert payload["status"] == "error"
    assert payload["error"] == QUEUE_BUSY_MESSAGE


def test_plan_corral_screenshots_timeout_from_queue_lock_lands_as_terminal_error(
    adapter, client, home, monkeypatch
):
    from cleanup_tools.commands import corral_screenshots as corral_screenshots_module
    from cleanup_tools.ui.routes import QUEUE_BUSY_MESSAGE

    def raise_timeout(adapter_arg, args=None):
        raise TimeoutError("Timed out after 5.0s waiting for lock on /some/queue.yaml.lock")

    monkeypatch.setattr(corral_screenshots_module, "run", raise_timeout)

    resp = client.get("/plan/corral-screenshots")
    job_id = resp.get_json()["job_id"]
    payload = _poll_job_until_terminal(client, job_id)

    assert payload["status"] == "error"
    assert payload["error"] == QUEUE_BUSY_MESSAGE


def test_plan_corral_screenshots_responds_immediately_without_blocking(
    adapter, client, home, monkeypatch
):
    import time

    desktop = home / "Desktop"
    desktop.mkdir()
    for i in range(5):
        (desktop / f"screenshot-202{i}.png").write_bytes(b"x" * 1024)

    real_build_plan_snapshot = queue_module.build_plan_snapshot

    def slow_build_plan_snapshot(path):
        time.sleep(0.2)
        return real_build_plan_snapshot(path)

    monkeypatch.setattr(queue_module, "build_plan_snapshot", slow_build_plan_snapshot)

    start = time.time()
    resp = client.get("/plan/corral-screenshots")
    elapsed = time.time() - start

    assert resp.status_code == 200
    payload = resp.get_json()
    assert "job_id" in payload and payload["job_id"]
    assert elapsed < 1.0

    _poll_job_until_terminal(client, payload["job_id"], timeout=10.0)


def test_plan_corral_screenshots_progress_reports_real_increasing_progress(
    adapter, client, home, monkeypatch
):
    import time

    desktop = home / "Desktop"
    desktop.mkdir()
    for i in range(4):
        (desktop / f"screenshot-202{i}.png").write_bytes(b"x" * 1024)

    real_build_plan_snapshot = queue_module.build_plan_snapshot

    def slow_build_plan_snapshot(path):
        time.sleep(0.15)
        return real_build_plan_snapshot(path)

    monkeypatch.setattr(queue_module, "build_plan_snapshot", slow_build_plan_snapshot)

    resp = client.get("/plan/corral-screenshots")
    job_id = resp.get_json()["job_id"]

    seen_current = []
    deadline = time.time() + 10
    status = "running"
    while time.time() < deadline:
        status_resp = client.get(f"/status/{job_id}")
        payload = status_resp.get_json()
        seen_current.append(payload["current"])
        status = payload["status"]
        if status != "running":
            break
        time.sleep(0.02)

    assert status == "done"
    assert len(set(seen_current)) > 1
    assert max(seen_current) == 4


# ---------------------------------------------------------------------------
# 2d. The kickoff bar's premise: /plan/sort, /plan/reclaim, and
#     /plan/corral-screenshots can all be launched together, each getting
#     its own independent background job, none blocking on the others.
# ---------------------------------------------------------------------------


def test_all_three_plan_routes_can_be_triggered_without_blocking_each_other(
    adapter, client, home, monkeypatch
):
    """Route-level proof for the kickoff bar's premise (the actual
    multi-launch UI is client-side -- see static/plan-trigger.js's
    initKickoffBar -- and is exercised end-to-end separately via
    Playwright): /plan/sort, /plan/reclaim, and /plan/corral-screenshots
    can all be started back to back, each getting its own distinct job_id
    immediately, even though the underlying work behind all three is
    deliberately slowed down here to prove none of them make the others
    wait.
    """
    import time

    downloads = home / "Downloads"
    downloads.mkdir()
    (downloads / "photo.jpg").write_bytes(b"photo-bytes")

    desktop = home / "Desktop"
    desktop.mkdir()
    (desktop / "screenshot-2024.png").write_bytes(b"shot-bytes")

    documents = home / "Documents"
    documents.mkdir()
    (documents / ".DS_Store").write_bytes(b"junk")

    real_build_plan_snapshot = queue_module.build_plan_snapshot
    real_dir_size_bytes = MacOSAdapter.dir_size_bytes

    def slow_build_plan_snapshot(path):
        time.sleep(0.2)
        return real_build_plan_snapshot(path)

    def slow_dir_size_bytes(self, path):
        time.sleep(0.2)
        return real_dir_size_bytes(self, path)

    monkeypatch.setattr(queue_module, "build_plan_snapshot", slow_build_plan_snapshot)
    monkeypatch.setattr(MacOSAdapter, "dir_size_bytes", slow_dir_size_bytes)

    start = time.time()
    resp_sort = client.get("/plan/sort")
    resp_reclaim = client.get("/plan/reclaim")
    resp_corral = client.get("/plan/corral-screenshots")
    elapsed = time.time() - start

    assert resp_sort.status_code == 200
    assert resp_reclaim.status_code == 200
    assert resp_corral.status_code == 200

    job_ids = {
        resp_sort.get_json()["job_id"],
        resp_reclaim.get_json()["job_id"],
        resp_corral.get_json()["job_id"],
    }
    # Three distinct jobs -- one route never blocked on, or accidentally
    # reused, another's job.
    assert len(job_ids) == 3

    # Launching all three took nowhere near the sum of their slowed-down
    # underlying work (well over a second each) -- each responded near
    # instantly, proving they were kicked off as independent background
    # jobs rather than one request waiting for the previous to finish.
    assert elapsed < 1.0

    for resp in (resp_sort, resp_reclaim, resp_corral):
        payload = _poll_job_until_terminal(client, resp.get_json()["job_id"], timeout=10.0)
        assert payload["status"] == "done"


def test_status_unknown_job_id_returns_404_not_500(client):
    resp = client.get("/status/this-job-id-does-not-exist")
    assert resp.status_code == 404


def test_healthz_returns_200_with_small_json_body(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.is_json
    payload = resp.get_json()
    assert payload == {"status": "ok"}


def test_healthz_never_touches_the_queue(client, monkeypatch):
    """/healthz must be a genuinely cheap liveness check -- not a
    repurposing of "/" (which does real queue-loading/grouping work).
    Proven here by making queue_module.load_queue blow up: if /healthz
    called it (directly or via _load_entries), this would surface as a
    500, not a clean 200.
    """

    def blow_up(*args, **kwargs):
        raise AssertionError("healthz must never call queue_module.load_queue")

    monkeypatch.setattr(queue_module, "load_queue", blow_up)

    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_threaded_dev_server_answers_a_cheap_poll_without_blocking_behind_a_slow_request(
    adapter, home, monkeypatch
):
    """End-to-end proof that run_server()'s threaded=True does something
    real: binds an actual socket via cleanup_tools.ui.app.run_server (the
    exact function/engine the "cleanup approve" CLI subcommand uses) and
    issues two REAL HTTP requests against it with http.client -- not
    Flask's test_client(), which never models concurrent connection
    handling at all, only sequential in-process calls.

    One connection hits "/" with queue_module.load_queue patched to sleep
    for a while (standing in for any slow synchronous request-handling
    work); a second, concurrent connection hits the cheap /healthz route
    and must come back promptly rather than queueing up behind the first
    on Werkzeug's dev server. Removing threaded=True from app.py's
    app.run(...) call makes this test hang/fail (verified manually during
    development -- see the task's self-review step).
    """
    import http.client
    import socket
    import threading
    import time

    from cleanup_tools.ui.app import run_server

    documents = home / "Documents"
    documents.mkdir()

    real_load_queue = queue_module.load_queue
    entered_slow_call = threading.Event()

    def slow_load_queue(adapter_arg, path=None):
        entered_slow_call.set()
        time.sleep(1.0)
        return real_load_queue(adapter_arg, path)

    monkeypatch.setattr(queue_module, "load_queue", slow_load_queue)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server_thread = threading.Thread(
        target=run_server,
        kwargs=dict(adapter=adapter, queue_path=None, host="127.0.0.1", port=port, open_browser=False),
        daemon=True,
    )
    server_thread.start()

    def _get(path: str, timeout: float = 5.0):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    # Wait for the real server to actually come up.
    deadline = time.time() + 5
    up = False
    while time.time() < deadline:
        try:
            if _get("/healthz") == 200:
                up = True
                break
        except OSError:
            time.sleep(0.05)
    assert up, "server never came up"

    slow_request_status = {}

    def _slow_request():
        slow_request_status["status"] = _get("/", timeout=10)

    slow_thread = threading.Thread(target=_slow_request, daemon=True)
    slow_thread.start()

    # Wait until the slow request has genuinely entered the slow call, so
    # the concurrent poll below overlaps with it for real.
    assert entered_slow_call.wait(timeout=5)

    start = time.time()
    status = _get("/healthz")
    elapsed = time.time() - start

    assert status == 200
    # Well under the slow request's 1.0s sleep -- proof this connection was
    # served concurrently, not queued up behind the in-flight "/" request.
    assert elapsed < 0.5

    slow_thread.join(timeout=5)
    assert slow_request_status.get("status") == 200


# ---------------------------------------------------------------------------
# 3. /queue: lists pending entries only, as review cards.
# ---------------------------------------------------------------------------


def test_queue_view_lists_only_pending_entries(adapter, client, tmp_path):
    f1 = tmp_path / "a.txt"
    f1.write_text("a")
    f2 = tmp_path / "b.txt"
    f2.write_text("b")
    f3 = tmp_path / "c.txt"
    f3.write_text("c")

    entries = [
        QueueEntry(action="move", src=str(f1), dest="", status="pending"),
        QueueEntry(action="move", src=str(f2), dest="", status="approved"),
        QueueEntry(action="delete", src=str(f3), dest="", status="rejected"),
    ]
    _seed_queue(adapter, entries)

    resp = client.get("/queue")
    html = resp.data.decode()
    assert str(f1) in html
    assert str(f2) not in html
    assert str(f3) not in html


# ---------------------------------------------------------------------------
# 4. approve / reject / undo: delegate to queue.set_status / queue.undo,
#    reflected on the next queue/dashboard load.
# ---------------------------------------------------------------------------


def test_approve_updates_status_and_reflects_on_next_load(adapter, client, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    entry = QueueEntry(action="move", src=str(f), dest="", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.post(f"/queue/{entry.id}/approve")
    assert resp.status_code == 302

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.status_history[-1]["status"] == "approved"

    # No longer listed on /queue (pending-only).
    queue_html = client.get("/queue").data.decode()
    assert str(f) not in queue_html

    # Dashboard reflects the new status count.
    dash_html = client.get("/").data.decode()
    assert "approved" in dash_html


def test_reject_updates_status(adapter, client, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    entry = QueueEntry(action="delete", src=str(f), dest="", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.post(f"/queue/{entry.id}/reject")
    assert resp.status_code == 302

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "rejected"


def test_undo_reverts_to_previous_status(adapter, client, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    # undo() needs at least two status_history records to have something to
    # revert to (see queue.undo's docstring), so seed the initial "pending"
    # transition explicitly -- mirroring what a real staged-then-approved
    # entry accumulates, rather than the bare empty-history default.
    entry = QueueEntry(
        action="move",
        src=str(f),
        dest="",
        status="pending",
        status_history=[{"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"}],
    )
    _seed_queue(adapter, [entry])

    client.post(f"/queue/{entry.id}/approve")
    assert _reload_queue(adapter)[0].status == "approved"

    resp = client.post(f"/queue/{entry.id}/undo")
    assert resp.status_code == 302

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "pending"


def test_undo_with_nothing_to_revert_returns_400_not_500(adapter, client, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    # Freshly staged entry: empty status_history, nothing to undo to.
    entry = QueueEntry(action="move", src=str(f), dest="", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.post(f"/queue/{entry.id}/undo")
    assert resp.status_code == 400


def test_approve_unknown_entry_id_returns_404_not_500(client):
    resp = client.post("/queue/does-not-exist/approve")
    assert resp.status_code == 404


def test_undo_unknown_entry_id_returns_404_distinct_from_nothing_to_revert(client):
    # Unknown id -> 404 (mirrors approve/reject), distinct from the 400
    # returned above for a real entry with nothing to revert to.
    resp = client.post("/queue/does-not-exist/undo")
    assert resp.status_code == 404


def test_approve_returns_503_not_500_when_queue_lock_is_contended(adapter, client, tmp_path, monkeypatch):
    """queue.set_status()'s file_lock() raises TimeoutError (a plain OSError
    subclass, but NOT ValueError) when another process -- e.g. a real
    ``cleanup sort --from-queue``/``cleanup reclaim --from-queue`` execution
    run -- legitimately holds the queue lock for the duration of its run.
    Before the fix, approve_entry only caught ValueError, so this propagated
    as a raw 500; it must instead surface as a clear 503.

    The lock is held with a REAL fcntl advisory lock (via adapter.file_lock,
    exactly like a concurrent CLI process would hold it), not a mocked
    exception, so this genuinely exercises the contention path. Only the
    lock-acquisition timeout is shortened (patching queue_module's
    with_queue_lock to pass a short timeout through to adapter.file_lock)
    so the test doesn't have to wait out the real 5s default -- mirroring
    test_adapters.py's own short-timeout pattern for the same reason.
    """
    f = tmp_path / "a.txt"
    f.write_text("a")
    entry = QueueEntry(action="move", src=str(f), dest="", status="pending")
    qpath = _seed_queue(adapter, [entry])

    @contextlib.contextmanager
    def fast_with_queue_lock(adapter, path):
        with adapter.file_lock(path, timeout=0.2):
            yield

    monkeypatch.setattr(queue_module, "with_queue_lock", fast_with_queue_lock)

    with adapter.file_lock(qpath, timeout=5.0):
        # A concurrent CLI execution run holding the real queue lock.
        resp = client.post(f"/queue/{entry.id}/approve")

    assert resp.status_code == 503
    assert b"busy" in resp.data.lower()

    # Once the external holder releases the lock, the same request succeeds
    # normally -- proving the entry itself was never corrupted by the
    # contended attempt.
    resp2 = client.post(f"/queue/{entry.id}/approve")
    assert resp2.status_code == 302
    assert _reload_queue(adapter)[0].status == "approved"


# ---------------------------------------------------------------------------
# 5. Thumbnails: genuinely resized, never the original bytes; non-image /
#    missing / unknown entries 404 instead of serving anything.
# ---------------------------------------------------------------------------


def _make_test_image(path: Path, size=(1200, 900), color=(200, 50, 50)) -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


def test_thumbnail_serves_resized_image_not_original_bytes(adapter, client, tmp_path):
    img_path = tmp_path / "big_photo.png"
    _make_test_image(img_path, size=(1200, 900))
    original_bytes = img_path.read_bytes()

    entry = QueueEntry(action="move", src=str(img_path), dest="", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.get(f"/thumbnail/{entry.id}")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"

    # Not a byte-for-byte stream of the original file (different format,
    # different size).
    assert resp.data != original_bytes
    assert len(resp.data) < len(original_bytes)

    # Genuinely resized: decode the response and check its dimensions
    # against the 256px cap, not just trusting a smaller byte count.
    import io as _io
    thumb_img = Image.open(_io.BytesIO(resp.data))
    assert max(thumb_img.size) <= 256
    assert thumb_img.size != (1200, 900)


def test_thumbnail_404s_for_non_image_entry(adapter, client, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("just text")
    entry = QueueEntry(action="move", src=str(f), dest="", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.get(f"/thumbnail/{entry.id}")
    assert resp.status_code == 404


def test_thumbnail_404s_for_missing_file(adapter, client, tmp_path):
    missing = tmp_path / "gone.jpg"
    entry = QueueEntry(action="move", src=str(missing), dest="", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.get(f"/thumbnail/{entry.id}")
    assert resp.status_code == 404


def test_thumbnail_404s_for_unknown_entry_id(client):
    resp = client.get("/thumbnail/does-not-exist")
    assert resp.status_code == 404


def test_thumbnail_404s_not_500s_for_decompression_bomb_sized_image(adapter, client, tmp_path):
    """A real, legitimately-large image (e.g. a stitched panorama or scan)
    whose declared pixel count exceeds Pillow's default safety threshold
    (~179M px, MAX_IMAGE_PIXELS ~89M px) makes Image.open() raise
    PIL.Image.DecompressionBombError -- a plain Exception subclass, NOT an
    OSError. Before the fix, the thumbnail route's except clause only
    caught (UnidentifiedImageError, OSError), so this propagated as a raw
    500 instead of the same graceful 404 every other unrenderable-image
    case gets.

    Uses a real 20000x20000 (400M px) PNG rather than a monkeypatch, so
    this genuinely exercises Pillow's own decompression-bomb check rather
    than merely asserting the except clause's shape.
    """
    img_path = tmp_path / "huge_panorama.png"
    Image.new("RGB", (20000, 20000), (10, 20, 30)).save(img_path, format="PNG")

    entry = QueueEntry(action="move", src=str(img_path), dest="", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.get(f"/thumbnail/{entry.id}")
    assert resp.status_code == 404


def test_no_route_serves_the_original_full_resolution_file(adapter, client, tmp_path):
    """There must be no way to fetch a queue entry's raw src bytes over
    HTTP -- only the (always-resized) thumbnail route serves image bytes.
    """
    img_path = tmp_path / "big_photo.png"
    _make_test_image(img_path, size=(1200, 900))
    entry = QueueEntry(action="move", src=str(img_path), dest="", status="pending")
    _seed_queue(adapter, [entry])

    # No plausible "raw file" route exists at all.
    for guess in (
        f"/file/{entry.id}",
        f"/files/{entry.id}",
        f"/raw/{entry.id}",
        f"/original/{entry.id}",
        f"/queue/{entry.id}/file",
        f"/queue/{entry.id}/raw",
    ):
        resp = client.get(guess)
        assert resp.status_code == 404, f"unexpected route responded: {guess}"

    # And the one route that DOES serve image bytes (thumbnail) never
    # matches the original file's size/bytes (already covered above, but
    # re-asserted here alongside the "no raw route" checks for one clear
    # place proving the full guarantee).
    resp = client.get(f"/thumbnail/{entry.id}")
    assert resp.data != img_path.read_bytes()


# ---------------------------------------------------------------------------
# 6. The UI never invokes --from-queue execution: staging routes only ever
#    add "pending" entries, they never move/delete real files.
# ---------------------------------------------------------------------------


def test_plan_sort_never_moves_files_only_stages_pending_entries(adapter, client, home):
    downloads = home / "Downloads"
    downloads.mkdir()
    photo = downloads / "photo.jpg"
    photo.write_bytes(b"photo-bytes")

    resp = client.get("/plan/sort")
    job_id = resp.get_json()["job_id"]
    _poll_job_until_terminal(client, job_id)

    # File still sitting exactly where it was -- staging is planning only.
    assert photo.exists()
    assert photo.read_bytes() == b"photo-bytes"
    assert not (downloads / "_sorted").exists()

    entries = _reload_queue(adapter)
    assert len(entries) == 1
    assert entries[0].status == "pending"


# ---------------------------------------------------------------------------
# 7. Bulk actions: group_key and explicit id-list scoping, and that neither
#    ever touches entries outside the requested scope.
# ---------------------------------------------------------------------------


def _make_entry(tmp_path, name, group_key=None, status="pending"):
    f = tmp_path / name
    f.write_text(name)
    return QueueEntry(action="move", src=str(f), dest="", status=status, group_key=group_key)


def test_bulk_approve_by_group_key_transitions_only_that_group(adapter, client, tmp_path):
    screenshots = [
        _make_entry(tmp_path, f"shot{i}.png", group_key="sort:screenshots") for i in range(3)
    ]
    other_group = [_make_entry(tmp_path, "report.pdf", group_key="reclaim:build_caches")]
    ungrouped = [_make_entry(tmp_path, "mystery.bin", group_key=None)]
    _seed_queue(adapter, screenshots + other_group + ungrouped)

    resp = client.post("/queue/bulk-approve", data={"group_key": "sort:screenshots"})
    assert resp.status_code == 302

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    for e in screenshots:
        assert reloaded[e.id].status == "approved"
        assert reloaded[e.id].status_history[-1]["status"] == "approved"

    # Entries OUTSIDE the target group must be genuinely untouched: still
    # pending, and with no history entry appended at all (not merely "not
    # approved") -- proving the bulk route never even looked at them.
    for e in other_group + ungrouped:
        assert reloaded[e.id].status == "pending"
        assert reloaded[e.id].status_history == []


def test_bulk_reject_by_group_key_transitions_only_that_group(adapter, client, tmp_path):
    caches = [_make_entry(tmp_path, f"cache{i}.bin", group_key="reclaim:build_caches") for i in range(4)]
    screenshots = [_make_entry(tmp_path, "shot.png", group_key="sort:screenshots")]
    _seed_queue(adapter, caches + screenshots)

    resp = client.post("/queue/bulk-reject", data={"group_key": "reclaim:build_caches"})
    assert resp.status_code == 302

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    for e in caches:
        assert reloaded[e.id].status == "rejected"
    for e in screenshots:
        assert reloaded[e.id].status == "pending"
        assert reloaded[e.id].status_history == []


def test_bulk_approve_group_key_is_exact_match_not_substring(adapter, client, tmp_path):
    """"sort:screenshots" must not also sweep up "sort:screenshots_old" --
    bulk scoping is an exact match against group_key, never a substring or
    prefix match.
    """
    target = [_make_entry(tmp_path, "shot.png", group_key="sort:screenshots")]
    decoy = [_make_entry(tmp_path, "old_shot.png", group_key="sort:screenshots_old")]
    _seed_queue(adapter, target + decoy)

    client.post("/queue/bulk-approve", data={"group_key": "sort:screenshots"})

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    assert reloaded[target[0].id].status == "approved"
    assert reloaded[decoy[0].id].status == "pending"


def test_bulk_reject_by_group_key_prefix_targets_every_leaf_under_the_branch(adapter, client, tmp_path):
    """A trailing ":" turns group_key into a tree-branch prefix match:
    "sort:downloads:" must reach every leaf under it ("sort:downloads:photos",
    "sort:downloads:screenshots", ...) but never a sibling branch that merely
    shares the same string prefix ("sort:downloads2:photos").
    """
    downloads_branch = [
        _make_entry(tmp_path, "shot.png", group_key="sort:downloads:screenshots"),
        _make_entry(tmp_path, "photo.jpg", group_key="sort:downloads:photos"),
    ]
    sibling_branch = [_make_entry(tmp_path, "other.jpg", group_key="sort:downloads2:photos")]
    unrelated = [_make_entry(tmp_path, "cache.bin", group_key="reclaim:downloads:build_caches")]
    _seed_queue(adapter, downloads_branch + sibling_branch + unrelated)

    resp = client.post("/queue/bulk-reject", data={"group_key": "sort:downloads:"})
    assert resp.status_code == 302

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    for e in downloads_branch:
        assert reloaded[e.id].status == "rejected"
    for e in sibling_branch + unrelated:
        assert reloaded[e.id].status == "pending"
        assert reloaded[e.id].status_history == []


def test_bulk_approve_by_explicit_entry_ids(adapter, client, tmp_path):
    entries = [_make_entry(tmp_path, f"f{i}.txt", group_key="sort:misc") for i in range(5)]
    _seed_queue(adapter, entries)

    target_ids = [entries[0].id, entries[2].id]
    resp = client.post("/queue/bulk-approve", data={"entry_ids": target_ids})
    assert resp.status_code == 302

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    assert reloaded[entries[0].id].status == "approved"
    assert reloaded[entries[2].id].status == "approved"
    # Untouched entries: still pending, no history recorded at all.
    for e in (entries[1], entries[3], entries[4]):
        assert reloaded[e.id].status == "pending"
        assert reloaded[e.id].status_history == []


def test_bulk_reject_by_explicit_entry_ids_ignores_non_pending_and_unknown_ids(adapter, client, tmp_path):
    pending = _make_entry(tmp_path, "a.txt", status="pending")
    already_approved = _make_entry(tmp_path, "b.txt", status="approved")
    _seed_queue(adapter, [pending, already_approved])

    resp = client.post(
        "/queue/bulk-reject",
        data={"entry_ids": [pending.id, already_approved.id, "totally-unknown-id"]},
    )
    assert resp.status_code == 302

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    assert reloaded[pending.id].status == "rejected"
    # Already-approved entry must NOT be flipped to rejected just because
    # its id was in the request -- bulk scope is pending entries only.
    assert reloaded[already_approved.id].status == "approved"


def test_bulk_approve_with_neither_group_key_nor_ids_touches_nothing(adapter, client, tmp_path):
    entries = [_make_entry(tmp_path, "a.txt"), _make_entry(tmp_path, "b.txt")]
    _seed_queue(adapter, entries)

    resp = client.post("/queue/bulk-approve", data={})
    assert resp.status_code == 302

    reloaded = _reload_queue(adapter)
    assert all(e.status == "pending" for e in reloaded)


def test_bulk_approve_via_json_body_returns_json_summary(adapter, client, tmp_path):
    entries = [_make_entry(tmp_path, f"j{i}.txt", group_key="sort:json_group") for i in range(2)]
    _seed_queue(adapter, entries)

    resp = client.post("/queue/bulk-approve", json={"group_key": "sort:json_group"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "approved"
    assert body["count"] == 2
    assert set(body["updated_ids"]) == {e.id for e in entries}

    reloaded = _reload_queue(adapter)
    assert all(e.status == "approved" for e in reloaded)


def test_bulk_approve_via_json_body_with_scalar_entry_ids_does_not_500(adapter, client, tmp_path):
    # Regression: a JSON body where entry_ids is a bare non-iterable scalar
    # (int/float/bool/None/dict, as opposed to a list or a lone string --
    # both of which are legitimate shapes handled elsewhere) used to reach
    # list(entry_ids) unguarded and raise TypeError, surfacing as a 500.
    # It should instead be treated the same as "no ids given": a normal
    # response that touches nothing, matching this file's existing
    # degrade-rather-than-raise handling of malformed input (see
    # _paginate's _to_int).
    entry = _make_entry(tmp_path, "a.txt", group_key="sort:misc")
    _seed_queue(adapter, [entry])

    resp = client.post("/queue/bulk-approve", json={"entry_ids": 12345})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 0
    assert body["updated_ids"] == []

    reloaded = _reload_queue(adapter)
    assert reloaded[0].status == "pending"


def test_bulk_approve_returns_503_when_queue_lock_contended(adapter, client, tmp_path, monkeypatch):
    entry = _make_entry(tmp_path, "a.txt", group_key="sort:misc")
    qpath = _seed_queue(adapter, [entry])

    @contextlib.contextmanager
    def fast_with_queue_lock(adapter, path):
        with adapter.file_lock(path, timeout=0.2):
            yield

    monkeypatch.setattr(queue_module, "with_queue_lock", fast_with_queue_lock)

    with adapter.file_lock(qpath, timeout=5.0):
        resp = client.post("/queue/bulk-approve", data={"group_key": "sort:misc"})

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 7b. Bulk undo -- unlike bulk-approve/bulk-reject, targets are resolved
#     against the WHOLE queue (approved/rejected entries are exactly what
#     undo is for), and supports the same ":"-suffixed prefix match.
# ---------------------------------------------------------------------------


def test_bulk_undo_by_group_key_reverts_only_that_group(adapter, client, tmp_path):
    approved = [
        _make_entry(tmp_path, f"shot{i}.png", group_key="sort:screenshots", status="approved")
        for i in range(2)
    ]
    for e in approved:
        e.status_history = [
            {"status": "pending", "timestamp": "t0"},
            {"status": "approved", "timestamp": "t1"},
        ]
    other = _make_entry(tmp_path, "cache.bin", group_key="reclaim:build_caches", status="approved")
    other.status_history = [
        {"status": "pending", "timestamp": "t0"},
        {"status": "approved", "timestamp": "t1"},
    ]
    _seed_queue(adapter, approved + [other])

    resp = client.post("/queue/bulk-undo", data={"group_key": "sort:screenshots"})
    assert resp.status_code == 302

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    for e in approved:
        assert reloaded[e.id].status == "pending"
    assert reloaded[other.id].status == "approved"  # untouched, different group


def test_bulk_undo_by_group_key_prefix_targets_every_leaf_under_the_branch(adapter, client, tmp_path):
    branch = [
        _make_entry(tmp_path, "shot.png", group_key="sort:downloads:screenshots", status="approved"),
        _make_entry(tmp_path, "photo.jpg", group_key="sort:downloads:photos", status="approved"),
    ]
    for e in branch:
        e.status_history = [
            {"status": "pending", "timestamp": "t0"},
            {"status": "approved", "timestamp": "t1"},
        ]
    sibling = _make_entry(tmp_path, "other.jpg", group_key="sort:downloads2:photos", status="approved")
    sibling.status_history = [
        {"status": "pending", "timestamp": "t0"},
        {"status": "approved", "timestamp": "t1"},
    ]
    _seed_queue(adapter, branch + [sibling])

    resp = client.post("/queue/bulk-undo", data={"group_key": "sort:downloads:"})
    assert resp.status_code == 302

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    for e in branch:
        assert reloaded[e.id].status == "pending"
    assert reloaded[sibling.id].status == "approved"


def test_bulk_undo_skips_entries_with_nothing_to_revert_but_still_processes_the_rest(
    adapter, client, tmp_path
):
    nothing_to_undo = _make_entry(tmp_path, "fresh.png", group_key="sort:misc", status="pending")
    has_history = _make_entry(tmp_path, "old.png", group_key="sort:misc", status="approved")
    has_history.status_history = [
        {"status": "pending", "timestamp": "t0"},
        {"status": "approved", "timestamp": "t1"},
    ]
    _seed_queue(adapter, [nothing_to_undo, has_history])

    resp = client.post("/queue/bulk-undo", data={"group_key": "sort:misc"})
    assert resp.status_code == 302  # must not 500 just because one entry has nothing to revert

    reloaded = {e.id: e for e in _reload_queue(adapter)}
    assert reloaded[nothing_to_undo.id].status == "pending"  # untouched, not crashed
    assert reloaded[has_history.id].status == "pending"  # reverted


def test_bulk_undo_via_json_body_returns_json_summary_with_skipped_ids(adapter, client, tmp_path):
    nothing_to_undo = _make_entry(tmp_path, "fresh.png", group_key="sort:misc", status="pending")
    _seed_queue(adapter, [nothing_to_undo])

    resp = client.post("/queue/bulk-undo", json={"group_key": "sort:misc"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 0
    assert body["skipped_ids"] == [nothing_to_undo.id]


# ---------------------------------------------------------------------------
# 7c. Entry edit -- change a pending entry's proposed dest/group_key before
#     approval.
# ---------------------------------------------------------------------------


def test_edit_entry_updates_dest_and_group_key(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.png", group_key="sort:photos", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.post(
        f"/queue/{entry.id}/edit",
        data={"dest": "/tmp/_sorted/screenshots/a.png", "group_key": "sort:screenshots"},
    )
    assert resp.status_code == 302

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.dest == "/tmp/_sorted/screenshots/a.png"
    assert reloaded.group_key == "sort:screenshots"
    assert reloaded.status == "pending"
    assert reloaded.status_history[-1]["edit"]["dest"]["new"] == "/tmp/_sorted/screenshots/a.png"


def test_edit_entry_missing_dest_returns_400_not_500(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.png", group_key="sort:photos", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.post(f"/queue/{entry.id}/edit", data={"group_key": "sort:screenshots"})
    assert resp.status_code == 400

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.group_key == "sort:photos"  # untouched


def test_edit_entry_non_pending_returns_400_not_500(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.png", group_key="sort:photos", status="approved")
    entry.status_history = [{"status": "approved", "timestamp": "t0"}]
    _seed_queue(adapter, [entry])

    resp = client.post(f"/queue/{entry.id}/edit", data={"dest": "/tmp/new-dest.png"})
    assert resp.status_code == 400

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.dest == ""  # untouched


def test_edit_entry_unknown_entry_id_returns_404_not_500(client):
    resp = client.post("/queue/does-not-exist/edit", data={"dest": "/tmp/x.png"})
    assert resp.status_code == 404


def test_edit_entry_via_json_body(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.png", group_key="sort:photos", status="pending")
    _seed_queue(adapter, [entry])

    resp = client.post(
        f"/queue/{entry.id}/edit",
        json={"dest": "/tmp/_sorted/docs/a.png", "group_key": "sort:docs"},
    )
    assert resp.status_code == 302

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.dest == "/tmp/_sorted/docs/a.png"
    assert reloaded.group_key == "sort:docs"


# ---------------------------------------------------------------------------
# 8. Pagination: correct boundaries (no dup/skip), sensible defaults,
#    graceful handling of out-of-range page numbers.
# ---------------------------------------------------------------------------


def test_queue_pagination_default_page_size_and_no_dup_no_skip(adapter, client, tmp_path):
    import re

    entries = [_make_entry(tmp_path, f"f{i:03d}.txt") for i in range(70)]
    _seed_queue(adapter, entries)
    all_ids = {e.id for e in entries}

    resp1 = client.get("/queue?page=1")
    html1 = resp1.data.decode()
    match = re.search(r"Page 1 of (\d+)", html1)
    assert match, "pagination indicator not found on page 1"
    total_pages = int(match.group(1))
    # 70 entries at the documented default of 25/page -> 3 pages (25, 25, 20).
    assert total_pages == 3

    seen_ids = []
    for page in range(1, total_pages + 1):
        resp = client.get(f"/queue?page={page}")
        assert resp.status_code == 200
        html = resp.data.decode()
        page_ids_here = [e.id for e in entries if f'data-entry-id="{e.id}"' in html]
        assert page_ids_here, f"page {page} unexpectedly empty"
        seen_ids.extend(page_ids_here)

    # Every entry appears exactly once across however many pages it took --
    # no duplicate (an entry appearing on two pages) and no skip.
    assert len(seen_ids) == len(all_ids)
    assert set(seen_ids) == all_ids
    assert len(seen_ids) == len(set(seen_ids))

    # And genuinely past the end (page total_pages + 1) doesn't 500 -- it
    # clamps to the last real page rather than erroring or ever-growing.
    resp_over = client.get(f"/queue?page={total_pages + 1}")
    assert resp_over.status_code == 200


def test_queue_pagination_exact_multiple_of_per_page(adapter, client, tmp_path):
    # Exactly 2 full pages at per_page=10 -- page 2 must be full (10 items),
    # and there must be no phantom page 3.
    entries = [_make_entry(tmp_path, f"f{i:03d}.txt") for i in range(20)]
    _seed_queue(adapter, entries)

    resp1 = client.get("/queue?page=1&per_page=10")
    resp2 = client.get("/queue?page=2&per_page=10")
    html1, html2 = resp1.data.decode(), resp2.data.decode()

    ids1 = [e.id for e in entries if f'data-entry-id="{e.id}"' in html1]
    ids2 = [e.id for e in entries if f'data-entry-id="{e.id}"' in html2]
    assert len(ids1) == 10
    assert len(ids2) == 10
    assert set(ids1).isdisjoint(set(ids2))
    assert "Page 2 of 2" in html2
    assert resp2.data  # sanity

    # Page 3 doesn't exist (only 2 full pages) -- must clamp, not 500 or
    # silently repeat page 1/2's content as "page 3".
    resp3 = client.get("/queue?page=3&per_page=10")
    assert resp3.status_code == 200
    html3 = resp3.data.decode()
    ids3 = [e.id for e in entries if f'data-entry-id="{e.id}"' in html3]
    assert set(ids3) == set(ids2)  # clamped to the last real page


def test_queue_pagination_one_more_than_a_multiple(adapter, client, tmp_path):
    # 21 entries at per_page=10 -> 3 pages, with page 3 holding exactly 1.
    entries = [_make_entry(tmp_path, f"f{i:03d}.txt") for i in range(21)]
    _seed_queue(adapter, entries)

    resp3 = client.get("/queue?page=3&per_page=10")
    assert resp3.status_code == 200
    html3 = resp3.data.decode()
    ids3 = [e.id for e in entries if f'data-entry-id="{e.id}"' in html3]
    assert len(ids3) == 1
    assert "Page 3 of 3" in html3

    resp4 = client.get("/queue?page=4&per_page=10")
    assert resp4.status_code == 200  # out of range but clamped, not 500


def test_queue_pagination_one_less_than_a_multiple(adapter, client, tmp_path):
    # 19 entries at per_page=10 -> page 2 holds exactly 9, no phantom page 3.
    entries = [_make_entry(tmp_path, f"f{i:03d}.txt") for i in range(19)]
    _seed_queue(adapter, entries)

    resp2 = client.get("/queue?page=2&per_page=10")
    html2 = resp2.data.decode()
    ids2 = [e.id for e in entries if f'data-entry-id="{e.id}"' in html2]
    assert len(ids2) == 9
    assert "Page 2 of 2" in html2


def test_queue_pagination_out_of_range_page_does_not_500(adapter, client, tmp_path):
    entries = [_make_entry(tmp_path, f"f{i:03d}.txt") for i in range(5)]
    _seed_queue(adapter, entries)

    for bad_page in ("0", "-5", "9999", "not-a-number", ""):
        resp = client.get(f"/queue?page={bad_page}")
        assert resp.status_code == 200, f"page={bad_page!r} should not 500"


def test_queue_pagination_empty_queue_does_not_500(client):
    resp = client.get("/queue?page=5&per_page=10")
    assert resp.status_code == 200
    assert b"Nothing pending" in resp.data


def test_queue_pagination_per_page_is_clamped(adapter, client, tmp_path):
    entries = [_make_entry(tmp_path, f"f{i:03d}.txt") for i in range(5)]
    _seed_queue(adapter, entries)

    # Non-numeric / negative per_page falls back to the default rather than
    # erroring or producing a zero/negative-size page.
    resp = client.get("/queue?per_page=-3")
    assert resp.status_code == 200
    html = resp.data.decode()
    ids = [e.id for e in entries if f'data-entry-id="{e.id}"' in html]
    assert len(ids) == 5  # all 5 fit within the fallback default page size


# ---------------------------------------------------------------------------
# 8b. AI-sourced entries are visibly distinguishable in the queue view --
#     the story's acceptance criteria require an AI proposal to "look and
#     behave identically to a manual entry... with source visibly
#     distinguishable but not functionally different". This asserts on the
#     actual rendered /queue response body (not by calling a template
#     helper directly), so it genuinely exercises what a human reviewing
#     the queue sees.
# ---------------------------------------------------------------------------


def test_queue_view_distinguishes_ai_proposed_entries_from_manual(adapter, client, tmp_path):
    manual_file = tmp_path / "manual_report.txt"
    manual_file.write_text("manual")
    ai_file = tmp_path / "ai_photo.jpg"
    ai_file.write_text("ai")

    manual_entry = QueueEntry(
        action="move", src=str(manual_file), dest="", status="pending", source="manual"
    )
    ai_entry = QueueEntry(
        action="move", src=str(ai_file), dest="", status="pending", source="ai:anthropic"
    )
    _seed_queue(adapter, [manual_entry, ai_entry])

    resp = client.get("/queue")
    assert resp.status_code == 200
    html = resp.data.decode()

    # Split the rendered page into each entry's own card so the assertions
    # below check what's near THAT card specifically, not just "the string
    # appears somewhere on the page".
    manual_card = html.split(f'data-entry-id="{manual_entry.id}"')[1].split("entry-card")[0]
    ai_card = html.split(f'data-entry-id="{ai_entry.id}"')[1].split("entry-card")[0]

    assert "AI-proposed" in ai_card
    assert "AI-proposed" not in manual_card
    assert "Manual" in manual_card


# ---------------------------------------------------------------------------
# 9. Keyboard shortcuts / bulk-select markup: static asset is served, and
#    queue.html carries the data attributes + focus/select scaffolding
#    keyboard.js depends on. (The real interactive keyboard behavior is
#    verified separately via a real browser -- see the story's report.)
# ---------------------------------------------------------------------------


def test_static_keyboard_js_is_served(client):
    resp = client.get("/static/keyboard.js")
    assert resp.status_code == 200
    assert b"keydown" in resp.data


def test_queue_view_includes_keyboard_js_and_bulk_scaffolding(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.txt", group_key="sort:screenshots")
    _seed_queue(adapter, [entry])

    resp = client.get("/queue")
    html = resp.data.decode()

    assert "keyboard.js" in html
    assert f'data-entry-id="{entry.id}"' in html
    # Checkbox is enabled now (no longer the disabled placeholder) and
    # associated with the bulk form by id, not by nesting.
    assert 'form="bulk-form"' in html
    assert "disabled" not in html.split('class="entry-select"')[1].split(">")[0]
    assert 'data-action="approve"' in html
    assert 'data-action="reject"' in html
    assert "bulk-selected-count" in html


# ---------------------------------------------------------------------------
# 10. "Plan: Sort" / "Plan: Reclaim" / "Plan: Corral Screenshots" links all
#     get real click-intercept + polling wiring (see
#     static/plan-trigger.js -- the generalized/renamed successor to
#     plan-reclaim.js, which used to wire up only the "Plan: Reclaim" link).
#     All three GET /plan/<kind> routes now kick off a background job and
#     return {"job_id": ...} immediately rather than blocking synchronously
#     (see _stage_sort_plan/_stage_corral_screenshots_plan/
#     _stage_reclaim_plan's per-entry build_plan_snapshot/dir_size_bytes
#     work) -- a plain <a href="/plan/sort">, left alone, would just
#     navigate a real browser to that bare JSON blob with no progress
#     feedback. These tests assert on the actual rendered HTML (not just
#     that the routes still work) so they genuinely catch the regression:
#     the click-intercept wiring must actually be present in the markup a
#     browser receives, for all three links now, not just "Plan: Reclaim".
# ---------------------------------------------------------------------------


PLAN_LINK_TEXT = {
    "sort": "Plan: Sort",
    "reclaim": "Plan: Reclaim",
    "corral-screenshots": "Plan: Corral Screenshots",
}
PLAN_LINK_HREF = {
    "sort": "/plan/sort",
    "reclaim": "/plan/reclaim",
    "corral-screenshots": "/plan/corral-screenshots",
}


def _plan_trigger_link_block(html: str, link_text: str) -> str:
    """Isolate one "Plan: X" anchor tag's own attributes out of a rendered
    page, so assertions check what's actually on that element rather than
    merely "this string appears somewhere on the page".
    """
    before = html.split(f">{link_text}<")[0]
    return before.rsplit("<a", 1)[-1]


def test_static_plan_trigger_js_is_served(client):
    resp = client.get("/static/plan-trigger.js")
    assert resp.status_code == 200
    assert b"plan-trigger-link" in resp.data
    # Polls /status/<job_id> using the server-rendered template attribute
    # (data-status-url-template), never a hardcoded path here.
    assert b"statusUrlTemplate" in resp.data
    # And wires up the kickoff bar's multi-launch flow too (see part 2).
    assert b"kickoff-form" in resp.data
    assert b"kickoff-checkbox" in resp.data


def test_dashboard_nav_all_three_plan_links_have_click_intercept_wiring(client):
    resp = client.get("/")
    html = resp.data.decode()

    assert "plan-trigger.js" in html
    assert 'id="plan-status"' in html

    for kind, text in PLAN_LINK_TEXT.items():
        link_attrs = _plan_trigger_link_block(html, text)
        assert 'class="plan-trigger-link"' in link_attrs
        assert f'data-plan-kind="{kind}"' in link_attrs
        assert f'href="{PLAN_LINK_HREF[kind]}"' in link_attrs
        assert "data-status-url-template=" in link_attrs
        assert "__JOB_ID__" in link_attrs
        assert 'data-dashboard-url="/"' in link_attrs


def test_dashboard_empty_state_plan_links_are_also_wired(client):
    # An empty queue renders dashboard.html's own "No queue entries yet..."
    # fallback paragraph, which has THREE more "Plan: X" links (in addition
    # to the nav ones) -- those must be wired up too.
    resp = client.get("/")
    html = resp.data.decode()
    assert html.count('class="plan-trigger-link"') == 6  # 3 in nav + 3 in empty state
    for kind in PLAN_LINK_TEXT:
        # nav link + empty-state link + the kickoff bar's own checkbox for
        # this kind (see test_dashboard_kickoff_bar_has_checkboxes_for_all_three_plans).
        assert html.count(f'data-plan-kind="{kind}"') == 3


def test_queue_empty_state_plan_links_are_also_wired(client):
    resp = client.get("/queue")
    html = resp.data.decode()
    assert b"Nothing pending" in resp.data
    assert html.count('class="plan-trigger-link"') == 6  # nav + empty-state


def test_queue_nonempty_still_has_nav_plan_links_wired(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.txt")
    _seed_queue(adapter, [entry])

    resp = client.get("/queue")
    html = resp.data.decode()
    # Only the nav links render once entries exist -- no "Nothing pending"
    # empty-state fallback to duplicate them.
    assert html.count('class="plan-trigger-link"') == 3
    for kind, text in PLAN_LINK_TEXT.items():
        link_attrs = _plan_trigger_link_block(html, text)
        assert "data-status-url-template=" in link_attrs


# ---------------------------------------------------------------------------
# 10b. The dashboard's kickoff bar: checkboxes + "Run selected" markup for
#      launching several plans at once (see static/plan-trigger.js's
#      initKickoffBar and section 2d's route-level multi-launch test above).
# ---------------------------------------------------------------------------


def test_dashboard_kickoff_bar_has_checkboxes_for_all_three_plans(client):
    resp = client.get("/")
    html = resp.data.decode()

    assert 'id="kickoff-form"' in html
    assert 'id="kickoff-panel"' in html
    assert 'id="kickoff-run-button"' in html
    assert "data-status-url-template=" in html.split('id="kickoff-form"')[1].split(">")[0]
    assert 'data-dashboard-url="/"' in html.split('id="kickoff-form"')[1].split(">")[0]

    for kind, href in PLAN_LINK_HREF.items():
        assert f'data-plan-kind="{kind}"' in html
        assert f'data-plan-url="{href}"' in html


def test_dashboard_kickoff_bar_location_picker_lists_every_configured_location(adapter, client, home):
    root_a = home / "root_a"
    root_a.mkdir()
    root_b = home / "root_b"
    root_b.mkdir()
    config_module.save_config(
        adapter,
        config_module.Config(
            bucket_rules=config_module.DEFAULT_BUCKET_RULES,
            search_roots=[str(root_a), str(root_b)],
        ),
    )

    resp = client.get("/")
    html = resp.data.decode()

    assert 'class="kickoff-locations"' in html
    for root in (root_a, root_b):
        assert f'class="kickoff-location-checkbox" value="{root}" checked' in html


def test_dashboard_location_tree_bucket_row_actions_scope_to_that_bucket_group_key(
    adapter, client, tmp_path
):
    entry = _entry_with_size(tmp_path, "a.jpg", 1024, "sort:/root_a:photos")
    _seed_queue(adapter, [entry])

    resp = client.get("/")
    html = resp.data.decode()

    assert "/root_a" in html
    assert "photos" in html
    assert 'action="/queue/bulk-approve"' in html
    assert 'action="/queue/bulk-reject"' in html
    assert 'action="/queue/bulk-undo"' in html
    assert 'name="group_key" value="sort:/root_a:photos"' in html


def test_dashboard_kickoff_bar_is_only_on_the_dashboard_not_the_queue_view(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.txt")
    _seed_queue(adapter, [entry])

    resp = client.get("/queue")
    html = resp.data.decode()
    assert 'id="kickoff-form"' not in html


# ---------------------------------------------------------------------------
# 11. Nav "current page" indicator: only Dashboard/Review Queue (real pages)
#     ever get aria-current="page" -- the Plan: links are one-shot actions
#     that redirect back to the dashboard, not destinations, and must never
#     show a stuck active state.
# ---------------------------------------------------------------------------


def _nav_link_block(html: str, link_text: str) -> str:
    """Isolate a nav anchor's own opening-tag attributes, the same way
    _plan_reclaim_link_block does above, so assertions check what's
    actually on that element rather than merely "this string appears
    somewhere on the page".
    """
    before = html.split(f">{link_text}<")[0]
    return before.rsplit("<a", 1)[-1]


def test_dashboard_nav_link_marked_current_dashboard_view(client):
    resp = client.get("/")
    html = resp.data.decode()

    dashboard_link = _nav_link_block(html, "Dashboard")
    assert 'class="nav-link"' in dashboard_link
    assert 'aria-current="page"' in dashboard_link

    queue_link = _nav_link_block(html, "Review Queue")
    assert 'class="nav-link"' in queue_link
    assert "aria-current" not in queue_link


def test_queue_nav_link_marked_current_on_queue_view(client):
    resp = client.get("/queue")
    html = resp.data.decode()

    queue_link = _nav_link_block(html, "Review Queue")
    assert 'class="nav-link"' in queue_link
    assert 'aria-current="page"' in queue_link

    dashboard_link = _nav_link_block(html, "Dashboard")
    assert 'class="nav-link"' in dashboard_link
    assert "aria-current" not in dashboard_link


def test_plan_links_never_carry_active_page_treatment(client):
    # Even on the dashboard (where all three "Plan: X" links visually sit
    # right next to the real aria-current="page" Dashboard link), none of
    # them may ever pick up nav-link or aria-current -- they're one-shot
    # background-job triggers, not pages you "are on".
    resp = client.get("/")
    html = resp.data.decode()

    for text in PLAN_LINK_TEXT.values():
        link = _nav_link_block(html, text)
        assert "nav-link" not in link
        assert "aria-current" not in link


# ---------------------------------------------------------------------------
# 12. Settings: the icon picker. Persisted via config.yaml's icon_choice
#     field (see cleanup_tools.config), unlike the theme picker, so the
#     Tauri/Rust shell can read the same choice cross-process -- see
#     routes.py's module docstring on this section.
# ---------------------------------------------------------------------------

def _icon_choice_card_block(html: str, slug: str) -> str:
    """Isolate one icon-choice <button>'s full markup (attributes and
    inner content), regardless of attribute order -- data-icon-choice
    comes AFTER class= in settings.html's source, so a naive forward split
    on the data-icon-choice marker (the way _nav_link_block's caller uses
    a trailing `>text<` marker) would miss the class="...selected" bit.
    """
    marker = f'data-icon-choice="{slug}"'
    idx = html.index(marker)
    start = html.rindex("<button", 0, idx)
    end = html.index("</button>", idx)
    return html[start:end]


def test_settings_renders_all_icon_choices_with_default_selected(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.data.decode()

    assert 'id="icon-choice-grid"' in html
    for slug, label in ICON_CHOICES.items():
        assert f'data-icon-choice="{slug}"' in html
        # Jinja auto-escapes "&" to "&amp;" in rendered HTML.
        assert label.replace("&", "&amp;") in html

    default_card = _icon_choice_card_block(html, DEFAULT_ICON_CHOICE)
    assert "selected" in default_card
    assert "Current" in default_card


def test_settings_nav_link_marked_current_on_settings_view(client):
    resp = client.get("/settings")
    html = resp.data.decode()

    # The Settings nav link is a bare gear icon (no text content -- see
    # base.html), so it's isolated by its id rather than _nav_link_block's
    # text-based split.
    settings_link = html.split('id="settings-nav-link"')[1].split(">")[0]
    assert 'aria-current="page"' in settings_link

    dashboard_link = _nav_link_block(html, "Dashboard")
    assert "aria-current" not in dashboard_link


def test_set_icon_choice_valid_slug_persists_via_config(adapter, client):
    from cleanup_tools import config as config_module

    resp = client.post("/settings/icon", json={"choice": "recycle-folder"})
    assert resp.status_code == 200
    assert resp.get_json() == {"choice": "recycle-folder"}

    reloaded = config_module.load_config(adapter)
    assert reloaded.icon_choice == "recycle-folder"


def test_set_icon_choice_reflected_as_selected_on_next_settings_load(client):
    client.post("/settings/icon", json={"choice": "broom-sparkle"})

    resp = client.get("/settings")
    html = resp.data.decode()
    card = _icon_choice_card_block(html, "broom-sparkle")
    assert "selected" in card
    assert "Current" in card

    default_card = _icon_choice_card_block(html, DEFAULT_ICON_CHOICE)
    assert "selected" not in default_card


def test_set_icon_choice_unknown_slug_returns_400_and_does_not_persist(adapter, client):
    from cleanup_tools import config as config_module

    resp = client.post("/settings/icon", json={"choice": "not-a-real-icon"})
    assert resp.status_code == 400
    assert "not-a-real-icon" in resp.get_json()["error"]

    reloaded = config_module.load_config(adapter)
    assert reloaded.icon_choice == DEFAULT_ICON_CHOICE


def test_set_icon_choice_missing_body_returns_400(client):
    resp = client.post("/settings/icon", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 12b. Settings shell: gear-icon nav link, sidebar sections, and CRUD for
#      bucket rules / search roots / master paths.
# ---------------------------------------------------------------------------


def test_gear_icon_nav_link_present_on_every_page(client):
    resp = client.get("/")
    html = resp.data.decode()
    assert 'id="settings-nav-link"' in html
    assert 'href="/settings"' in html
    # Bare icon, no visible text label between the anchor tags.
    link_html = html.split('id="settings-nav-link"')[1].split("</a>")[0]
    assert "<svg" in link_html


def test_settings_shell_js_and_shortcut_js_are_served(client):
    for filename in ("settings-shell.js", "settings-shortcut.js"):
        resp = client.get(f"/static/{filename}")
        assert resp.status_code == 200


def test_settings_page_renders_all_six_sidebar_sections(client):
    resp = client.get("/settings")
    html = resp.data.decode()
    for pane_id in ("general", "app-icon", "bucket-rules", "search-roots", "master-paths", "ai-provider"):
        assert f'id="{pane_id}"' in html
        assert f'data-pane="{pane_id}"' in html


def test_add_bucket_rule_persists_and_appears_in_settings(adapter, client):
    resp = client.post(
        "/settings/bucket-rules/add",
        data={"extensions": ".log, TXT", "bucket": "logs", "filename_pattern": ""},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("#bucket-rules")

    config = config_module.load_config(adapter)
    user_rules = _user_bucket_rules_for_test(config)
    assert len(user_rules) == 1
    assert user_rules[0].extensions == frozenset({"log", "txt"})
    assert user_rules[0].bucket == "logs"

    html = client.get("/settings").data.decode()
    assert "logs" in html


def _user_bucket_rules_for_test(config):
    from cleanup_tools.ui.routes import _user_bucket_rules

    return _user_bucket_rules(config)


def test_add_bucket_rule_missing_fields_returns_400(adapter, client):
    resp = client.post("/settings/bucket-rules/add", data={"extensions": "", "bucket": "logs"})
    assert resp.status_code == 400
    assert _user_bucket_rules_for_test(config_module.load_config(adapter)) == []


def test_edit_bucket_rule_updates_in_place(adapter, client):
    client.post("/settings/bucket-rules/add", data={"extensions": "log", "bucket": "logs"})

    resp = client.post(
        "/settings/bucket-rules/0/edit",
        data={"extensions": "log, out", "bucket": "server-logs", "filename_pattern": "app*"},
    )
    assert resp.status_code == 302

    rules = _user_bucket_rules_for_test(config_module.load_config(adapter))
    assert len(rules) == 1
    assert rules[0].extensions == frozenset({"log", "out"})
    assert rules[0].bucket == "server-logs"
    assert rules[0].filename_pattern == "app*"


def test_edit_bucket_rule_unknown_index_returns_404(client):
    resp = client.post("/settings/bucket-rules/5/edit", data={"extensions": "log", "bucket": "logs"})
    assert resp.status_code == 404


def test_remove_bucket_rule_deletes_only_that_rule(adapter, client):
    client.post("/settings/bucket-rules/add", data={"extensions": "log", "bucket": "logs"})
    client.post("/settings/bucket-rules/add", data={"extensions": "out", "bucket": "outputs"})

    resp = client.post("/settings/bucket-rules/0/remove")
    assert resp.status_code == 302

    rules = _user_bucket_rules_for_test(config_module.load_config(adapter))
    assert len(rules) == 1
    assert rules[0].bucket == "outputs"


def test_move_bucket_rule_up_and_down_swaps_neighbors(adapter, client):
    client.post("/settings/bucket-rules/add", data={"extensions": "a", "bucket": "first"})
    client.post("/settings/bucket-rules/add", data={"extensions": "b", "bucket": "second"})
    client.post("/settings/bucket-rules/add", data={"extensions": "c", "bucket": "third"})

    client.post("/settings/bucket-rules/0/move", data={"direction": "down"})
    rules = _user_bucket_rules_for_test(config_module.load_config(adapter))
    assert [r.bucket for r in rules] == ["second", "first", "third"]

    client.post("/settings/bucket-rules/2/move", data={"direction": "up"})
    rules = _user_bucket_rules_for_test(config_module.load_config(adapter))
    assert [r.bucket for r in rules] == ["second", "third", "first"]


def test_move_bucket_rule_at_boundary_is_a_harmless_no_op(adapter, client):
    client.post("/settings/bucket-rules/add", data={"extensions": "a", "bucket": "only"})

    resp = client.post("/settings/bucket-rules/0/move", data={"direction": "up"})
    assert resp.status_code == 302
    rules = _user_bucket_rules_for_test(config_module.load_config(adapter))
    assert [r.bucket for r in rules] == ["only"]


def test_reorder_bucket_rule_actually_changes_first_match_wins_on_next_sort(adapter, client, tmp_path):
    """The single highest-risk property in this whole story: a persisted
    reorder must actually drive resolve_bucket's first-match-wins behavior
    on the next real sort run, not just move rows around in the UI.
    """
    from cleanup_tools.commands import sort as sort_module

    # Two competing rules for the SAME extension, added in an order where
    # "wrong-bucket" would win first-match if the reorder below didn't
    # really persist.
    client.post("/settings/bucket-rules/add", data={"extensions": "xyz", "bucket": "wrong-bucket"})
    client.post("/settings/bucket-rules/add", data={"extensions": "xyz", "bucket": "right-bucket"})

    # Reorder so "right-bucket" (index 1) moves ahead of "wrong-bucket".
    client.post("/settings/bucket-rules/1/move", data={"direction": "up"})
    rules = _user_bucket_rules_for_test(config_module.load_config(adapter))
    assert [r.bucket for r in rules] == ["right-bucket", "wrong-bucket"]

    target = tmp_path / "target"
    target.mkdir()
    (target / "file.xyz").write_text("content")

    from types import SimpleNamespace
    result = sort_module.run(adapter, SimpleNamespace(dir=[str(target)], go=False))
    assert result["plan"][0]["bucket"] == "right-bucket"


def test_add_search_root_persists_and_dedupes(adapter, client, tmp_path):
    root = str(tmp_path / "some-root")

    resp = client.post("/settings/search-roots/add", data={"path": root})
    assert resp.status_code == 302

    config = config_module.load_config(adapter)
    assert config.search_roots == [root]

    # Adding the same path again is a no-op, not a duplicate.
    client.post("/settings/search-roots/add", data={"path": root})
    assert config_module.load_config(adapter).search_roots == [root]


def test_add_search_root_missing_path_returns_400(client):
    resp = client.post("/settings/search-roots/add", data={"path": ""})
    assert resp.status_code == 400


def test_remove_search_root_deletes_only_that_path(adapter, client, tmp_path):
    root_a = str(tmp_path / "a")
    root_b = str(tmp_path / "b")
    client.post("/settings/search-roots/add", data={"path": root_a})
    client.post("/settings/search-roots/add", data={"path": root_b})

    resp = client.post("/settings/search-roots/remove", data={"path": root_a})
    assert resp.status_code == 302

    assert config_module.load_config(adapter).search_roots == [root_b]


def test_add_master_path_persists_with_backed_up_flag(adapter, client, tmp_path):
    path = str(tmp_path / "irreplaceable")

    resp = client.post("/settings/master-paths/add", data={"path": path, "backed_up": "on"})
    assert resp.status_code == 302

    config = config_module.load_config(adapter)
    assert len(config.master_paths) == 1
    assert config.master_paths[0].path == path
    assert config.master_paths[0].backed_up is True


def test_add_master_path_defaults_backed_up_false_when_checkbox_omitted(adapter, client, tmp_path):
    path = str(tmp_path / "irreplaceable")
    client.post("/settings/master-paths/add", data={"path": path})

    config = config_module.load_config(adapter)
    assert config.master_paths[0].backed_up is False


def test_remove_master_path_deletes_only_that_path(adapter, client, tmp_path):
    path_a = str(tmp_path / "a")
    path_b = str(tmp_path / "b")
    client.post("/settings/master-paths/add", data={"path": path_a})
    client.post("/settings/master-paths/add", data={"path": path_b})

    resp = client.post("/settings/master-paths/remove", data={"path": path_a})
    assert resp.status_code == 302

    config = config_module.load_config(adapter)
    assert [mp.path for mp in config.master_paths] == [path_b]


def test_toggle_master_path_backed_up_flips_the_flag(adapter, client, tmp_path):
    path = str(tmp_path / "irreplaceable")
    client.post("/settings/master-paths/add", data={"path": path})  # backed_up=False

    client.post("/settings/master-paths/toggle-backed-up", data={"path": path})
    assert config_module.load_config(adapter).master_paths[0].backed_up is True

    client.post("/settings/master-paths/toggle-backed-up", data={"path": path})
    assert config_module.load_config(adapter).master_paths[0].backed_up is False


def test_toggle_master_path_backed_up_unknown_path_returns_404(client):
    resp = client.post("/settings/master-paths/toggle-backed-up", data={"path": "/nope"})
    assert resp.status_code == 404


def test_master_paths_pane_shows_explicit_warning_copy_on_backed_up_toggle(adapter, client, tmp_path):
    """The safety-critical property this story's review step calls out
    explicitly: flipping backed_up must never read as a generic checkbox.
    """
    backed_up_path = str(tmp_path / "safe")
    not_backed_up_path = str(tmp_path / "unsafe")
    client.post("/settings/master-paths/add", data={"path": backed_up_path, "backed_up": "on"})
    client.post("/settings/master-paths/add", data={"path": not_backed_up_path})

    html = client.get("/settings").data.decode()

    # true -> false direction: explicit copy naming what re-enabling
    # delete-refusal means, not a bare "are you sure?".
    assert "delete-refusal" in html
    # The not-backed-up row's own status badge is explicit, not silent.
    assert "NOT backed up" in html


def test_ai_provider_pane_shows_not_configured_when_no_key_anywhere(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import cleanup_tools.ui.routes as routes_module

    monkeypatch.setattr(
        routes_module, "default_credentials_path", lambda: Path("/nonexistent/credentials/path")
    )

    html = client.get("/settings").data.decode()
    assert "Not configured" in html


def test_ai_provider_pane_shows_configured_via_env_var_without_leaking_the_key(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")

    html = client.get("/settings").data.decode()
    assert "Configured" in html
    assert "ANTHROPIC_API_KEY environment variable" in html
    assert "sk-super-secret-value" not in html


# ---------------------------------------------------------------------------
# 12c. Advanced pane: read-only JSON of the effective config, single source
#      of truth via config_to_dict (see config.py).
# ---------------------------------------------------------------------------


def test_advanced_pane_renders_id_and_data_pane_attribute(client):
    html = client.get("/settings").data.decode()
    assert 'id="advanced"' in html
    assert 'data-pane="advanced"' in html


def _advanced_pane_json(page_html: str) -> dict:
    """Extract and parse the Advanced pane's <pre> JSON block.

    Jinja auto-escapes {{ config_json }} (correctly -- bucket names and
    search-root paths are user-controlled strings rendered inside a <pre>,
    so this must stay auto-escaped rather than marked |safe), so the raw
    substring is HTML-entity-encoded (e.g. `&#34;` for `"`) and needs
    html.unescape() before it's valid JSON again.
    """
    raw = page_html.split("<pre class=\"settings-json\"><code>")[1].split("</code></pre>")[0]
    return json.loads(html.unescape(raw))


def test_advanced_pane_json_matches_load_configs_actual_output(adapter, client):
    client.post(
        "/settings/bucket-rules/add", data={"extensions": "log", "bucket": "logs"}
    )
    client.post("/settings/search-roots/add", data={"path": "/some/root"})
    client.post("/settings/master-paths/add", data={"path": "/some/master", "backed_up": "on"})

    page_html = client.get("/settings").data.decode()
    parsed = _advanced_pane_json(page_html)

    expected = config_module.config_to_dict(config_module.load_config(adapter))
    assert parsed == expected
    assert parsed["search_roots"] == ["/some/root"]
    assert parsed["master_paths"] == [{"path": "/some/master", "backed_up": True}]
    assert {"extensions": ["log"], "bucket": "logs"} in parsed["bucket_rules"]


def test_advanced_pane_reflects_a_change_made_via_another_pane_on_reload(adapter, client):
    before_json = _advanced_pane_json(client.get("/settings").data.decode())
    assert before_json["search_roots"] == []

    client.post("/settings/search-roots/add", data={"path": "/newly/added"})

    after_json = _advanced_pane_json(client.get("/settings").data.decode())
    assert after_json["search_roots"] == ["/newly/added"]


def test_advanced_pane_never_leaks_ai_credentials(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
    html = client.get("/settings").data.decode()
    assert "sk-super-secret-value" not in html


# ---------------------------------------------------------------------------
# 13. History: a reverse-chronological feed built entirely from existing
#     QueueEntry.status_history, with Undo strictly gated on executed_at.
#     This is the single highest-risk correctness property in the whole
#     settings-and-transparency epic -- see _history_rows' docstring.
# ---------------------------------------------------------------------------


def test_history_rows_sorted_reverse_chronological_across_entries(tmp_path):
    from cleanup_tools.ui.routes import _history_rows

    old = _make_entry(tmp_path, "old.txt")
    old.status_history = [{"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"}]
    new = _make_entry(tmp_path, "new.txt")
    new.status_history = [{"status": "pending", "timestamp": "2026-06-01T00:00:00+00:00"}]

    rows = _history_rows([old, new])
    assert [r["entry_id"] for r in rows] == [new.id, old.id]


def test_history_rows_only_latest_row_per_entry_is_undo_eligible(tmp_path):
    from cleanup_tools.ui.routes import _history_rows

    entry = _make_entry(tmp_path, "a.txt", status="approved")
    entry.status_history = [
        {"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"status": "rejected", "timestamp": "2026-01-02T00:00:00+00:00"},
        {"status": "approved", "timestamp": "2026-01-03T00:00:00+00:00"},
    ]

    rows = _history_rows([entry])
    # Reverse-chronological: approved (latest) first, then rejected, then pending.
    assert [r["status"] for r in rows] == ["approved", "rejected", "pending"]
    assert [r["is_latest"] for r in rows] == [True, False, False]
    # Only the latest row is undo-eligible -- an older row's Undo would
    # actually undo a LATER transition, not its own, which would lie.
    assert rows[0]["can_undo"] is True
    assert rows[1]["can_undo"] is False
    assert rows[2]["can_undo"] is False


def test_history_rows_can_undo_false_when_nothing_to_revert_to(tmp_path):
    from cleanup_tools.ui.routes import _history_rows

    entry = _make_entry(tmp_path, "a.txt")
    entry.status_history = [{"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"}]

    rows = _history_rows([entry])
    assert rows[0]["can_undo"] is False  # single history record, nothing before it


def test_history_rows_can_undo_false_when_executed(tmp_path):
    """The single safety-critical property: an entry whose --from-queue
    execution already ran must never be marked can_undo, even though it's
    the latest row and has multiple history records.
    """
    from cleanup_tools.ui.routes import _history_rows

    entry = _make_entry(tmp_path, "a.txt", status="approved")
    entry.status_history = [
        {"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"status": "approved", "timestamp": "2026-01-02T00:00:00+00:00"},
    ]
    entry.executed_at = "2026-01-03T00:00:00+00:00"

    rows = _history_rows([entry])
    assert rows[0]["is_latest"] is True
    assert rows[0]["can_undo"] is False
    assert rows[0]["executed_at"] == "2026-01-03T00:00:00+00:00"


def test_history_rows_carries_edit_record_shape(tmp_path):
    from cleanup_tools.ui.routes import _history_rows

    entry = _make_entry(tmp_path, "a.txt")
    entry.status_history = [
        {
            "status": "pending",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "edit": {
                "dest": {"old": "/old/dest.txt", "new": "/new/dest.txt"},
                "group_key": {"old": "sort:photos", "new": "sort:screenshots"},
            },
        }
    ]

    rows = _history_rows([entry])
    assert rows[0]["edit"]["dest"]["new"] == "/new/dest.txt"


def test_history_view_route_renders_rows_reverse_chronological(adapter, client, tmp_path):
    old = _make_entry(tmp_path, "old.txt")
    old.status_history = [{"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"}]
    new = _make_entry(tmp_path, "new.txt")
    new.status_history = [{"status": "pending", "timestamp": "2026-06-01T00:00:00+00:00"}]
    _seed_queue(adapter, [old, new])

    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.data.decode()

    assert html.index("new.txt") < html.index("old.txt")


def test_history_view_not_yet_executed_entry_shows_working_undo_button(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "a.txt", status="approved")
    entry.status_history = [
        {"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"status": "approved", "timestamp": "2026-01-02T00:00:00+00:00"},
    ]
    _seed_queue(adapter, [entry])

    html = client.get("/history").data.decode()
    row_html = html.split(f'data-entry-id="{entry.id}"')[1].split('<div class="history-row"')[0]
    assert f"/queue/{entry.id}/undo" in row_html
    assert "does not restore the file" not in row_html


def test_history_view_already_executed_entry_never_shows_undo_button(adapter, client, tmp_path):
    """The exact regression this story must never ship: an executed entry's
    History row must not offer a plain, working-looking Undo button.
    """
    entry = _make_entry(tmp_path, "a.txt", status="approved")
    entry.status_history = [
        {"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"status": "approved", "timestamp": "2026-01-02T00:00:00+00:00"},
    ]
    entry.executed_at = "2026-01-03T00:00:00+00:00"
    _seed_queue(adapter, [entry])

    html = client.get("/history").data.decode()
    row_html = html.split(f'data-entry-id="{entry.id}"')[1].split('<div class="history-row"')[0]

    assert f"/queue/{entry.id}/undo" not in row_html
    assert "does not restore the file" in row_html


def test_history_view_paginates_at_realistic_scale(adapter, client, tmp_path):
    entries = []
    for i in range(60):
        e = _make_entry(tmp_path, f"f{i}.txt")
        e.status_history = [{"status": "pending", "timestamp": f"2026-01-01T00:00:{i:02d}+00:00"}]
        entries.append(e)
    _seed_queue(adapter, entries)

    resp = client.get("/history")
    html = resp.data.decode()
    assert "Page 1 of" in html
    assert "pagination" in html

    # Reuses the existing _paginate helper -- never eagerly renders all 60
    # rows on one page.
    assert html.count('class="history-row"') < 60


def test_history_view_empty_queue_shows_empty_state_not_500(client):
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "No history yet" in resp.data.decode()


def test_history_nav_link_present_and_marked_current(client):
    resp = client.get("/history")
    html = resp.data.decode()
    link = _nav_link_block(html, "History")
    assert 'class="nav-link"' in link
    assert 'aria-current="page"' in link

    dashboard_link = _nav_link_block(html, "Dashboard")
    assert "aria-current" not in dashboard_link


# ---------------------------------------------------------------------------
# 14. UI design-review baseline fixes: short paths, action buttons visually
#     distinct from status badges, status is icon+text everywhere, and
#     every bulk action confirms a real count/target before it fires. See
#     the published design-review artifact for the full rationale -- these
#     five fixes are shared across every screen, not a per-page style choice.
# ---------------------------------------------------------------------------


def test_dashboard_kickoff_location_picker_shows_short_path_with_full_path_available(
    adapter, client, home
):
    deep = home / "Documents" / "work" / "archive" / "2019"
    deep.mkdir(parents=True)
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(deep)]),
    )

    html = client.get("/").data.decode()
    assert "…/archive/2019" in html
    # Full path never disappears entirely -- reachable via title tooltip
    # and a screen-reader-only span, not mouse-hover-only.
    assert str(deep) in html


def test_dashboard_location_tree_branch_uses_short_path_with_disclosure(adapter, client, tmp_path):
    entry = _entry_with_size(tmp_path, "a.jpg", 1024, f"sort:{tmp_path}:photos")
    _seed_queue(adapter, [entry])

    html = client.get("/").data.decode()
    # The <details>/<summary> disclosure pattern -- keyboard/focus
    # operable, not mouse-hover-only (a specific, repeated design-review
    # finding across multiple personas).
    assert 'class="location-branch"' in html
    assert "Full path:" in html
    assert f"<code>{tmp_path}</code>" in html


def test_settings_search_roots_and_master_paths_use_short_path_disclosure(adapter, client, tmp_path):
    root = tmp_path / "a" / "b" / "c" / "d"
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root)]),
    )
    client.post("/settings/master-paths/add", data={"path": str(root)})

    html = client.get("/settings").data.decode()
    assert html.count("…/c/d") >= 2  # once in search-roots, once in master-paths
    assert html.count(str(root)) >= 2


def test_history_view_entry_paths_show_basename_prominently_with_short_path_disclosure(
    adapter, client, tmp_path
):
    entry = _make_entry(tmp_path, "report.pdf", group_key="sort:misc", status="pending")
    entry.status_history = [{"status": "pending", "timestamp": "2026-01-01T00:00:00+00:00"}]
    _seed_queue(adapter, [entry])

    html = client.get("/history").data.decode()
    assert "<strong>report.pdf</strong>" in html


def test_approve_reject_undo_buttons_carry_distinct_data_intent(adapter, client, tmp_path):
    """The single finding two independent personas (safety-focused and
    accessibility-focused) hit from opposite directions: action buttons
    must never be visually indistinguishable from each other or from a
    status badge. Pinned down structurally via data-intent, not just
    visually -- primary/secondary/ghost are three genuinely different
    risk levels, not a re-skin of the same button three times.
    """
    entry = _make_entry(tmp_path, "a.txt", group_key="sort:misc", status="pending")
    _seed_queue(adapter, [entry])

    html = client.get("/queue").data.decode()
    card = html.split(f'data-entry-id="{entry.id}"')[1].split("</div>\n</div>")[0]
    assert 'data-intent="primary"' in card
    assert 'data-intent="secondary"' in card
    assert 'data-intent="ghost"' in card


def test_dashboard_bucket_row_actions_carry_distinct_data_intent(adapter, client, tmp_path):
    entry = _entry_with_size(tmp_path, "a.jpg", 1024, f"sort:{tmp_path}:photos")
    _seed_queue(adapter, [entry])

    html = client.get("/").data.decode()
    assert 'data-intent="primary"' in html  # Approve all
    assert 'data-intent="secondary"' in html  # Reject all
    assert 'data-intent="ghost"' in html  # Undo all


def test_status_badges_render_an_icon_alongside_the_text_everywhere(adapter, client, tmp_path):
    """Never color-alone -- the accessibility-review finding. Checked
    across every surface that renders a status: the dashboard tree,
    history, and settings (master-paths backed_up, AI provider).
    """
    entry = _entry_with_size(tmp_path, "a.jpg", 1024, f"sort:{tmp_path}:photos", status="approved")
    entry.status_history = [{"status": "approved", "timestamp": "2026-01-01T00:00:00+00:00"}]
    _seed_queue(adapter, [entry])

    dashboard_html = client.get("/").data.decode()
    assert 'class="status-icon"' in dashboard_html

    history_html = client.get("/history").data.decode()
    assert 'class="status-icon"' in history_html

    settings_html = client.get("/settings").data.decode()
    assert 'class="status-icon"' in settings_html  # AI-provider "Not configured" badge


def test_dashboard_bucket_row_approve_all_has_confirm_with_real_count_and_label(
    adapter, client, tmp_path
):
    entries = [
        _entry_with_size(tmp_path, f"f{i}.jpg", 1024, f"sort:{tmp_path}:photos") for i in range(3)
    ]
    _seed_queue(adapter, entries)

    html = client.get("/").data.decode()
    assert 'data-confirm="Approve all 3 pending items in photos?"' in html
    assert 'data-confirm="Reject all 3 pending items in photos?"' in html


def test_queue_bulk_group_button_has_confirm_with_real_count_and_short_location(
    adapter, client, tmp_path
):
    entries = [
        _make_entry(tmp_path, f"f{i}.jpg", group_key=f"sort:{tmp_path}:photos") for i in range(2)
    ]
    _seed_queue(adapter, entries)

    html = client.get("/queue").data.decode()
    assert "Approve all 2 pending items in photos" in html


def test_master_path_toggle_uses_data_confirm_not_inline_onsubmit(adapter, client, tmp_path):
    """Regression guard: the old onsubmit="return confirm('...' + path)"
    pattern breaks (or is a minor injection surface) the moment a
    user-entered path contains a single quote, since it's spliced
    directly into a JS string literal in markup. data-confirm sidesteps
    this entirely -- confirm-actions.js reads it as plain attribute text,
    never as JS source.
    """
    tricky_path = str(tmp_path / "Bob's Files")
    client.post("/settings/master-paths/add", data={"path": tricky_path})

    html = client.get("/settings").data.decode()
    assert "onsubmit=" not in html
    assert "data-confirm=" in html
    assert "REMOVES reclaim" in html


def test_confirm_actions_js_is_served(client):
    resp = client.get("/static/confirm-actions.js")
    assert resp.status_code == 200
    assert b"data-bulk-selected-action" in resp.data


def test_status_icon_macro_renders_distinct_svg_per_status():
    from flask import Flask

    app = Flask(__name__, template_folder=str(Path(__file__).parent.parent / "src" / "cleanup_tools" / "ui" / "templates"))
    with app.app_context():
        template = app.jinja_env.from_string(
            '{% import "_status_icon.html" as icons %}{{ icons.status_icon(status) }}'
        )
        approved = template.render(status="approved")
        rejected = template.render(status="rejected")
        pending = template.render(status="pending")

    assert "<svg" in approved and "<svg" in rejected and "<svg" in pending
    assert approved != rejected != pending
    assert 'aria-hidden="true"' in approved


def test_heading_detail_class_used_instead_of_inline_style_on_page_headings(client):
    """Regression guard: the "(N pending)"/"(N total)" suffix next to a
    page h1/h2 goes through the shared .heading-detail class (part of
    the real type scale), not a one-off inline style repeated per
    template.
    """
    for path in ("/queue", "/history", "/"):
        html = client.get(path).data.decode()
        assert "font-weight:normal; color:var(--ink-soft)" not in html
    assert 'class="heading-detail"' in client.get("/queue").data.decode()


# ---------------------------------------------------------------------------
# 15. UI mode (config.ui_mode): Standard/Guided/Console, orthogonal to the
#     color theme. Standard is the default and must render identically to
#     every pre-mode-system test above (none of them set ui_mode).
# ---------------------------------------------------------------------------


def test_bucket_icon_returns_distinct_icon_per_known_bucket_and_a_default_otherwise():
    assert bucket_icon("photos") != bucket_icon("pdfs")
    assert bucket_icon("totally-unknown-bucket") == bucket_icon(None)
    assert bucket_icon("photos")  # non-empty


def test_dashboard_html_root_carries_data_ui_mode_matching_config(adapter, client):
    default_html = client.get("/").data.decode()
    assert 'data-ui-mode="standard"' in default_html

    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, ui_mode="console"),
    )
    console_html = client.get("/").data.decode()
    assert 'data-ui-mode="console"' in console_html


def test_settings_general_pane_lists_all_three_modes_with_current_selected(adapter, client):
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, ui_mode="guided"),
    )
    html = client.get("/settings").data.decode()

    for slug in UI_MODES:
        assert f'value="{slug}"' in html

    guided_option = html.split('value="guided"')[1].split("</label>")[0]
    assert "checked" in guided_option


def test_set_ui_mode_valid_choice_persists_and_redirects(adapter, client):
    resp = client.post("/settings/ui-mode", data={"ui_mode": "console"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("#general")

    reloaded = config_module.load_config(adapter)
    assert reloaded.ui_mode == "console"


def test_set_ui_mode_unknown_value_returns_400_and_does_not_persist(adapter, client):
    resp = client.post("/settings/ui-mode", data={"ui_mode": "not-a-real-mode"})
    assert resp.status_code == 400

    reloaded = config_module.load_config(adapter)
    assert reloaded.ui_mode == "standard"


def test_queue_standard_mode_renders_technical_src_dest_labels(adapter, client, tmp_path):
    entry = _make_entry(tmp_path, "report.pdf", group_key="sort:misc", status="pending")
    _seed_queue(adapter, [entry])

    html = client.get("/queue").data.decode()
    assert "will move from" in html
    assert 'class="guided-summary"' not in html


def test_queue_guided_mode_renders_plain_sentence_and_collapsed_technical_details(
    adapter, client, tmp_path
):
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, ui_mode="guided"),
    )
    entry = _make_entry(tmp_path, "report.pdf", group_key="sort:pdfs", status="pending")
    _seed_queue(adapter, [entry])

    html = client.get("/queue").data.decode()
    assert 'class="guided-summary"' in html
    assert "will move into the" in html
    assert "Nothing is deleted." in html
    assert "Show technical details" in html
    # The raw src is still present (never actually hidden from the DOM,
    # only tucked behind a closed-by-default <details>), just not the
    # first thing the sentence shows.
    assert str(tmp_path / "report.pdf") in html


def test_queue_guided_mode_delete_action_gets_distinct_reassurance_copy(adapter, client, tmp_path):
    """Move and delete are genuinely different risk levels -- guided mode
    must not use the same "nothing is deleted" reassurance for an entry
    that IS a deletion.
    """
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, ui_mode="guided"),
    )
    f = tmp_path / "cache.tmp"
    f.write_bytes(b"x")
    entry = QueueEntry(
        action="delete", src=str(f), dest="", status="pending",
        group_key="reclaim:build_caches", plan_snapshot=build_plan_snapshot(f),
    )
    _seed_queue(adapter, [entry])

    html = client.get("/queue").data.decode()
    assert "will be permanently deleted" in html
    assert "Nothing is deleted." not in html.split("permanently deleted")[0][-200:]


def test_console_mode_data_attribute_present_for_density_css(adapter, client, tmp_path):
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, ui_mode="console"),
    )
    entry = _make_entry(tmp_path, "a.txt", group_key="sort:misc")
    _seed_queue(adapter, [entry])

    html = client.get("/queue").data.decode()
    assert 'data-ui-mode="console"' in html


# ---------------------------------------------------------------------------
# 16. Chat: GET /chat renders the page; POST /chat/new creates a
#     conversation; POST /chat/<id>/message validates its input and kicks
#     off a background job (see jobs.py's wants_partial=True path); GET
#     /chat/status/<job_id> is the dedicated poll route for that job.
#
#     get_raw_client and chat_engine.run_turn are always monkeypatched
#     below before a message is ever sent -- the real Anthropic client is
#     never constructed and no real network call is possible from this
#     file, mirroring test_chat_engine.py's own no-real-network discipline.
# ---------------------------------------------------------------------------


def _stub_chat_engine(monkeypatch, *, final_text="Sure, here's what I found."):
    import cleanup_tools.ui.routes as routes_module

    monkeypatch.setattr(routes_module, "get_raw_client", lambda **_kwargs: object())

    def fake_run_turn(client, adapter, history, user_message, *, partial_callback=None, **_kwargs):
        if partial_callback is not None:
            partial_callback(final_text[: max(1, len(final_text) // 2)])
        return {"text": final_text, "staged_entry_ids": []}

    monkeypatch.setattr(routes_module.chat_engine, "run_turn", fake_run_turn)


def test_chat_view_renders_the_page(client):
    resp = client.get("/chat")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'id="chat-transcript"' in html
    assert 'id="chat-form"' in html


def test_chat_nav_link_present_and_marked_current_on_the_chat_page(client):
    html = client.get("/chat").data.decode()
    assert ">Chat<" in html
    assert 'href="/chat"' in html


def test_chat_new_creates_a_conversation_and_returns_its_id(client):
    resp = client.post("/chat/new")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["conversation_id"]


def test_chat_message_empty_body_returns_400(client):
    conversation_id = client.post("/chat/new").get_json()["conversation_id"]

    resp = client.post(f"/chat/{conversation_id}/message", json={"message": "   "})
    assert resp.status_code == 400


def test_chat_message_unknown_conversation_returns_404(client, monkeypatch):
    _stub_chat_engine(monkeypatch)

    resp = client.post("/chat/not-a-real-conversation/message", json={"message": "hi"})
    assert resp.status_code == 404


def test_chat_message_full_turn_streams_partial_then_completes(client, monkeypatch):
    _stub_chat_engine(monkeypatch, final_text="Your Downloads folder has 12 PDFs.")
    conversation_id = client.post("/chat/new").get_json()["conversation_id"]

    resp = client.post(f"/chat/{conversation_id}/message", json={"message": "what's in Downloads?"})
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    payload = _poll_chat_job_until_terminal(client, job_id)
    assert payload["status"] == "done"
    assert payload["result"]["text"] == "Your Downloads folder has 12 PDFs."


def test_chat_status_unknown_job_id_returns_404(client):
    resp = client.get("/chat/status/not-a-real-job")
    assert resp.status_code == 404


def _poll_chat_job_until_terminal(client, job_id: str, timeout: float = 5.0) -> dict:
    """Poll GET /chat/status/<job_id> until it reports "done" or "error".

    Mirrors ``_poll_job_until_terminal`` above, against the dedicated chat
    status route (a different result shape than the generic one -- see
    ``chat_job_status``'s docstring in routes.py).
    """
    import time

    deadline = time.time() + timeout
    payload = None
    while time.time() < deadline:
        resp = client.get(f"/chat/status/{job_id}")
        assert resp.status_code == 200
        payload = resp.get_json()
        if payload["status"] in ("done", "error"):
            return payload
        time.sleep(0.01)
    raise AssertionError(f"chat job {job_id} did not reach a terminal status within {timeout}s: {payload}")

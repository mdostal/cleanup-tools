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
from pathlib import Path

import pytest
from PIL import Image

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools import queue as queue_module
from cleanup_tools.queue import QueueEntry, build_plan_snapshot
from cleanup_tools.ui.app import create_app


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


# ---------------------------------------------------------------------------
# 1. Dashboard: groups by group_key, sizes/counts, and per-status counts.
# ---------------------------------------------------------------------------


def test_dashboard_empty_queue_shows_zero_entries(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Queue is empty" in resp.data


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
    assert "sort:photos" in html
    assert "2 items" in html
    assert "3.0 MB" in html

    # Group "reclaim:os_junk": 1 item, 512 KiB exactly -> "0.5 MB".
    assert "reclaim:os_junk" in html
    assert "1 item" in html
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
    assert resp1.status_code == 302

    entries_after_first = _reload_queue(adapter)
    assert len(entries_after_first) == 2
    assert {e.status for e in entries_after_first} == {"pending"}
    assert {e.action for e in entries_after_first} == {"move"}

    # Hitting it again must not create duplicate pending entries for files
    # already staged -- this is stage_entries()'s dedup, exercised through
    # the route.
    resp2 = client.get("/plan/sort")
    assert resp2.status_code == 302

    entries_after_second = _reload_queue(adapter)
    assert len(entries_after_second) == 2
    assert {e.id for e in entries_after_second} == {e.id for e in entries_after_first}


def test_plan_sort_missing_downloads_dir_does_not_crash(adapter, client, home):
    # No Downloads dir created under the fake home -- sort.run() raises
    # FileNotFoundError; the route must turn that into a redirect/flash,
    # not a 500.
    assert not (home / "Downloads").exists()
    resp = client.get("/plan/sort")
    assert resp.status_code == 302
    assert "plan_error" in resp.headers["Location"]


def test_plan_reclaim_stages_entries_and_is_idempotent(adapter, client, home):
    documents = home / "Documents"
    documents.mkdir()
    (documents / ".DS_Store").write_bytes(b"junk")

    resp1 = client.get("/plan/reclaim")
    assert resp1.status_code == 302

    entries_after_first = _reload_queue(adapter)
    junk_entries = [e for e in entries_after_first if e.action == "delete"]
    assert len(junk_entries) >= 1
    ds_store_entries = [e for e in junk_entries if e.src.endswith(".DS_Store")]
    assert len(ds_store_entries) == 1

    resp2 = client.get("/plan/reclaim")
    assert resp2.status_code == 302

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

    client.get("/plan/reclaim")

    entries = _reload_queue(adapter)
    assert not any(e.src == str(junk) for e in entries)


def test_plan_corral_screenshots_stages_move_entries_and_is_idempotent(adapter, client, home):
    desktop = home / "Desktop"
    desktop.mkdir()
    shot = desktop / "screenshot-2024.png"
    shot.write_bytes(b"shot-bytes")

    resp1 = client.get("/plan/corral-screenshots")
    assert resp1.status_code == 302

    entries_after_first = _reload_queue(adapter)
    assert len(entries_after_first) == 1
    entry = entries_after_first[0]
    assert entry.action == "move"
    assert entry.status == "pending"
    assert entry.source == "ui-plan-corral-screenshots"
    assert entry.group_key == "corral-screenshots"
    assert entry.src == str(shot)
    assert entry.dest == str(home / "Pictures" / "Screenshots" / "screenshot-2024.png")

    # Hitting it again must not create a duplicate pending entry for the
    # same src -- stage_entries()'s existing dedup, exercised through the
    # route, mirroring plan_sort/plan_reclaim's own idempotency tests.
    resp2 = client.get("/plan/corral-screenshots")
    assert resp2.status_code == 302

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
    # than raising, and the route must still redirect cleanly.
    resp = client.get("/plan/corral-screenshots")
    assert resp.status_code == 302
    assert _reload_queue(adapter) == []


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

    client.get("/plan/sort")

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

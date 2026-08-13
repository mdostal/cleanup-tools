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

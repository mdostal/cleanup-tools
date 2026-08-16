"""Tests for cleanup_tools.chat.tools."""

from __future__ import annotations

import json

import pytest

from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.chat import state as chat_state
from cleanup_tools.chat import tools
from cleanup_tools.commands.sort import SORTED_SUBDIR
from cleanup_tools.queue import QueueEntry, build_plan_snapshot


@pytest.fixture
def adapter(tmp_path) -> MacOSAdapter:
    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    return FakeHomeAdapter()


def test_list_locations_returns_configured_search_roots(adapter, tmp_path):
    root = tmp_path / "my-photos"
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root)]),
    )

    result = tools.list_locations(adapter)

    assert result == {"locations": [str(root.resolve())]}


def test_list_locations_falls_back_to_standard_trio_when_unconfigured(adapter):
    result = tools.list_locations(adapter)

    assert result["locations"] == [
        str(adapter.resolve_standard_dir("downloads")),
        str(adapter.resolve_standard_dir("desktop")),
        str(adapter.resolve_standard_dir("documents")),
    ]


def test_list_locations_matches_config_configured_locations_exactly(adapter, tmp_path):
    """Regression guard: the agent's answer must never be able to disagree
    with the dashboard's own kickoff-bar location picker -- both go
    through the exact same config.configured_locations function.
    """
    config = config_module.load_config(adapter)
    expected = config_module.configured_locations(config, adapter)

    assert tools.list_locations(adapter)["locations"] == expected


def test_tool_schemas_and_functions_are_kept_in_sync():
    schema_names = {schema["name"] for schema in tools.TOOL_SCHEMAS}
    function_names = set(tools.TOOL_FUNCTIONS.keys())
    assert schema_names == function_names


def test_list_locations_schema_has_no_required_input():
    schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "list_locations")
    assert schema["input_schema"]["properties"] == {}


# ---------------------------------------------------------------------------
# list_queue_summary: must mirror queue.group_entries_hierarchical exactly
# (the same shape the dashboard tree renders) -- aggregates only.
# ---------------------------------------------------------------------------


def test_list_queue_summary_matches_group_entries_hierarchical_exactly(adapter, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"x")
    entry = QueueEntry(
        action="move", src=str(f), dest=str(tmp_path / "_sorted" / "pdfs" / "report.pdf"),
        status="pending", group_key=f"sort:{tmp_path}:pdfs", plan_snapshot=build_plan_snapshot(f),
    )
    path = queue_module.default_queue_path(adapter)
    queue_module.save_queue(adapter, [entry], path)

    result = tools.list_queue_summary(adapter)

    entries = queue_module.load_queue(adapter, path)
    assert result == {"locations": queue_module.group_entries_hierarchical(entries)}


def test_list_queue_summary_empty_queue_returns_empty_locations(adapter):
    assert tools.list_queue_summary(adapter) == {"locations": []}


def test_list_queue_summary_never_returns_a_per_entry_list(adapter, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"x")
    entry = QueueEntry(
        action="move", src=str(f), dest="", status="pending",
        group_key=f"sort:{tmp_path}:pdfs", plan_snapshot=build_plan_snapshot(f),
    )
    queue_module.save_queue(adapter, [entry], queue_module.default_queue_path(adapter))

    result = tools.list_queue_summary(adapter)

    # QueueEntry.id (and .src, the real filesystem path) never leak into the
    # aggregate -- only aggregate keys (location/count/total_size/buckets/...).
    assert entry.id not in json.dumps(result)
    assert str(f) not in json.dumps(result)


# ---------------------------------------------------------------------------
# scan_location / list_candidate_files: real, read-only dry-run bucket
# counts and filenames for one location -- never write anything.
# ---------------------------------------------------------------------------


def _make_loose_files(tmp_path, names: list[str]) -> None:
    for name in names:
        (tmp_path / name).write_bytes(b"x")


def test_scan_location_returns_real_bucket_counts(adapter, tmp_path):
    _make_loose_files(tmp_path, ["a.pdf", "b.pdf", "c.jpg"])

    result = tools.scan_location(adapter, str(tmp_path))

    counts = {b["bucket"]: b["count"] for b in result["buckets"]}
    assert counts["pdfs"] == 2
    assert counts["photos"] == 1


def test_scan_location_can_be_scoped_to_a_subset_of_buckets(adapter, tmp_path):
    _make_loose_files(tmp_path, ["a.pdf", "b.jpg"])

    result = tools.scan_location(adapter, str(tmp_path), buckets=["pdfs"])

    assert {b["bucket"] for b in result["buckets"]} == {"pdfs"}


def test_scan_location_never_writes_to_disk(adapter, tmp_path):
    _make_loose_files(tmp_path, ["a.pdf"])

    tools.scan_location(adapter, str(tmp_path))

    assert (tmp_path / "a.pdf").exists()
    assert not (tmp_path / "_sorted").exists()


def test_list_candidate_files_returns_matching_filenames(adapter, tmp_path):
    _make_loose_files(tmp_path, ["a.pdf", "b.pdf", "c.jpg"])

    result = tools.list_candidate_files(adapter, str(tmp_path), "pdfs")

    assert result["files"] == ["a.pdf", "b.pdf"]
    assert result["total_count"] == 2
    assert result["truncated"] is False


def test_list_candidate_files_caps_at_fifty_and_signals_truncation(adapter, tmp_path):
    _make_loose_files(tmp_path, [f"file-{i}.pdf" for i in range(60)])

    result = tools.list_candidate_files(adapter, str(tmp_path), "pdfs")

    assert len(result["files"]) == 50
    assert result["total_count"] == 60
    assert result["truncated"] is True


def test_list_candidate_files_never_writes_to_disk(adapter, tmp_path):
    _make_loose_files(tmp_path, ["a.pdf"])

    tools.list_candidate_files(adapter, str(tmp_path), "pdfs")

    assert (tmp_path / "a.pdf").exists()
    assert not (tmp_path / "_sorted").exists()


def test_list_candidate_files_cap_cannot_be_overridden_via_unexpected_arguments(adapter, tmp_path):
    """The 50-item cap is a hardcoded module constant, not a caller-supplied
    parameter -- no schema property lets the model raise it, and any
    unexpected argument the model supplies anyway is silently absorbed by
    **_kwargs rather than accepted as a real cap override. This is the
    property the "flat context cost regardless of queue size" design claim
    depends on (see the story's own risk list).
    """
    _make_loose_files(tmp_path, [f"file-{i}.pdf" for i in range(60)])

    result = tools.list_candidate_files(
        adapter, str(tmp_path), "pdfs", cap=1000, limit=1000, max_results=1000
    )

    assert len(result["files"]) == 50


def test_scan_location_and_list_candidate_files_schemas_require_location():
    scan_schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "scan_location")
    assert scan_schema["input_schema"]["required"] == ["location"]

    files_schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "list_candidate_files")
    assert set(files_schema["input_schema"]["required"]) == {"location", "bucket"}


# ---------------------------------------------------------------------------
# propose_moves: the ONE write tool -- stages ordinary pending QueueEntry
# "move" proposals, never touches a real file. Every candidate must pass
# BOTH the protected-path guard and the bucket-name-shape guard before
# queue.stage_entries is ever called for it; a failing candidate is refused
# (never staged) and reported back with a reason, never silently dropped.
# ---------------------------------------------------------------------------


def _configure_search_root(adapter, root) -> None:
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root)]),
    )


def test_propose_moves_stages_entry_with_dest_and_group_key_matching_manual_shape(adapter, tmp_path):
    root = tmp_path / "my-downloads"
    root.mkdir()
    f = root / "report.pdf"
    f.write_bytes(b"x")
    _configure_search_root(adapter, root)

    result = tools.propose_moves(adapter, moves=[{"src": str(f), "dest_bucket": "pdfs"}])

    assert result["refused"] == []
    assert len(result["staged_entry_ids"]) == 1

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == result["staged_entry_ids"][0]
    assert entry.action == "move"
    assert entry.src == str(f.resolve())
    assert entry.dest == str(root.resolve() / SORTED_SUBDIR / "pdfs" / "report.pdf")
    assert entry.group_key == f"sort:{root.resolve()}:pdfs"
    assert entry.source == "ai:chat"
    assert entry.status == "pending"


def test_propose_moves_refuses_protected_path_and_stages_nothing(adapter, tmp_path, monkeypatch):
    fake_system_root = tmp_path / "FakeSystem"
    fake_system_root.mkdir()
    protected_file = fake_system_root / "important.plist"
    protected_file.write_bytes(b"do-not-touch")
    monkeypatch.setattr(queue_module, "PROTECTED_PATH_ROOTS", [fake_system_root])

    result = tools.propose_moves(adapter, moves=[{"src": str(protected_file), "dest_bucket": "pdfs"}])

    assert result["staged_entry_ids"] == []
    assert result["refused"] == [
        {"src": str(protected_file), "dest_bucket": "pdfs", "reason": "protected_path"}
    ]
    assert queue_module.load_queue(adapter, queue_module.default_queue_path(adapter)) == []


@pytest.mark.parametrize(
    "malicious_bucket", ["../../etc", "/etc/passwd", "a/b", "..", "a.b", ""]
)
def test_propose_moves_refuses_malformed_dest_bucket_before_any_path_is_built(
    adapter, tmp_path, malicious_bucket
):
    root = tmp_path / "my-downloads"
    root.mkdir()
    f = root / "report.pdf"
    f.write_bytes(b"x")
    _configure_search_root(adapter, root)

    result = tools.propose_moves(adapter, moves=[{"src": str(f), "dest_bucket": malicious_bucket}])

    assert result["staged_entry_ids"] == []
    assert len(result["refused"]) == 1
    assert result["refused"][0]["reason"] in ("invalid_bucket_name", "missing_or_invalid_input")
    assert queue_module.load_queue(adapter, queue_module.default_queue_path(adapter)) == []
    # Never wrote anything under a bucket dir named after the malicious input.
    assert not (root / SORTED_SUBDIR).exists()


def test_propose_moves_refuses_a_src_that_does_not_exist(adapter, tmp_path):
    root = tmp_path / "my-downloads"
    root.mkdir()
    _configure_search_root(adapter, root)
    missing = root / "ghost.pdf"

    result = tools.propose_moves(adapter, moves=[{"src": str(missing), "dest_bucket": "pdfs"}])

    assert result["staged_entry_ids"] == []
    assert result["refused"] == [{"src": str(missing), "dest_bucket": "pdfs", "reason": "does_not_exist"}]


def test_propose_moves_partial_batch_stages_valid_and_refuses_invalid_independently(
    adapter, tmp_path, monkeypatch
):
    root = tmp_path / "my-downloads"
    root.mkdir()
    valid_file = root / "report.pdf"
    valid_file.write_bytes(b"x")
    _configure_search_root(adapter, root)

    fake_system_root = tmp_path / "FakeSystem"
    fake_system_root.mkdir()
    protected_file = fake_system_root / "important.plist"
    protected_file.write_bytes(b"do-not-touch")
    monkeypatch.setattr(queue_module, "PROTECTED_PATH_ROOTS", [fake_system_root])

    result = tools.propose_moves(
        adapter,
        moves=[
            {"src": str(valid_file), "dest_bucket": "pdfs"},
            {"src": str(protected_file), "dest_bucket": "pdfs"},
            {"src": str(root / "ghost.jpg"), "dest_bucket": "../escape"},
        ],
    )

    assert len(result["staged_entry_ids"]) == 1
    reasons = {r["reason"] for r in result["refused"]}
    assert reasons == {"protected_path", "invalid_bucket_name"}

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 1
    assert entries[0].src == str(valid_file.resolve())


def test_propose_moves_never_stages_when_every_candidate_is_refused(adapter, tmp_path, monkeypatch):
    """Mirrors the review step's core regression check: no code path can
    reach queue.stage_entries() with an entry that failed either guard.
    """
    fake_system_root = tmp_path / "FakeSystem"
    fake_system_root.mkdir()
    protected_file = fake_system_root / "important.plist"
    protected_file.write_bytes(b"do-not-touch")
    monkeypatch.setattr(queue_module, "PROTECTED_PATH_ROOTS", [fake_system_root])

    result = tools.propose_moves(
        adapter,
        moves=[
            {"src": str(protected_file), "dest_bucket": "pdfs"},
            {"src": str(protected_file), "dest_bucket": "../escape"},
        ],
    )

    assert result["staged_entry_ids"] == []
    path = queue_module.default_queue_path(adapter)
    assert not path.exists() or queue_module.load_queue(adapter, path) == []


def test_propose_moves_schema_requires_moves_with_src_and_dest_bucket():
    schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "propose_moves")
    assert schema["input_schema"]["required"] == ["moves"]
    item_schema = schema["input_schema"]["properties"]["moves"]["items"]
    assert set(item_schema["required"]) == {"src", "dest_bucket"}


# ---------------------------------------------------------------------------
# propose_moves' per-conversation file cap (_CONVERSATION_FILE_CAP): a
# pre-call gate on the TOTAL files a conversation may stage across every
# propose_moves call in its lifetime -- never call-then-discard.
# ---------------------------------------------------------------------------


def test_propose_moves_stages_only_the_remaining_budget_when_near_the_cap(
    adapter, tmp_path, monkeypatch
):
    monkeypatch.setattr(tools, "_CONVERSATION_FILE_CAP", 3)
    root = tmp_path / "my-downloads"
    root.mkdir()
    files = []
    for i in range(3):
        f = root / f"file-{i}.pdf"
        f.write_bytes(b"x")
        files.append(f)
    _configure_search_root(adapter, root)

    conv_id = chat_state.create_conversation()
    chat_state.record_staged_files(conv_id, 2)  # 2 of 3 already used

    result = tools.propose_moves(
        adapter,
        moves=[{"src": str(f), "dest_bucket": "pdfs"} for f in files],
        conversation_id=conv_id,
    )

    assert len(result["staged_entry_ids"]) == 1
    cap_refusals = [r for r in result["refused"] if r["reason"] == "conversation_file_cap_reached"]
    assert len(cap_refusals) == 2

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 1


def test_propose_moves_never_stages_zero_when_budget_remains_and_over_when_it_doesnt(
    adapter, tmp_path, monkeypatch
):
    """Not zero, not over -- exactly the remaining budget (acceptance
    criterion for this story's file cap).
    """
    monkeypatch.setattr(tools, "_CONVERSATION_FILE_CAP", 5)
    root = tmp_path / "my-downloads"
    root.mkdir()
    files = []
    for i in range(4):
        f = root / f"file-{i}.pdf"
        f.write_bytes(b"x")
        files.append(f)
    _configure_search_root(adapter, root)

    conv_id = chat_state.create_conversation()
    chat_state.record_staged_files(conv_id, 1)  # 4 of 5 remain

    result = tools.propose_moves(
        adapter,
        moves=[{"src": str(f), "dest_bucket": "pdfs"} for f in files],
        conversation_id=conv_id,
    )

    assert len(result["staged_entry_ids"]) == 4  # exactly the remaining budget, not fewer
    assert result["refused"] == []


def test_propose_moves_updates_the_conversations_running_staged_file_count(adapter, tmp_path):
    root = tmp_path / "my-downloads"
    root.mkdir()
    f = root / "report.pdf"
    f.write_bytes(b"x")
    _configure_search_root(adapter, root)

    conv_id = chat_state.create_conversation()
    assert chat_state.get_conversation(conv_id).staged_file_count == 0

    tools.propose_moves(adapter, moves=[{"src": str(f), "dest_bucket": "pdfs"}], conversation_id=conv_id)

    assert chat_state.get_conversation(conv_id).staged_file_count == 1


def test_propose_moves_cap_is_never_exceeded_across_multiple_calls(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_CONVERSATION_FILE_CAP", 3)
    root = tmp_path / "my-downloads"
    root.mkdir()
    _configure_search_root(adapter, root)

    conv_id = chat_state.create_conversation()
    total_staged = 0

    for batch in range(3):  # 3 calls x 2 files each = 6 candidates against a cap of 3
        batch_files = []
        for i in range(2):
            f = root / f"batch-{batch}-file-{i}.pdf"
            f.write_bytes(b"x")
            batch_files.append(f)
        result = tools.propose_moves(
            adapter,
            moves=[{"src": str(bf), "dest_bucket": "pdfs"} for bf in batch_files],
            conversation_id=conv_id,
        )
        total_staged += len(result["staged_entry_ids"])

    assert total_staged == 3
    assert chat_state.get_conversation(conv_id).staged_file_count == 3


def test_propose_moves_with_unknown_conversation_id_falls_back_to_a_fresh_budget(adapter, tmp_path):
    root = tmp_path / "my-downloads"
    root.mkdir()
    f = root / "report.pdf"
    f.write_bytes(b"x")
    _configure_search_root(adapter, root)

    result = tools.propose_moves(
        adapter, moves=[{"src": str(f), "dest_bucket": "pdfs"}], conversation_id="does-not-exist"
    )

    assert len(result["staged_entry_ids"]) == 1

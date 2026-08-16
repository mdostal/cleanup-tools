"""Tests for cleanup_tools.chat.tools."""

from __future__ import annotations

import json

import pytest

from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.chat import tools
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

"""Tests for cleanup_tools.chat.tools."""

from __future__ import annotations

import pytest

from cleanup_tools import config as config_module
from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.chat import tools


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

"""Tests for cleanup_tools.ai.wiring: bridging the AI-provider interface
into the approval queue.

Zero real network/API calls anywhere in this file -- every test uses a
locally-defined ``_FakeProvider`` (a plain :class:`AIProvider` subclass with
queued in-memory responses), never a real ``AnthropicProvider`` making an
actual HTTP request. The centerpiece assertion throughout is CALL COUNT
(``provider.calls``), not result count: a wiring bug that calls the provider
for every 'other'-bucketed candidate and only truncates/filters the results
afterward would still pass a result-count-only assertion but must fail the
call-count ones here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.ai.base import AIProvider, ProposalResult
from cleanup_tools.ai.wiring import DEFAULT_CAP, propose_for_other_bucket
from cleanup_tools.ui.app import create_app


# ---------------------------------------------------------------------------
# Fake provider: queues one ProposalResult per filename (or a single
# constant result for every call), and records every (filename, metadata)
# pair it was invoked with -- so tests can assert exact call counts, not
# just how many results came back.
# ---------------------------------------------------------------------------


class _FakeProvider(AIProvider):
    def __init__(self, responses=None, constant: ProposalResult | None = None):
        """Either ``responses`` (a dict of filename -> ProposalResult, or a
        list consumed in call order) or a single ``constant`` result
        returned for every call. Exactly one of the two must be given.
        """
        assert (responses is None) != (constant is None), "give exactly one of responses/constant"
        self._responses = responses
        self._constant = constant
        self.calls: list[tuple[str, dict]] = []

    def propose_bucket(self, filename: str, metadata: dict) -> ProposalResult:
        self.calls.append((filename, metadata))
        if self._constant is not None:
            return self._constant
        if isinstance(self._responses, dict):
            return self._responses[filename]
        # list form: consumed in call order, raises loudly (IndexError) if
        # called more times than queued -- an unmistakable test failure.
        return self._responses.pop(0)


# A fake double named exactly like the real provider class, purely so
# _provider_source()'s class-name-derived "ai:<name>" tagging produces the
# same "ai:anthropic" string a real AnthropicProvider would, without this
# test file importing (or making network calls through) the real one.
class AnthropicProvider(_FakeProvider):
    pass


# ---------------------------------------------------------------------------
# Fixtures: fake-home adapter (mirrors test_ui_routes.py / test_sort.py),
# an empty-bucket-rules Config so every non-dotfile in Downloads lands in
# 'other' regardless of extension, and a helper to populate N loose files.
# ---------------------------------------------------------------------------


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
def downloads(home: Path) -> Path:
    d = home / "Downloads"
    d.mkdir()
    return d


@pytest.fixture
def other_config() -> config_module.Config:
    # No bucket rules at all -> resolve_bucket() falls through to "other"
    # for every file, regardless of extension.
    return config_module.Config(bucket_rules=[])


def _make_files(downloads: Path, n: int, prefix: str = "mystery") -> list[Path]:
    paths = []
    for i in range(n):
        p = downloads / f"{prefix}{i}.dat"
        p.write_bytes(f"content-{i}".encode())
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# 1. Cap enforcement by CALL COUNT -- the centerpiece test.
# ---------------------------------------------------------------------------


def test_cap_is_enforced_by_call_count_not_result_count(adapter, downloads, other_config):
    _make_files(downloads, 7)
    cap = 3
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
    )

    result = propose_for_other_bucket(adapter, other_config, provider, cap)

    # The provider must have been invoked EXACTLY `cap` times -- not 7 times
    # with only 3 results kept. This is the property a "filter results after
    # calling everyone" bug would violate.
    assert len(provider.calls) == cap
    assert result["calls_made"] == cap
    assert len(result["proposed"]) == cap


def test_cap_larger_than_candidate_count_calls_provider_once_per_candidate(
    adapter, downloads, other_config
):
    _make_files(downloads, 4)
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
    )

    result = propose_for_other_bucket(adapter, other_config, provider, cap=20)

    assert len(provider.calls) == 4
    assert result["calls_made"] == 4


def test_cap_of_zero_never_calls_provider(adapter, downloads, other_config):
    _make_files(downloads, 5)
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
    )

    result = propose_for_other_bucket(adapter, other_config, provider, cap=0)

    assert provider.calls == []
    assert result["calls_made"] == 0
    assert result["proposed"] == []


def test_default_cap_constant_is_twenty():
    assert DEFAULT_CAP == 20


# ---------------------------------------------------------------------------
# 2. Successful proposals become correctly-shaped QueueEntry objects.
# ---------------------------------------------------------------------------


def test_successful_proposal_becomes_correctly_shaped_queue_entry(adapter, downloads, other_config):
    files = [downloads / "invoice_final.dat"]
    files[0].write_bytes(b"pdf-ish-content")

    provider = AnthropicProvider(
        responses={
            "invoice_final.dat": ProposalResult(
                kind="success", bucket="invoices", confidence=0.87, rationale="looks like a bill"
            )
        }
    )

    result = propose_for_other_bucket(adapter, other_config, provider, cap=20)

    assert len(result["proposed"]) == 1
    entry = result["proposed"][0]
    assert entry.source == "ai:anthropic"
    assert entry.action == "move"
    assert entry.status == "pending"
    assert entry.group_key == "sort:invoices"
    assert entry.src == str(files[0])
    assert entry.dest.endswith(str(Path("_sorted") / "invoices" / "invoice_final.dat"))
    assert result["failures"] == []


def test_proposed_entries_are_actually_staged_in_the_queue(adapter, downloads, other_config):
    _make_files(downloads, 2, prefix="thing")
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.6, rationale="r")
    )

    propose_for_other_bucket(adapter, other_config, provider, cap=20)

    on_disk = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(on_disk) == 2
    assert {e.source for e in on_disk} == {"ai:anthropic"}
    assert {e.status for e in on_disk} == {"pending"}


# ---------------------------------------------------------------------------
# 3. Non-success proposals create NO queue entry and land in failures.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["auth_failure", "rate_limited", "timeout", "unparseable", "error"]
)
def test_non_success_proposal_creates_no_entry_and_is_recorded_as_failure(
    adapter, downloads, other_config, kind
):
    f = downloads / "weird_thing.dat"
    f.write_bytes(b"x")
    provider = AnthropicProvider(
        constant=ProposalResult(kind=kind, error=f"simulated {kind}")
    )

    result = propose_for_other_bucket(adapter, other_config, provider, cap=20)

    assert result["proposed"] == []
    assert len(result["failures"]) == 1
    failure = result["failures"][0]
    assert failure["filename"] == "weird_thing.dat"
    assert failure["kind"] == kind
    assert failure["error"] == f"simulated {kind}"

    on_disk = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert on_disk == []


# ---------------------------------------------------------------------------
# 4. Mix of successes and failures in one run.
# ---------------------------------------------------------------------------


def test_mix_of_successes_and_failures(adapter, downloads, other_config):
    ok = downloads / "ok_file.dat"
    ok.write_bytes(b"a")
    bad = downloads / "bad_file.dat"
    bad.write_bytes(b"b")

    provider = AnthropicProvider(
        responses={
            "ok_file.dat": ProposalResult(
                kind="success", bucket="misc", confidence=0.7, rationale="r"
            ),
            "bad_file.dat": ProposalResult(kind="timeout", error="took too long"),
        }
    )

    result = propose_for_other_bucket(adapter, other_config, provider, cap=20)

    assert result["calls_made"] == 2
    assert len(result["proposed"]) == 1
    assert result["proposed"][0].src == str(ok)
    assert len(result["failures"]) == 1
    assert result["failures"][0]["filename"] == "bad_file.dat"


# ---------------------------------------------------------------------------
# 5. Only 'other'-bucketed files are ever candidates.
# ---------------------------------------------------------------------------


def test_only_other_bucketed_files_are_proposed_for():
    # Uses the real DEFAULT_BUCKET_RULES this time: a .pdf matches a rule
    # (bucket="pdfs") and must never reach the provider at all.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        downloads = home / "Downloads"
        downloads.mkdir()
        (downloads / "resume.pdf").write_bytes(b"pdf")
        (downloads / "mystery.dat").write_bytes(b"?")

        adapter = _make_fake_adapter(home)
        provider = AnthropicProvider(
            constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
        )

        result = propose_for_other_bucket(
            adapter, config_module.DEFAULT_CONFIG, provider, cap=20
        )

        assert provider.calls == [("mystery.dat", provider.calls[0][1])]
        assert len(result["proposed"]) == 1
        assert result["proposed"][0].src.endswith("mystery.dat")


# ---------------------------------------------------------------------------
# 6. Route-level: POST /propose-ai.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(adapter):
    return create_app(adapter)


@pytest.fixture
def client(app):
    return app.test_client()


def test_propose_ai_route_creates_expected_queue_entries(adapter, client, downloads, monkeypatch):
    (downloads / "clip1.dat").write_bytes(b"a")
    (downloads / "clip2.dat").write_bytes(b"b")

    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.66, rationale="r")
    )
    monkeypatch.setattr("cleanup_tools.ui.routes.get_provider", lambda *a, **kw: provider)

    resp = client.post("/propose-ai")
    assert resp.status_code == 302
    assert "staged=2" in resp.headers["Location"]

    on_disk = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(on_disk) == 2
    assert {e.source for e in on_disk} == {"ai:anthropic"}
    assert {e.status for e in on_disk} == {"pending"}
    assert {e.group_key for e in on_disk} == {"sort:misc"}


def test_propose_ai_route_no_api_key_degrades_gracefully_not_500(client, monkeypatch):
    from cleanup_tools.ai import CredentialsError

    def _raise(*_a, **_kw):
        raise CredentialsError("No Anthropic API key found.")

    monkeypatch.setattr("cleanup_tools.ui.routes.get_provider", _raise)

    resp = client.post("/propose-ai")
    assert resp.status_code == 302
    assert resp.status_code != 500
    assert "plan_error" in resp.headers["Location"]


def test_propose_ai_route_missing_downloads_dir_does_not_crash(adapter, client, home, monkeypatch):
    assert not (home / "Downloads").exists()
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
    )
    monkeypatch.setattr("cleanup_tools.ui.routes.get_provider", lambda *a, **kw: provider)

    resp = client.post("/propose-ai")
    assert resp.status_code == 302
    assert "plan_error" in resp.headers["Location"]
    assert provider.calls == []


# ---------------------------------------------------------------------------
# 7. AI-sourced entries behave identically to manual ones through the
#    EXISTING approve/bulk-approve/undo routes -- zero special-casing.
# ---------------------------------------------------------------------------


def test_ai_entry_is_approved_rejected_and_undone_through_existing_routes(
    adapter, client, downloads, monkeypatch
):
    (downloads / "gizmo.dat").write_bytes(b"a")
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
    )
    monkeypatch.setattr("cleanup_tools.ui.routes.get_provider", lambda *a, **kw: provider)
    client.post("/propose-ai")

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 1
    entry_id = entries[0].id
    assert entries[0].source == "ai:anthropic"

    # The entry shows up in the plain review queue, same as manual entries.
    resp = client.get("/queue")
    assert resp.status_code == 200
    assert b"gizmo.dat" in resp.data

    # Approve via the existing single-entry route.
    resp = client.post(f"/queue/{entry_id}/approve")
    assert resp.status_code == 302
    approved = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))[0]
    assert approved.status == "approved"

    # Undo via the existing undo route. A freshly-staged entry (AI or
    # manual) has an empty status_history at staging time -- approve()
    # appends exactly one record ("approved"), so there is nothing before
    # it to revert to yet. This 400 is the SAME behavior a manually-staged
    # entry gets in this exact situation (see test_ui_routes.py's
    # test_undo_with_nothing_to_revert_returns_400_not_500) -- asserting it
    # here is itself proof of zero special-casing, not a workaround.
    resp = client.post(f"/queue/{entry_id}/undo")
    assert resp.status_code == 400
    still_approved = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))[0]
    assert still_approved.status == "approved"

    # Reject then undo back to pending DOES have a prior state to revert to
    # (the implicit initial status transition recorded by set_status the
    # first time an AI entry's status actually changes), proving undo works
    # identically to a manual entry once there IS history to unwind.
    resp = client.post(f"/queue/{entry_id}/reject")
    assert resp.status_code == 302
    rejected = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))[0]
    assert rejected.status == "rejected"

    resp = client.post(f"/queue/{entry_id}/undo")
    assert resp.status_code == 302
    undone = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))[0]
    assert undone.status == "approved"


def test_ai_entries_work_through_bulk_approve_by_group_key(adapter, client, downloads, monkeypatch):
    (downloads / "a.dat").write_bytes(b"a")
    (downloads / "b.dat").write_bytes(b"b")
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
    )
    monkeypatch.setattr("cleanup_tools.ui.routes.get_provider", lambda *a, **kw: provider)
    client.post("/propose-ai")

    resp = client.post("/queue/bulk-approve", data={"group_key": "sort:misc"})
    assert resp.status_code == 302

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 2
    assert {e.status for e in entries} == {"approved"}


def test_ai_approved_entry_executes_via_sort_from_queue(adapter, downloads, other_config, monkeypatch):
    from cleanup_tools.commands import sort as sort_module

    f = downloads / "widget.dat"
    f.write_bytes(b"widget-bytes")
    provider = AnthropicProvider(
        constant=ProposalResult(kind="success", bucket="misc", confidence=0.5, rationale="r")
    )

    result = propose_for_other_bucket(adapter, other_config, provider, cap=20)
    entry = result["proposed"][0]
    queue_module.set_status(adapter, entry.id, "approved", queue_module.default_queue_path(adapter))

    class Args:
        dir = str(downloads)
        go = False
        from_queue = True

    run_result = sort_module.run(adapter, Args())
    executed = run_result["from_queue"]["executed"]
    assert len(executed) == 1
    assert executed[0]["src"] == str(f)

    dest = downloads / sort_module.SORTED_SUBDIR / "misc" / "widget.dat"
    assert dest.is_file()
    assert not f.exists()

    final_entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert final_entries[0].status == "approved"
    assert final_entries[0].executed_at is not None

"""Wires the standalone AI-provider interface into the approval queue.

``ai/base.py`` and ``ai/anthropic_provider.py`` are deliberately standalone
(see their docstrings) -- nothing in ``queue.py``, ``ui/``, or
``commands/`` imports them. This module is the one bridge: it takes an
:class:`~cleanup_tools.ai.base.AIProvider`, asks it to propose a bucket for
each file :mod:`cleanup_tools.commands.sort`'s dry-run plan would otherwise
dump into the ambiguous ``"other"`` bucket, and stages every successful
proposal into the SAME approval queue manual entries use -- via
``queue.stage_entries()``, the identical function
``ui/routes.py``'s ``_stage_sort_plan()``/``_stage_reclaim_plan()`` already
use for manually-planned entries. Reusing that one function is what gives
AI-sourced entries "identical to manual, zero special-casing" behavior in
the approvals UI and in ``sort --from-queue`` execution: neither has to
know or care that an entry came from the AI rather than a human-triggered
plan.

Cap enforcement -- the single most important correctness property here
--------------------------------------------------------------------------
:func:`propose_for_other_bucket` is the ONLY caller of
``AIProvider.propose_bucket()`` in this codebase. The candidate list of
``'other'``-bucketed files is sliced down to ``cap`` items BEFORE the loop
below ever starts, so the provider is invoked AT MOST ``cap`` times, full
stop -- never once more, no matter how many ambiguous files the plan
contains. This is a pre-call gate, not a post-call filter: nothing here
ever calls the provider for a file and then discards the result to satisfy
the cap. If that were changed to "call for every candidate, then
``[:cap]`` the results", the cap would no longer bound the number of
network-backed calls at all -- which is exactly the property this module
exists to guarantee, since ``propose_bucket`` is this project's one
sanctioned network-calling code path (see ``ai/base.py``).
"""

from __future__ import annotations

from pathlib import Path

from .. import config as config_module
from .. import queue as queue_module
from ..adapters.base import OSAdapter
from ..commands import sort as sort_module
from .base import AIProvider

DEFAULT_CAP = 20


def _provider_source(provider: AIProvider) -> str:
    """Return a ``source`` tag like ``"ai:anthropic"`` for ``provider``.

    Derived from the provider's class name (``AnthropicProvider`` ->
    ``"ai:anthropic"``) rather than a hardcoded string, so a future second
    provider implementation gets a distinct, correct ``source`` tag
    automatically, without this module needing to special-case it by name.
    """
    name = type(provider).__name__
    if name.endswith("Provider"):
        name = name[: -len("Provider")]
    return f"ai:{name.lower() or 'unknown'}"


def _file_metadata(path: Path) -> dict:
    """Small, cheap metadata dict handed to the provider alongside the filename.

    Deliberately NOT ``queue.build_plan_snapshot()``'s full shape (which
    includes a content hash): the provider only ever sees the filename plus
    this metadata, never file contents (see ``ai/base.py``'s "never see the
    file's contents" contract, enforced by ``AnthropicProvider``'s own
    prompt), so hashing every candidate's content just to describe it in a
    prompt would be wasted work that couldn't help the classification
    anyway. Returns ``{}`` (rather than raising) if ``path`` can't be
    stat'd -- the proposal can still proceed on filename alone.
    """
    try:
        stat = path.stat()
    except OSError:
        return {}
    return {
        "extension": path.suffix.lstrip(".").lower(),
        "size_bytes": stat.st_size,
    }


def propose_for_other_bucket(
    adapter: OSAdapter,
    config: config_module.Config,
    provider: AIProvider,
    cap: int = DEFAULT_CAP,
    *,
    target_dir: Path | None = None,
    queue_path: Path | None = None,
) -> dict:
    """Propose AI buckets for files sort.py's dry-run plan puts in ``'other'``.

    Runs the same planning logic ``sort.run()`` uses (``sort._plan()``)
    against ``target_dir`` (default: the platform Downloads dir -- the same
    default ``sort.run()`` falls back to), filters to the entries bucketed
    ``'other'`` (the files no configured bucket rule matched -- the
    ambiguous ones the AI is meant to help with), and asks ``provider`` to
    propose a real bucket for up to ``cap`` of them.

    See the module docstring for why the cap is enforced by slicing the
    candidate list BEFORE the loop, rather than truncating results after
    calling the provider for every candidate.

    Every successful proposal (``ProposalResult.kind == "success"``)
    becomes a ``QueueEntry`` with:

    - ``source=f"ai:<provider>"`` (e.g. ``"ai:anthropic"``, see
      ``_provider_source``)
    - ``action="move"``, ``status="pending"`` (the ``QueueEntry`` default)
    - ``group_key=f"sort:{location}:{bucket}"`` -- the current 3-segment,
      location-aware grouping scheme (via ``config.location_for_src``)
      every other staging path in this app uses, so an AI-proposed
      "screenshots" entry lands in the same dashboard group as a
      manually-planned "screenshots" entry for the same location. (Fixed
      from an old 2-segment ``sort:{bucket}`` format as part of the
      chat-agent-plan-builder epic, which already touches this staging
      convention elsewhere -- ``parse_group_key`` already defensively
      handles the old format for any already-staged entries, so no
      migration is needed.)
    - ``dest`` computed the same way ``sort._plan()`` computes it for a
      rule-matched bucket: ``target_dir / sort.SORTED_SUBDIR / bucket /
      filename``
    - ``plan_snapshot`` from ``queue.build_plan_snapshot()``, so
      ``check_staleness()`` works identically to a manually-staged entry
      when this entry is later approved and executed via
      ``sort --from-queue``

    and is staged via ``queue.stage_entries()`` -- the SAME function manual
    staging uses, and the source of the "zero special-casing" guarantee
    described in the module docstring.

    Every non-success proposal creates NO queue entry, and is instead
    recorded in the returned ``"failures"`` list as
    ``{"filename", "src", "kind", "error"}``.

    Returns ``{"proposed": list[QueueEntry], "failures": list[dict],
    "calls_made": int}``. ``"proposed"`` holds whatever
    ``queue.stage_entries()`` actually added (its own pending-src dedup
    still applies, exactly as it does for manual planning routes) -- not
    necessarily every successful proposal, if one happened to duplicate an
    already-pending entry's ``src``. ``calls_made`` is always
    ``len(candidates) <= cap``, regardless of how many of those calls
    succeeded.
    """
    if target_dir is None:
        target_dir = adapter.resolve_standard_dir("downloads")
    target_dir = Path(target_dir)

    # sort._plan() itself does no existence check (os.walk() over a missing
    # dir just yields nothing, silently), so this mirrors sort.run()'s own
    # explicit guard: a typo'd or since-deleted target directory is a loud
    # error here too, not indistinguishable from "no 'other' files to
    # propose for". This is also what lets callers (e.g. the /propose-ai
    # route) catch FileNotFoundError the same way they already do for
    # sort.run() via plan_sort.
    if not target_dir.is_dir():
        raise FileNotFoundError(f"sort target directory does not exist: {target_dir}")

    plan = sort_module._plan(adapter, config, [target_dir])
    other_items = [item for item in plan if item["bucket"] == "other"]

    # THE cap gate: slice to `cap` candidates BEFORE any provider call is
    # made. The loop below iterates this (already-capped) list exactly
    # once per item -- there is no path from here that calls
    # provider.propose_bucket() more than len(candidates) <= cap times.
    candidates = other_items[: max(cap, 0)]

    source = _provider_source(provider)
    to_stage: list[queue_module.QueueEntry] = []
    failures: list[dict] = []
    calls_made = 0

    for item in candidates:
        src_path = Path(item["src"])
        filename = src_path.name
        metadata = _file_metadata(src_path)

        result = provider.propose_bucket(filename, metadata)
        calls_made += 1

        if result.kind != "success":
            failures.append(
                {
                    "filename": filename,
                    "src": str(src_path),
                    "kind": result.kind,
                    "error": result.error,
                }
            )
            continue

        bucket = result.bucket
        dest = target_dir / sort_module.SORTED_SUBDIR / bucket / filename
        location = config_module.location_for_src(str(src_path), config, adapter)
        to_stage.append(
            queue_module.QueueEntry(
                action="move",
                src=str(src_path),
                dest=str(dest),
                source=source,
                status="pending",
                group_key=f"sort:{location}:{bucket}",
                plan_snapshot=queue_module.build_plan_snapshot(src_path),
            )
        )

    proposed = queue_module.stage_entries(adapter, to_stage, queue_path) if to_stage else []

    return {"proposed": proposed, "failures": failures, "calls_made": calls_made}

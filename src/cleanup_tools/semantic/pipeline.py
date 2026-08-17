"""Orchestrates local document-topic clustering end to end: recursive scan
-> extract/embed/index (incremental) -> cluster over the full accumulated
index -> stage real ``QueueEntry`` "move" proposals via
``queue.stage_entries()`` -- the SAME approval queue every other pipeline in
this app uses, never a parallel state store.

See ``.pHive/epics/document-topic-clustering/docs/design-discussion.md`` for
the full architecture; the three load-bearing correctness properties this
module implements are documented inline at each guard below.
"""

from __future__ import annotations

from pathlib import Path

from .. import config as config_module
from .. import queue as queue_module
from ..adapters.base import OSAdapter
from . import cluster as cluster_module
from . import embeddings as embeddings_module
from . import extract as extract_module
from . import index as index_module

CLUSTER_SUBDIR = "_clusters"
TOPIC_SUBDIR = "by-topic"


def run(
    adapter: OSAdapter,
    *,
    queue_path: Path | None = None,
    dirs: list[str] | None = None,
    threshold: float = cluster_module.DEFAULT_THRESHOLD,
) -> dict:
    """Scan configured (or explicitly given) locations, extract+embed+index
    any new documents, cluster over the FULL accumulated index (not just this
    run's new files -- a previous run's already-indexed documents are always
    reconsidered too), and stage move proposals for the resulting groups.

    Raises ``NotImplementedError`` on any non-macOS adapter -- the whole
    pipeline is gated once, up front, rather than only the specific calls
    that technically need PyObjC, matching this epic's "the whole feature is
    macOS-only in phase 1" scoping (extract.py/embeddings.py's own module
    docstrings).

    Returns ``{"staged_entry_ids": list[str], "clusters_found": int,
    "files_scanned": int, "files_indexed": int}``.
    """
    embedder = embeddings_module.get_embedder(adapter)  # platform gate, fires before any scanning.

    config = config_module.load_config(adapter)
    locations = dirs if dirs else config_module.configured_locations(config, adapter)

    files_scanned = 0
    files_indexed = 0

    for location in locations:
        location_path = Path(location)
        if not location_path.is_dir():
            continue

        # Recursive, not shallow -- documents worth topic-clustering live
        # nested in folders (~/Documents/Taxes/2023/, ...), unlike sort's
        # flat Downloads-clutter scan. adapter.list_dir(max_depth=None) is
        # this app's existing unlimited-depth walk primitive.
        for file_path in adapter.list_dir(location_path, max_depth=None):
            if file_path.name.startswith("."):
                continue
            if queue_module.is_protected_path(file_path, adapter):
                continue
            if not _is_safe_to_read(file_path):
                # An un-materialized iCloud placeholder -- this pipeline's
                # first content-reading code path must never force a
                # download just by trying to read it. See the design
                # discussion §2.6.
                continue

            files_scanned += 1
            _index_one_file(adapter, embedder, file_path)
            files_indexed += 1

    new_entries, clusters_found = _stage_clusters(adapter, config, queue_path, threshold)

    return {
        "staged_entry_ids": [e.id for e in new_entries],
        "clusters_found": clusters_found,
        "files_scanned": files_scanned,
        "files_indexed": files_indexed,
    }


def _index_one_file(adapter: OSAdapter, embedder, file_path: Path) -> None:
    """Extract+embed+index ``file_path`` if it isn't already indexed by
    content hash. A no-op if extraction returns nothing usable (unsupported
    type, corrupt file, ...) -- never raises for a real-but-unusual file.
    """
    snapshot = queue_module.build_plan_snapshot(file_path)
    content_hash = snapshot.get("content_hash")
    if not content_hash:
        return  # not a plain file (directory, broken symlink, ...) -- nothing to embed.

    if index_module.is_indexed(adapter, content_hash, kind=index_module.KIND_DOCUMENT):
        return  # incremental -- unchanged content already embedded.

    text = extract_module.extract_text(file_path, adapter)
    if text is None:
        return

    vector = embedder.embed(text)
    index_module.add_embedding(
        adapter, content_hash, str(file_path), vector, kind=index_module.KIND_DOCUMENT, text=text
    )


def _stage_clusters(
    adapter: OSAdapter, config: config_module.Config, queue_path: Path | None, threshold: float
) -> tuple[list[queue_module.QueueEntry], int]:
    """Cluster over the full accumulated index and stage move proposals.

    Returns ``(staged_entries, clusters_found)`` -- clustering is computed
    exactly once here; ``run()`` reads both results from this single call
    rather than re-clustering a second time just to report a count.
    """
    all_embeddings = index_module.get_embeddings(adapter, kind=index_module.KIND_DOCUMENT)
    clusters = cluster_module.cluster(
        [e.embedding for e in all_embeddings],
        [e.text or "" for e in all_embeddings],
        threshold=threshold,
    )

    new_entries: list[queue_module.QueueEntry] = []
    claimed_dests: set[str] = set()

    for one_cluster in clusters:
        for member_index in one_cluster.member_indices:
            member = all_embeddings[member_index]
            src_path = Path(member.path)
            if not src_path.exists():
                continue  # moved/deleted since indexing -- nothing to propose.
            if queue_module.is_protected_path(src_path, adapter):
                # Defensive re-check, not just belt-and-suspenders: the scan
                # loop already refuses a protected path before it's ever
                # indexed, but staging reads from the ACCUMULATED index
                # (built up across every past run), not a fresh scan -- so
                # this also refuses a path indexed under a location that
                # later became protected (e.g. PROTECTED_PATH_ROOTS or
                # search_roots changed since indexing), the same way every
                # other staging path in this app never trusts a single
                # earlier check to hold forever.
                continue

            location = config_module.location_for_src(str(src_path), config, adapter)
            dest = _disambiguated_dest(location, one_cluster.slug, src_path, claimed_dests)
            claimed_dests.add(str(dest))

            new_entries.append(
                queue_module.QueueEntry(
                    action="move",
                    src=str(src_path.resolve()),
                    dest=str(dest),
                    source="local:cluster",
                    group_key=f"cluster:{location}:{one_cluster.slug}",
                    plan_snapshot=queue_module.build_plan_snapshot(src_path),
                )
            )

    if not new_entries:
        return [], len(clusters)
    path = queue_path or queue_module.default_queue_path(adapter)
    staged = queue_module.stage_entries(adapter, new_entries, path)
    return staged, len(clusters)


def _disambiguated_dest(location: str, slug: str, src_path: Path, claimed: set[str]) -> Path:
    """``<location>/_clusters/by-topic/<slug>/<filename>``, disambiguated on
    collision -- because the scan is recursive (unlike ``sort``'s shallow,
    single-namespace scan), two files with the same name from different
    subfolders can legitimately cluster together and need distinct
    destinations. Never silently overwrites: a candidate that already
    exists on disk, or was already claimed by another entry proposed in this
    same run, gets a suffix derived from its own original parent directory
    appended before the extension.
    """
    base_dir = Path(location) / CLUSTER_SUBDIR / TOPIC_SUBDIR / slug
    candidate = base_dir / src_path.name
    if str(candidate) not in claimed and not candidate.exists():
        return candidate

    parent_hint = src_path.parent.name or "root"
    stem, suffix = src_path.stem, src_path.suffix
    disambiguated = base_dir / f"{stem}__{parent_hint}{suffix}"
    counter = 2
    while str(disambiguated) in claimed or disambiguated.exists():
        disambiguated = base_dir / f"{stem}__{parent_hint}-{counter}{suffix}"
        counter += 1
    return disambiguated


def _is_safe_to_read(path: Path) -> bool:
    """True if ``path`` can be read without forcing an iCloud download.

    Two checks: the classic evicted-placeholder pattern (a
    ``.name.ext.icloud`` dotfile sitting where the real file would be), and
    ``NSURLUbiquitousItemDownloadingStatusKey`` for a file that exists under
    its real name but isn't fully resident. Best-effort: if the platform
    check itself fails for any reason, this does not block the whole
    pipeline on it -- a real read failure downstream (extract_text returning
    ``None``) is the existing, already-safe fallback either way.
    """
    if path.name.startswith(".") and path.name.endswith(".icloud"):
        return False

    try:
        from Foundation import (
            NSURL,
            NSURLIsUbiquitousItemKey,
            NSURLUbiquitousItemDownloadingStatusKey,
            NSURLUbiquitousItemDownloadingStatusNotDownloaded,
        )

        url = NSURL.fileURLWithPath_(str(path))
        values, _error = url.resourceValuesForKeys_error_(
            [NSURLIsUbiquitousItemKey, NSURLUbiquitousItemDownloadingStatusKey], None
        )
        if values and values.get(NSURLIsUbiquitousItemKey):
            status = values.get(NSURLUbiquitousItemDownloadingStatusKey)
            if status == NSURLUbiquitousItemDownloadingStatusNotDownloaded:
                return False
    except Exception:
        pass

    return True

"""Orchestrates local document-topic clustering (``run``) and local
photo-person clustering (``run_faces``) end to end: recursive scan ->
extract/embed/index (incremental) -> cluster over the full accumulated
index -> stage real ``QueueEntry`` "move" proposals via
``queue.stage_entries()`` -- the SAME approval queue every other pipeline in
this app uses, never a parallel state store.

Both pipelines share ONE walk (:func:`_walk_candidate_files`: recursive
scan + protected-path + iCloud-materialization guards) -- ``run_faces``
(``photo-face-clustering``) reuses this module's existing walk rather than
a second, parallel implementation, per that epic's own design discussion §6.

See ``.pHive/epics/document-topic-clustering/docs/design-discussion.md``
and ``.pHive/epics/photo-face-clustering/docs/design-discussion.md`` for
the full architecture; the load-bearing correctness properties each
pipeline implements are documented inline at each guard below.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .. import config as config_module
from .. import queue as queue_module
from ..adapters.base import OSAdapter
from . import cluster as cluster_module
from . import embeddings as embeddings_module
from . import extract as extract_module
from . import faces as faces_module
from . import index as index_module

CLUSTER_SUBDIR = "_clusters"
TOPIC_SUBDIR = "by-topic"
PERSON_SUBDIR = "by-person"


def run(
    adapter: OSAdapter,
    *,
    queue_path: Path | None = None,
    dirs: list[str] | None = None,
    threshold: float = cluster_module.DEFAULT_THRESHOLD,
    progress_callback=None,
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

    ``progress_callback``, if given, is called once per scanned (guard-
    passing) file as ``progress_callback(current, current)`` -- the total
    file count isn't known upfront without a separate full listing pass, so
    (like several other jobs in this app that can't know their total ahead
    of time) it's reported growing together with ``current`` rather than
    against a fixed denominator.

    Returns ``{"staged_entry_ids": list[str], "clusters_found": int,
    "files_scanned": int, "files_indexed": int}``. ``files_indexed`` counts
    only files that were ACTUALLY newly embedded this run (not already in
    the index, and real text was extracted) -- it is not simply
    ``files_scanned`` under a different name.
    """
    embedder = embeddings_module.get_embedder(adapter)  # platform gate, fires before any scanning.

    config = config_module.load_config(adapter)
    locations = dirs if dirs else config_module.configured_locations(config, adapter)

    files_scanned = 0
    files_indexed = 0

    for file_path in _walk_candidate_files(adapter, locations):
        files_scanned += 1
        if _index_one_file(adapter, embedder, file_path):
            files_indexed += 1
        if progress_callback is not None:
            progress_callback(files_scanned, files_scanned)

    new_entries, clusters_found = _stage_clusters(adapter, config, queue_path, threshold)

    return {
        "staged_entry_ids": [e.id for e in new_entries],
        "clusters_found": clusters_found,
        "files_scanned": files_scanned,
        "files_indexed": files_indexed,
    }


def _walk_candidate_files(adapter: OSAdapter, locations: list[str]) -> Iterator[Path]:
    """Recursive scan of ``locations``, yielding every file that passes
    this pipeline's guards -- shared by ``run()`` (documents) and
    ``run_faces()`` (photos), never duplicated between them.

    Guards, in order: not a dotfile, not under a protected path
    (``queue.is_protected_path``), and not an un-materialized iCloud
    placeholder (see :func:`_is_safe_to_read`) -- this pipeline's first
    content-reading code path must never force a download just by trying
    to read a file. See the design discussion's §2.6.

    Recursive, not shallow -- documents/photos worth clustering live nested
    in folders (``~/Documents/Taxes/2023/``, ``~/Pictures/2024/``, ...),
    unlike ``sort``'s flat Downloads-clutter scan.
    ``adapter.list_dir(max_depth=None)`` is this app's existing unlimited-
    depth walk primitive.
    """
    for location in locations:
        location_path = Path(location)
        if not location_path.is_dir():
            continue

        for file_path in adapter.list_dir(location_path, max_depth=None):
            if file_path.name.startswith("."):
                continue
            if queue_module.is_protected_path(file_path, adapter):
                continue
            if not _is_safe_to_read(file_path):
                continue
            yield file_path


def _index_one_file(adapter: OSAdapter, embedder, file_path: Path) -> bool:
    """Extract+embed+index ``file_path`` if it isn't already indexed by
    content hash. A no-op if extraction returns nothing usable (unsupported
    type, corrupt file, ...) -- never raises for a real-but-unusual file.

    Returns whether a new embedding was actually stored this call -- callers
    use this to distinguish real work from an already-indexed/unextractable
    skip, rather than treating every scanned file as "indexed."
    """
    snapshot = queue_module.build_plan_snapshot(file_path)
    content_hash = snapshot.get("content_hash")
    if not content_hash:
        return False  # not a plain file (directory, broken symlink, ...) -- nothing to embed.

    if index_module.is_indexed(adapter, content_hash, kind=index_module.KIND_DOCUMENT):
        return False  # incremental -- unchanged content already embedded.

    text = extract_module.extract_text(file_path, adapter)
    if text is None:
        return False

    vector = embedder.embed(text)
    index_module.add_embedding(
        adapter, content_hash, str(file_path), vector, kind=index_module.KIND_DOCUMENT, text=text
    )
    return True


def run_faces(
    adapter: OSAdapter,
    *,
    queue_path: Path | None = None,
    dirs: list[str] | None = None,
    threshold: float = cluster_module.DEFAULT_THRESHOLD,
    progress_callback=None,
) -> dict:
    """Scan configured (or explicitly given) locations, detect+embed+index
    faces in any new photos (incremental by content hash -- see
    ``index.is_scanned``/``index.mark_scanned_no_faces`` for how a
    zero-face photo is distinguished from a never-scanned one), cluster
    over the FULL accumulated face index, and stage move proposals -- ONLY
    for photos where every detected face maps to the SAME person-cluster
    (see :func:`_stage_face_clusters`).

    Reuses this module's SAME recursive-scan/protected-path/iCloud-guard
    walk as ``run()`` (:func:`_walk_candidate_files`) -- not a second,
    parallel implementation. See
    ``.pHive/epics/photo-face-clustering/docs/design-discussion.md`` §6.

    Unlike ``run()``, this is genuinely cross-platform (no
    ``NotImplementedError`` gate) -- see ``faces.get_face_detector``'s own
    docstring for why no local-native, Mac-only alternative exists here.

    Returns the same shape as ``run()``: ``{"staged_entry_ids",
    "clusters_found", "files_scanned", "files_indexed"}``.
    """
    detector = faces_module.get_face_detector(adapter)

    config = config_module.load_config(adapter)
    locations = dirs if dirs else config_module.configured_locations(config, adapter)

    files_scanned = 0
    files_indexed = 0

    for file_path in _walk_candidate_files(adapter, locations):
        if file_path.suffix.lower() not in extract_module.IMAGE_EXTENSIONS:
            continue  # face detection only makes sense on image files.

        files_scanned += 1
        if _index_one_photo(adapter, detector, file_path):
            files_indexed += 1
        if progress_callback is not None:
            progress_callback(files_scanned, files_scanned)

    new_entries, clusters_found = _stage_face_clusters(adapter, config, queue_path, threshold)

    return {
        "staged_entry_ids": [e.id for e in new_entries],
        "clusters_found": clusters_found,
        "files_scanned": files_scanned,
        "files_indexed": files_indexed,
    }


def _index_one_photo(adapter: OSAdapter, detector, file_path: Path) -> bool:
    """Detect+embed+index every face in ``file_path`` if it hasn't already
    been scanned (by content hash). A photo with zero detected faces is
    marked via ``index.mark_scanned_no_faces`` so it isn't re-scanned every
    single run either -- without that, a zero-face photo would be
    indistinguishable from a never-scanned one and pay the real detection
    cost again on every incremental run (see that function's docstring).

    Returns whether this photo was ACTUALLY newly scanned this call (true
    whether 0 or more faces were found) -- callers use this to distinguish
    real work from an already-scanned skip, mirroring
    ``_index_one_file``'s own ``files_indexed`` contract for documents.
    """
    snapshot = queue_module.build_plan_snapshot(file_path)
    content_hash = snapshot.get("content_hash")
    if not content_hash:
        return False  # not a plain file (directory, broken symlink, ...) -- nothing to scan.

    if index_module.is_scanned(adapter, content_hash, kind=index_module.KIND_FACE):
        return False  # incremental -- unchanged content already scanned.

    detections = detector.detect(file_path)
    if not detections:
        index_module.mark_scanned_no_faces(adapter, content_hash, str(file_path))
        return True

    for face_index, detection in enumerate(detections):
        index_module.add_embedding(
            adapter,
            content_hash,
            str(file_path),
            detection.embedding,
            kind=index_module.KIND_FACE,
            face_index=face_index,
            bbox=detection.bbox,
        )
    return True


def _stage_face_clusters(
    adapter: OSAdapter, config: config_module.Config, queue_path: Path | None, threshold: float
) -> tuple[list[queue_module.QueueEntry], int]:
    """Cluster over the full accumulated FACE index and stage move
    proposals -- ONLY for photos where every detected face maps to the
    SAME person-cluster (design discussion §5's multi-person-photo
    exclusion). A photo with any unclustered (singleton) face, or with
    faces spanning more than one cluster, is never staged -- both cases
    fail the "exactly one distinct cluster id, no Nones" check below.

    Returns ``(staged_entries, clusters_found)`` -- same shape as
    ``_stage_clusters`` (documents).
    """
    all_faces = index_module.get_embeddings(adapter, kind=index_module.KIND_FACE)
    clusters = cluster_module.cluster([f.embedding for f in all_faces], threshold=threshold, prefix="person")

    face_index_to_cluster: dict[int, int] = {}
    for cluster_idx, one_cluster in enumerate(clusters):
        for member_index in one_cluster.member_indices:
            face_index_to_cluster[member_index] = cluster_idx

    # Group faces by (content_hash, path) -- one photo, potentially many faces.
    photos: dict[tuple[str, str], list[int]] = {}
    for i, face in enumerate(all_faces):
        photos.setdefault((face.content_hash, face.path), []).append(i)

    new_entries: list[queue_module.QueueEntry] = []
    claimed_dests: set[str] = set()

    # Deterministic order: sorted keys, not dict/set iteration.
    for content_hash, photo_path in sorted(photos):
        face_positions = photos[(content_hash, photo_path)]
        cluster_ids = {face_index_to_cluster.get(pos) for pos in face_positions}
        if None in cluster_ids or len(cluster_ids) != 1:
            # None => at least one face is an unclustered singleton;
            # len != 1 => faces span more than one identity. Either way,
            # this is not a single-identity photo -- never staged.
            continue

        one_cluster = clusters[next(iter(cluster_ids))]

        src_path = Path(photo_path)
        if not src_path.exists():
            continue  # moved/deleted since indexing -- nothing to propose.
        if queue_module.is_protected_path(src_path, adapter):
            # Defensive re-check -- same reasoning as _stage_clusters:
            # staging reads from the accumulated index, not a fresh scan.
            continue

        location = config_module.location_for_src(str(src_path), config, adapter)
        dest = _disambiguated_dest(location, one_cluster.slug, src_path, claimed_dests, subdir=PERSON_SUBDIR)
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


def _disambiguated_dest(
    location: str, slug: str, src_path: Path, claimed: set[str], *, subdir: str = TOPIC_SUBDIR
) -> Path:
    """``<location>/_clusters/<subdir>/<slug>/<filename>``, disambiguated on
    collision -- because the scan is recursive (unlike ``sort``'s shallow,
    single-namespace scan), two files with the same name from different
    subfolders can legitimately cluster together and need distinct
    destinations. Never silently overwrites: a candidate that already
    exists on disk, or was already claimed by another entry proposed in this
    same run, gets a suffix derived from its own original parent directory
    appended before the extension.

    ``subdir`` defaults to ``TOPIC_SUBDIR`` (``"by-topic"``,
    document-topic-clustering's convention) -- ``run_faces()`` passes
    ``PERSON_SUBDIR`` (``"by-person"``) instead, so a human browsing
    ``_clusters/`` sees two clearly separated kinds of grouping and a topic
    slug can never coincidentally collide with a person slug
    (photo-face-clustering's own grill finding T1).
    """
    base_dir = Path(location) / CLUSTER_SUBDIR / subdir / slug
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

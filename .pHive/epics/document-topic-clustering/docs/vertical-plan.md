# Vertical plan: document-topic-clustering

Five slices. Executed sequentially this session (no concurrent agent dispatch), though
slices 1 and 2 are architecturally independent of each other per the horizontal plan.

## Slice 1 — semantic-extraction-and-embedding

`semantic/embeddings.py` (`TextEmbedder` ABC + `AppleTextEmbedder` + factory) and
`semantic/extract.py` (PDFKit/Vision/plain-text extraction). Includes the load-bearing
PyObjC-import-and-call spike (confirm `pyobjc-framework-NaturalLanguage`/`Vision`/`Quartz`
actually work in this dev environment) as this story's first concrete step — a real go/no-
go signal for the epic's whole dependency-footprint decision. **Working state:** a Python
function you can call on a real `.txt`/`.pdf`/screenshot file on this Mac and get back real
extracted text and a real embedding vector; `NotImplementedError` on a non-macOS adapter.

## Slice 2 — semantic-index-and-clustering

`semantic/index.py` (content-hash-keyed incremental SQLite cache) and `semantic/cluster.py`
(cosine-similarity threshold + union-find, local label/slug heuristic). Fully testable with
fake embedding vectors — no dependency on slice 1's real PyObjC calls. **Working state:**
given a set of fake (test) embeddings with known similarity relationships, the clustering
function returns the expected groupings and stable slugs; given real files, the index
correctly skips re-embedding anything already indexed by content hash.

## Slice 3 — semantic-pipeline-and-queue-integration

`semantic/pipeline.py` wiring slices 1+2 together: recursive scan of configured locations,
iCloud-placeholder-materialized guard (design discussion §2.6), protected-path reuse
(`queue.is_protected_path`), dest-collision disambiguation (grill-T1's resolution), and
staging via `queue.stage_entries()` with `group_key=f"cluster:{location}:{slug}"` — plus
extending `queue.parse_group_key`/`group_entries_hierarchical` to recognize the new
`"cluster"` pipeline. **Working state:** calling one function against a real folder of
documents produces real, correctly-shaped pending `QueueEntry` proposals in the actual
queue file, visible in the existing dashboard tree's location→bucket grouping, entirely
offline — the epic's actual headline capability, provable end-to-end before any UI trigger
exists.

## Slice 4 — semantic-ui-and-settings

The `/plan/cluster-documents` trigger route + nav link (mirrors `plan_sort`), the
extracted-text-snippet disclosure on `source="local:cluster"` queue entries, and the new
`semantic_cluster_threshold` Settings field. **Working state:** a real, clickable
end-to-end feature in the running app — click "Plan: Cluster Documents," see real staged
proposals with a "why was this grouped" disclosure in the Review Queue, adjust the
similarity threshold in Settings.

## Slice 5 — semantic-packaging-and-verification

`pyproject.toml`'s new `semantic` extras group + `numpy` base dependency,
`packaging/pyinstaller/cleanup_ui.spec`'s new hiddenimports, and a real manual verification
pass against a frozen build (launch the packaged app, run the feature end-to-end) — closing
out the design discussion's stated PyObjC-under-PyInstaller risk with an actual pass/fail,
not an assumption. **Working state:** the feature works identically from the frozen
desktop-app build, not just from a dev `python -m` invocation.

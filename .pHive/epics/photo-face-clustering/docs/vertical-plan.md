# Vertical plan: photo-face-clustering

Five slices, sequential (mirrors `document-topic-clustering`'s own shape; slices 1 and 2
are architecturally independent per the horizontal plan but executed sequentially this
session).

## Slice 1 — face-detection-and-embedding

`semantic/faces.py`: InsightFace detection+embedding via `onnxruntime`, vendored model
weights, the offline-reliability/no-auto-download verification spike as the first concrete
step (a real go/no-go signal, same discipline as phase 1's PyObjC spike). **Working
state:** a Python function you can call on a real photo and get back real per-face bounding
boxes + 512-d embeddings, entirely from vendored local files, on Mac AND Arch identically.

## Slice 2 — face-index-and-clustering

`semantic/index.py`'s `kind`/per-face schema extension + reused `cluster.py`. **Working
state:** given fake face embeddings with known identity relationships, clustering returns
the expected person-groupings, correctly isolated from any document-kind embeddings already
in the same index.

## Slice 3 — face-pipeline-and-single-identity-staging

Reuses `document-topic-clustering`'s walk, adds the multi-person-photo exclusion rule and
the `by-person/` dest convention. **Working state:** calling one function against a real
folder of photos produces real, correctly-shaped, single-identity-only pending `QueueEntry`
proposals in the actual queue — group photos are indexed but correctly never staged.

## Slice 4 — face-ui-and-settings

"Plan: Cluster Photos by Person" trigger + nav link, reusing the existing thumbnail
rendering unmodified, plus the new `semantic_face_cluster_threshold` Settings field.
**Working state:** a real, clickable end-to-end feature — click the trigger, see real
staged person-cluster proposals with photo thumbnails in the Review Queue.

## Slice 5 — face-packaging-and-verification

The heavier `semantic` extras group, vendored model-weight bundling, PyInstaller
hiddenimports/datas, the offline-reliability regression test, and a real frozen-build
manual verification pass. **Working state:** the feature works identically from the frozen
desktop-app build, with a documented, real pass/fail verdict on the ~300-500MB bundling
question this epic's design discussion flagged as genuinely unverified going in.

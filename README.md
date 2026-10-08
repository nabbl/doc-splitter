# Doc splitter

A headless, local PDF preprocessor for scanner batches and Paperless. One worker
polls a separate bulk inbox, reuses page text or runs German/English Tesseract,
detects boundaries with CPU ONNX, exports **original PDF pages**, and delivers
complete PDFs only. It does not classify documents, remove blank backs, reconstruct
shuffled pages, call cloud inference, or publish failed batches unsplit.

**Deployment status:** implementation and isolated acceptance are separate from
production deployment. The operator requested ownership of live verification.
The GitOps integration belongs in `nabbl/gitops-arcane/doc-splitter`, not a parallel
unmanaged stack. Actual Arcane sync, host paths/permissions and Paperless ingestion
must be verified by that operator. See [deployment handoff](docs/deployment.md)
and [acceptance evidence](docs/acceptance.md).

## Model and boundaries

Model: [`nutrientdocs/doc-split-v1`](https://huggingface.co/nutrientdocs/doc-split-v1/tree/dccadc69dd4ae69c63f42aa6715f249c6e5e6cf8),
revision **`dccadc69dd4ae69c63f42aa6715f249c6e5e6cf8`**, Apache-2.0.
Eight artifacts, including model card/configs, are SHA-256 pinned in `model.py`.
Startup validates every file and loads/warms all ONNX graphs before accepting jobs.
Missing or damaged artifacts download via HTTPS into `.part` files; checksum-verified
files are atomically installed and fsynced. Interrupted artifacts are re-downloaded
on restart, not falsely treated as complete. A complete cache starts offline.
Weights (~951 MB decimal) never enter Git or download per document.

Inference follows the pinned model card, implemented independently:
RGB PIL resize to 512x512, float32 normalization `(pixel/255 - .5)/.5`, NCHW;
bundled XLM-R tokenizer with `query: ` prefix, pad ID 1, truncation at 512 tokens;
text gates suppress absent text; image/text encoders use bounded page batches.
**All page embeddings remain ordered and enter one full-sequence boundary head**,
not independent chunk heads. CRF forward/backward smoothing uses `crf.json`,
first-page logit is forced to 30, and beta calibration uses
`sigmoid(0.516*ln(p) - 0.402*ln(1-p) - 0.155)`. Page one always starts a document.
No demo code or its 40-page cap is copied.

Lower `SPLIT_THRESHOLD` causes more splitting; higher values risk merging
unrelated documents. Default is 0.5 on **calibrated** scores. Scores do not prove
whole-batch correctness, and v1 accuracy is not claimed equivalent to a prior
demo that might have used commercial v2. `SPLIT_REVIEW_MARGIN=0` disables optional
review near the threshold; nonzero values quarantine such proposals instead of
publishing. This is an operator policy, not a statistical correctness guarantee.

The current validated sequence ceiling is **128 pages**; larger batches are
quarantined whole, never truncated. Rendering is one page at a time, encoders use
two pages by default, and the head sees the complete bounded sequence. Render pixel,
source byte, OCR time, job time, worker CPU and container memory limits are explicit.
Large/unusual pages exceeding the pixel bound are rejected, not silently downscaled.

## Installation and configuration

Python 3.12/3.13 dependencies and wheel hashes are in `uv.lock`. Use
`uv sync --locked`; local runs additionally require Tesseract with `deu` and `eng`
language data. The Docker image includes both languages, runs non-root, pins base
images by digest, and provides `doc-splitter` as its entrypoint.

All eight directory paths must be explicitly supplied. `.env.example` lists the
container paths and every application setting. Shell use requires exporting these
variables; the CLI does not implicitly load a `.env` file.

| Setting | Default / requirement |
| --- | --- |
| `SPLIT_INBOX`, `CONSUME`, `STAGING`, `ARCHIVE`, `REVIEW`, `WORK`, `STATE`, `MODEL_CACHE` | Eight absolute, pre-created, non-overlapping roots; prefix every name with `SPLIT_` |
| `SPLIT_MODEL_REVISION` | The audited commit above; other values fail until hashes/code are reviewed |
| `SPLIT_THRESHOLD`, `REVIEW_MARGIN`, `DRY_RUN` | `0.5`, `0`, `false` |
| `SPLIT_DELETE_COMPLETED_INPUTS`, `REMOVE_BLANK_PAGES` | Both `false`; explicit opt-ins described below |
| `SPLIT_POLL_SECONDS`, `SETTLE_SECONDS`, `COMPLETION_MODE` | `5`, `30`, `settle` |
| `SPLIT_OCR_LANGUAGES`, `OCR_TIMEOUT` | `deu+eng`, 120 seconds per page |
| `SPLIT_BATCH_SIZE`, `THREADS` | 2 pages, 2 CPU inference/OCR threads |
| `SPLIT_MAX_PAGES`, `MAX_SOURCE_MB`, `MAX_RENDER_PIXELS` | 128, 512 MiB, 16 million pixels |
| `SPLIT_RENDER_DPI`, `JOB_TIMEOUT` | 150 DPI, 1800 seconds |
| `SPLIT_MAX_ATTEMPTS`, `RETRY_SECONDS` | 3; exponential backoff starting at 30 seconds |
| `SPLIT_HEARTBEAT_TIMEOUT`, `STARTUP_TIMEOUT` | 60 seconds, 1800 seconds |
| `SPLIT_IMAGE_REVISION` | Baked from the application Git commit during image build |

`STATE` and `ARCHIVE` must be genuinely local durable storage. On Linux, state,
archive and handoff accept
ext4/xfs/btrfs/zfs/overlay only, not NFS/CIFS/FUSE or tmpfs. **Consume and staging
must be siblings under ONE narrow parent bind mount**, outside each other's trees.
Matching `st_dev` alone is insufficient: mount IDs are checked to reject separate
bind mounts that return `EXDEV`. Runtime uses Linux `renameat2(RENAME_NOREPLACE)`
(macOS tests use `renamex_np(RENAME_EXCL)`), never overwrite-style fallback.
The archive also needs this exclusive rename when sealing `source.pending` as
`source.pdf`, before a job is queued. A user-share archive can therefore fail
claiming even when state and handoff are on supported storage. Claim failures
record an operation name (such as `copy_to_work` or `seal_archive`) and errno,
without logging document text or filenames.
Only final PDFs enter consume; no probe files, JSON, DBs or staging directories.
Keep the handoff on durable local storage with atomic rename and fsync semantics.

Provide compatible numeric UID/GID and narrow host ownership/ACLs. UID 0 is rejected.
Outputs are `0640`; Paperless needs that UID/group's read access. Archives become
`0440` and are never modified by the application. This is application-level
immutability, **not WORM protection from a malicious same-UID operator**.
State/archive/review/work/models should not be writable by the scanner or other
untrusted users. Only the scanner inbox accepts external uploads. Configure model
egress during cold startup; remove egress after warming if desired.

## Scanner upload contract

The watcher scans immediately at startup and polls thereafter, including on
network-backed inbox shares. Temporary `.part`, `.partial`, `.tmp`, `.upload`,
hidden files and `.done` markers are ignored.

Preferred: write a temporary name, close/fsync it, then rename within the inbox to
`name.pdf`; select `SPLIT_COMPLETION_MODE=atomic`. Alternatively, select `marker`,
remove any previous `name.pdf.done` **before** reusing a name, close the entire PDF,
then create `name.pdf.done` last. Marker mode also waits for configured settling.
Default `settle` requires repeated stable size/mtime/ctime observations but cannot
prove upload completion. Never keep writing after the final rename/marker.

Before claim, regular-file/readability checks reject symlinks, directories and
unsupported extensions. Copying checks descriptor/path identity, timestamps and
size, then SHA-256 verifies the retained source before publishing. Subsequent source
mutation quarantines the job. Inbox originals are retained by default. Enable
`SPLIT_DELETE_COMPLETED_INPUTS=true` to remove successfully delivered inbox copies;
the immutable archived original and deduplication history are always retained.

Sources are identified by SHA-256. Different filenames with the same bytes are
suppressed durably, even if Paperless has already removed every output.
`archive/<source-sha>/source.pdf` retains the original; `<job-id>.json` contains
page scores/ranges, model/config/image revisions, output hashes, progress and audit
events. Review contains JSON records referencing that archive or, for pre-claim
errors, the untouched inbox file. **No failed PDF is sent to Paperless.**

PDF exports use pikepdf page copying, not image reconstruction. Ordered,
non-overlapping ranges cover every retained page exactly once. Each output is
reopened, page count/geometry checked and rendered page-by-page against the original
before any publication begins. One-document results are valid and publish one PDF.
PDF forms/annotations whose exported appearance differs are quarantined rather
than silently losing visible content.

## Blank duplex pages and inbox cleanup

`SPLIT_REMOVE_BLANK_PAGES=true` filters confidently blank pages **before** boundary
inference, so a blank duplex back cannot become a standalone document. Both
embedded text/local OCR and the rendered image are checked: any recognized text
keeps a page, as do dark backgrounds, meaningful marks, faint strokes or borders.
Near-white paper with no text and at most 0.001% pixels more than four gray levels
darker than its median background is considered blank. There is no edge cropping
or destructive denoising. This deliberately conservative heuristic can retain
shadowed/noisy backs; inspect representative scans before relying on it.

Manifests record `blank_pages`, `retained_pages`, original-page `ranges`, each
output's `source_pages`, and `null` scores for excluded pages. For example, a
range `(2, 4)` with page 3 blank exports original pages 2 and 4, unchanged. Page
limits still apply to the entire input before filtering. An all-blank scan is
marked completed with a `blank_only` audit event and no output files.

With `SPLIT_DELETE_COMPLETED_INPUTS=true`, completed inbox copies (including prior
completed jobs found on startup and newly claimed duplicates) are removed after
**every** output has a durable publication acknowledgment. All-blank scans are
also removed after archiving, without sending anything to Paperless. Cleanup does
not wait for Paperless ingestion or use consume-file presence as delivery proof.
Dry-run mode, review jobs, failed/partial/ambiguous publication and active
reprocessing preserve their inbox inputs. Neither enabling cleanup nor blank
filtering reprocesses completed jobs or removes previously ingested Paperless documents.

Cleanup verifies the archive and the exact input identity/hash, records a private
temporary directory beneath the inbox, then detaches the source there and
re-verifies it before deletion. It never unlinks a reused public inbox filename.
This separate inbox operation uses a normal rename within its filesystem;
archive/handoff exclusive-rename requirements are unchanged. The runtime needs
write/execute access to the inbox. Crashes resume the recorded cleanup on startup.
Do not remove `.doc-splitter-cleanup-*` directories manually: an unexpectedly
changed file may be retained there for recovery. `review/cleanup-*.json` records
the location/reason; cleanup errors never roll back successful delivery. I/O
failures retry once on the next restart, not on every poll. Completion `.done`
markers are not deleted; the scanner must remove stale markers before name reuse.

There is no archive purge. To administratively reprocess a cleaned-up source,
restore a **copy** of its archived `source.pdf` to its recorded inbox name first,
then explicitly accept duplicate-ingestion risk with `reprocess`.

## Durable publication and recovery

SQLite WAL uses `synchronous=FULL`; a nonblocking OS lock permits only one worker or
administrator on a state directory. An isolated analyzer process bounds native
inference/OCR time, has a parent-death watchdog, and serializes access with a second
lock. SIGTERM interrupts analysis safely. A worker heartbeat watchdog exits a hung
process so Docker's restart policy can recover; Docker health alone does not restart
containers. Startup/model readiness is distinct from worker liveness.

1. Copy/hash/archive the original and commit the source job.
2. Persist the attempt **before** inference; build and validate all PDFs privately.
   Commit the proposal and complete output list before publishing any.
3. For each output, commit a `publication_intent`, perform the exclusive atomic rename,
   fsync destination and staging directories, then commit `published`.
4. On restart, never regenerate `published` outputs just because consume is empty.
   For an unresolved intent, acknowledge automatically **only** when staging is absent
   and the final destination exists with the expected hash. Otherwise quarantine
   the entire remaining job for reconciliation.

The rename and database commit are **not one transaction**. If Paperless immediately
takes a renamed PDF before the published acknowledgment is recorded, delivery is
ambiguous. Even a crash after intent but before rename is treated conservatively.
Quarantine may leave earlier outputs already ingested; no automatic rollback,
duplicate delivery, or unsplit fallback is attempted. This protocol does **not**
claim exact-once Paperless ingestion. Back up state and archives together; restoring
an old ledger without reconciling consumer history can duplicate deliveries.

Technical analysis failures have bounded retries/backoff; invalid PDF, password/
encryption, unsupported size/page count, mutation and structural errors go directly
to review.

**Restarting automatically retries recoverable inbox claim failures**, including
storage/permission errors and interrupted copies. The existing PDF stays exactly
where it is: no deletion, re-upload, rename or `retry-input` command is needed after
fixing storage. Each restart schedules at most one new claim attempt per failed
input, subject to the usual settling/marker contract and a durable
`SPLIT_MAX_ATTEMPTS` budget (default 3 total attempts). Persistent failures do not
loop on every poll or gain an unlimited budget across restarts. This claim budget
is separate from the job's analysis attempt counter. A successful claim
removes its stale input-review record; retry events remain in the ledger.

Already claimed batches, completed outputs, deliberate review/dry-run decisions,
permanent input failures and ambiguous deliveries are **not** automatically
reprocessed. Existing schema-v1 `claim I/O failure` review records are recognized
on upgrade, so scans rejected by older images are retried too. Keep both the state
and review directories during the upgrade. The current ledger migrates to schema 3 in
place, preserving jobs/hashes/output progress; older schema-1/2 images cannot open
the upgraded database. Back up the stopped state directory before upgrading and
do not restore a stale backup after new deliveries without reconciling them.

An exhausted attempt budget remains in review. `retry-input` is still available
as an explicit reset after an operator resolves a persistent failure; it never
requires deleting the PDF. Stop the worker before administration:

```sh
doc-splitter check
doc-splitter list
doc-splitter manifest JOB_ID
doc-splitter retry-input BASENAME.pdf
doc-splitter ack-delivered JOB_ID OUTPUT_NAME.pdf --confirmed-in-consumer
doc-splitter reprocess SOURCE_SHA256 --accept-duplicate-risk
```

`ack-delivered` requires independently verified Paperless/consumer delivery; it
preserves other outputs' progress. Unknown delivery must stay quarantined.
`reprocess` creates an audited generation suffix and intentionally new output names;
it can duplicate already delivered documents and requires the exact source at its
recorded inbox name. Use it after review/dry-run or a chosen model/config change.
Automatic restarts never do administrative reprocessing. Review decisions are
recorded locally; document text is not logged.

`SPLIT_DRY_RUN=true` writes the proposed manifest and no PDFs. Changing to normal
mode does not bypass duplicate suppression: explicitly reprocess selected batches.

## Development and image publication

```sh
uv sync --locked
uv run ruff check src tests
uv run pytest -q -m "not model"
# Existing persistent real snapshot:
TEST_MODEL_CACHE=/absolute/cache uv run pytest -q -s tests/test_real_model.py
docker build --build-arg VCS_REF="$(git rev-parse HEAD)" -t doc-splitter:acceptance .
uv run python -m tests.container_acceptance
```

The last command creates **isolated, uniquely named temporary Docker volumes**,
downloads the real model, checks German/English OCR, processes 1/4/41/128 pages,
rejects 129 pages, simulates consumer removal, restarts with `--network none`, and
kills/restarts the actual worker during inference. It also starts from a schema-v1
failed inbox claim and verifies automatic migration/retry of the untouched PDF.
Duplex/all-blank fixtures verify blank filtering, archive retention and no empty
handoff. Enabling cleanup on restart removes previous completed inbox copies
without regenerating consumer-removed outputs.
It removes only its own test
container/volume afterward. Fixtures are explicitly synthetic and non-sensitive.

GitHub Actions runs lint/tests and cold-cache real-container acceptance before
publishing `ghcr.io/nabbl/doc-splitter:sha-<full-commit>` for amd64/arm64. Action/base
references are digest/commit-pinned; publication includes provenance/SBOM and
records the deployable digest. Production should pin **that digest**. A source SHA
tag is traceable but remains registry-retaggable; it is not itself immutable storage.
No registry credentials are committed. OS package metadata and transitive notices
remain in the image; Python package versions/hashes are locked.

See [third-party licensing](THIRD_PARTY.md) for the model/example-code distinction.

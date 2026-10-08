# Acceptance evidence

Measured on **2026-10-08**, using synthetic non-sensitive documents only. These
tests establish processing/delivery mechanics, **not boundary accuracy on household
mail** and not equivalence to commercial v2.

## Automated tests

`uv run pytest -q -m "not model"`: **61 passed**. Coverage includes:

- Single/multiple page ranges, all 41 and 128 pages processed; 129 pages explicitly
  rejected without partial publication.
- Image-only local OCR, reused text without OCR, corrupt/empty/encrypted PDFs,
  exact render comparisons, rotation, dimensions/crop boxes and full coverage.
- Slow writes/markers, temporary uploads, duplicate content, unsupported input,
  symlinks/overlapping roots, mutation during copying and after claim.
- Exclusive rename collisions, permission errors, ENOSPC and EXDEV handoff failures,
  bounded retries, config changes, interrupted claims/inference.
- Actual spawned processes killed with **SIGKILL** before intent, after intent,
  after rename and after acknowledgment, including an immediately removing
  simulated consumer. Missing acknowledged outputs are never regenerated;
  ambiguous deliveries enter review.
- Explicit reprocess/reconciliation, interrupted model download/checksum recovery,
  invalid configurations and warm-cache no-network calls.

Lint/format checks use the pinned Ruff version. Local real-model tests also passed
for 1, 4 and 41 pages, an image-only scan, and startup with network calls forbidden.
Model revision: `dccadc69dd4ae69c63f42aa6715f249c6e5e6cf8`.

## Actual Linux container

The isolated acceptance runner built and exercised the real image on OrbStack
Linux **arm64**, with **2 CPU / 4 GiB** limits, read-only root filesystem, non-root
UID/GID 10001, dropped capabilities and no-new-privileges. Host runtime reported
16 CPUs and about 16 GiB available to its VM; these are **not production resources**.
German and English Tesseract data were present.

A cold-cache run automatically downloaded/verified the real model and processed
synthetic 1/4/41/128-page plus image-only PDFs; a 129-page PDF was quarantined.

| Measurement | Observed |
| --- | --- |
| Healthy after cold download/warmup | 39.214 seconds |
| Initial six fixtures complete, including rejected fixture | 252.151 seconds |
| Successfully processed pages | 175 |
| Cgroup peak memory | 3,137,277,952 bytes (about 2.92 GiB) |
| Post-processing resident/container accounting | 2.051 GiB of 4 GiB |
| Cgroup CPU time through initial fixtures | 439.800 seconds |
| Network during cold cache fill | Approximately 954 MB |

After simulated consumption of every published file, the container restarted using
the **same volume with `--network none`**. It became healthy, retained publication
records and produced no duplicate outputs. Then the actual worker was SIGKILLed
during a new 41-page inference and restarted offline; the batch completed on
attempt 2. The restarted batch's recorded processing time was 49.834 seconds.
Only the runner's own uniquely named synthetic test containers/volumes were removed.

After final startup-layout hardening, the complete suite passed again with a
prepopulated real cache: healthy in **6.945 seconds**, all initial fixtures in
**217.722 seconds**, peak cgroup memory **2,309,214,208 bytes (about 2.15 GiB)**.
Offline restart, no regeneration of consumed outputs, and inference SIGKILL
recovery all passed again. The local tested image ID was
`sha256:79f92a01f2852083ebbc698017aaa104739557cadefc5439366c1eefae19e979`
(development OCI revision). A rebuild with the final Git revision changes image
metadata/ID; a published multi-architecture registry digest is also a distinct ID.

The final separate native real-model suite passed **5 tests**. Its deliberately
merged four-page synthetic invoice/letter fixture was predicted as **one document**
at threshold 0.5, missing the constructed page-three boundary. This is recorded
rather than hidden by the mechanics assertions: the fixture is not a household-mail
accuracy benchmark, and real-model success does not imply correct boundaries.

## Deployment boundary

The GitOps Compose validates and fails closed when any of its ten required
deployment variables is missing. No production Arcane sync, host runtime mount/
permission verification, scanner settings or actual Paperless ingestion was
performed. The user explicitly took ownership of those checks. See
[deployment handoff](deployment.md) for required evidence and rollback.

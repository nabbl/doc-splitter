# Operator deployment handoff

The user explicitly chose to perform live deployment verification. The service
must not be described as live until that verification is complete.

## Discovered, not inferred

- Application repository: `https://github.com/nabbl/doc-splitter`.
- Existing local GitOps repository: `nabbl/gitops-arcane`. Initial API/clone/fetch
  attempts used an unrelated injected GitHub identity and failed. Repository
  access was restored using the owner's existing keyring account scoped to the
  relevant commands, without switching global authentication.
- Existing conventions: one project directory with `compose.yaml`, private
  Arcane environment or `.env`, `.env.example`, restart policy, host binds and GHCR
  images. No build workflow existed for this new application; one is provided here.
- Recorded Paperless consume mapping is documented in the existing private
  GitOps checkout. Host topology is deliberately not copied into this public
  application repository. Export values are not freshly verified live mappings.
- No authorized host shell or Arcane management endpoint was used.
- Exact new scanner destination, runtime UID/GID, available production resources,
  dedicated local handoff topology, Arcane project ID and synced commit are unknown.
  They are deliberately not fabricated.

The existing consume mapping uses Unraid user-share FUSE semantics. Its correct
narrow physical backing directory has not been established; do not guess a safe
parent bind from a shared pathname prefix.
The application fails closed for FUSE handoff/state/archive. Operator must first identify
the actual physical local pool/filesystem and explicitly approve a dedicated narrow
handoff layout. Do not automatically move existing documents or change Paperless.

## GitOps project

The scoped integration is `doc-splitter/{compose.yaml,.env.example,README.md}` plus
the project inventory entry in the existing GitOps checkout. Paperless and other
services are untouched. Populate:

| Host configuration | Service path |
| --- | --- |
| `DOC_SPLITTER_INBOX_HOST` | `/inbox` |
| `DOC_SPLITTER_HANDOFF_HOST/consume` | `/handoff/consume` |
| `DOC_SPLITTER_HANDOFF_HOST/staging` | `/handoff/staging` |
| `DOC_SPLITTER_ARCHIVE_HOST` | `/archive` |
| `DOC_SPLITTER_REVIEW_HOST` | `/review` |
| `DOC_SPLITTER_WORK_HOST` | `/work` |
| `DOC_SPLITTER_STATE_HOST` | `/state` |
| `DOC_SPLITTER_MODELS_HOST` | `/models` |

All host values and numeric `DOC_SPLITTER_UID/GID` must be supplied from verified
host facts. Handoff is one parent bind, not two binds. No directories are implicitly
created by Compose. Image must be the successful build digest; default resource
budgets are examples (4 GiB/two CPUs), not production capacity measurements.

## Required operator evidence

1. Restore GitOps remote access; commit/push the scoped integration using normal
   repository practice. Record the application commit and successful image
   workflow's digest. Verify the running image OCI revision matches it.
2. Record the actual host directory ownership/ACLs, worker UID/GID, filesystem
   types and mount IDs. Confirm physical handoff paths and Paperless visibility.
   Confirm enough disk for retained originals, cache, private outputs and ledger.
3. Configure Arcane's existing Git sync to the integration path/ref. Record project
   identity, successful sync and exact synced commit. Do not deploy a separate stack.
4. First use an isolated, unwatched consume directory; submit clearly labelled
   synthetic 1/4/41-page and scan fixtures. Check outputs/manifests/health and record
   timings, CPU/memory, model revision/cache, termination recovery and offline restart.
5. Only after explicit topology approval, send one harmless synthetic document
   through the real inbox. Confirm arrival in consume and ingestion in Paperless;
   record the exact verification boundary if ingestion cannot be queried.
6. Record the **new scanner destination** as the chosen `DOC_SPLITTER_INBOX_HOST`
   and its scanner share mapping. Do not change scanner settings until checks pass.

## Rollback

Stop only the preprocessor through Arcane. Revert its GitOps image/config commit
to the previously verified image (or leave this newly added service stopped).
Preserve state including SQLite/WAL, originals, review, staging, work and model
cache. Reconcile incomplete publication intents before restart; missing output
files do not establish non-delivery. Never reroute failed/accumulated batches to
Paperless unsplit. Paperless itself and real ingested documents remain unchanged.

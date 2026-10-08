import errno
import hashlib
import json
import logging
import os
import stat
import time
import uuid

from .cleanup import InputCleanup
from .config import Config
from .fs import (
    AmbiguousDelivery,
    UnsafeInput,
    atomic_json,
    copy_source,
    fingerprint,
    regular_fd,
    rename_noreplace,
    sha256,
    sync_dir,
)
from .ledger import Ledger
from .pdf import blank_only, validate_ranges

LOG = logging.getLogger(__name__)
TERMINAL = ("completed", "review", "dry_run")


class Worker:
    def __init__(self, config: Config, ledger: Ledger, analyzer, stopped=lambda: False):
        self.config = config
        self.ledger = ledger
        self.analyzer = analyzer
        self.stopped = stopped
        self.input_cleanup = InputCleanup(config, ledger)

    def source(self, job):
        return self.config.archive / job["source_hash"] / "source.pdf"

    def write_manifest(self, job_id):
        manifest = self.ledger.manifest(job_id)
        folder = self.config.archive / manifest["source_hash"]
        atomic_json(folder / (job_id + ".json"), manifest)
        if manifest["status"] == "review":
            atomic_json(self.config.review / (job_id + ".json"), manifest)
        else:
            (self.config.review / (job_id + ".json")).unlink(missing_ok=True)

    def quarantine(self, job_id, reason):
        self.ledger.transition(job_id, "review", reason)
        self.write_manifest(job_id)
        LOG.error("job=%s transition=review reason=%s", job_id, reason)

    def source_unchanged(self, job):
        source = self.config.inbox / job["source_name"]
        if fingerprint(source.lstat()) != tuple(json.loads(job["source_signature"])):
            raise UnsafeInput("inbox source changed after claim")
        if sha256(source) != job["source_hash"] or sha256(self.source(job)) != job["source_hash"]:
            raise UnsafeInput("inbox or immutable archive checksum changed")

    def input_review(self, name, signature):
        identifier = hashlib.sha256((name + signature).encode()).hexdigest()
        return identifier, self.config.review / ("input-" + identifier + ".json")

    def scan(self, now=None):
        now = time.time() if now is None else now
        for path in sorted(self.config.inbox.iterdir()):
            if self.stopped():
                return
            if path.name.startswith(".") or path.suffix.lower() in {
                ".part",
                ".partial",
                ".tmp",
                ".upload",
                ".done",
            }:
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            signature = json.dumps(fingerprint(info))
            observation = self.ledger.db.execute(
                "SELECT * FROM observations WHERE name=?", (path.name,)
            ).fetchone()
            if observation is None or observation["signature"] != signature:
                with self.ledger.db:
                    self.ledger.db.execute(
                        "INSERT OR REPLACE INTO observations(name,signature,since,handled) "
                        "VALUES(?,?,?,0)",
                        (path.name, signature, now),
                    )
                since = now
            else:
                if observation["handled"]:
                    continue
                since = observation["since"]
            if self.config.completion_mode != "atomic" and now - since < self.config.settle_seconds:
                continue
            if self.config.completion_mode == "marker":
                marker = path.with_name(path.name + ".done")
                if not marker.exists() or marker.is_symlink() or not marker.is_file():
                    continue
            temporary = self.config.work / ("claim-" + uuid.uuid4().hex + ".pdf")
            with self.ledger.db:
                self.ledger.db.execute(
                    "UPDATE observations SET handled=2,attempts=attempts+1,retryable=0 "
                    "WHERE name=? AND signature=?",
                    (path.name, signature),
                )
            attempts = self.ledger.db.execute(
                "SELECT attempts FROM observations WHERE name=?", (path.name,)
            ).fetchone()["attempts"]
            identifier, review_path = self.input_review(path.name, signature)
            operation = "validate_input"
            claim_committed = False
            try:
                if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                    raise UnsafeInput("inbox entry is not a regular non-symlink file")
                if path.suffix.lower() != ".pdf":
                    raise UnsafeInput("unsupported input extension; only PDF is accepted")
                operation = "copy_to_work"
                digest, copied_signature = copy_source(
                    path, temporary, self.config.max_source_mb * 1024 * 1024
                )
                operation = "create_archive_directory"
                folder = self.config.archive / digest
                folder.mkdir(mode=0o700, exist_ok=True)
                if folder.is_symlink():
                    raise UnsafeInput("archive directory is a symlink")
                archived = folder / "source.pdf"
                if archived.exists():
                    operation = "verify_archive"
                    if sha256(archived) != digest:
                        raise UnsafeInput("immutable archive checksum mismatch")
                else:
                    operation = "copy_to_archive"
                    pending = folder / "source.pending"
                    pending.unlink(missing_ok=True)
                    copied_hash, _ = copy_source(
                        temporary, pending, self.config.max_source_mb * 1024 * 1024
                    )
                    if copied_hash != digest:
                        raise UnsafeInput("archive copy checksum mismatch")
                    operation = "protect_archive"
                    pending.chmod(0o440)
                    operation = "seal_archive"
                    rename_noreplace(pending, archived)
                    operation = "sync_archive"
                    sync_dir(self.config.archive)
                with self.ledger.db:
                    existing = self.ledger.db.execute(
                        "SELECT id FROM jobs WHERE source_hash=? LIMIT 1", (digest,)
                    ).fetchone()
                    if existing is None:
                        self.ledger.db.execute(
                            """INSERT INTO jobs(
                            id,source_hash,generation,source_name,source_signature,
                            config_revision,config,created,updated) VALUES(?,?,0,?,?,?,?,?,?)""",
                            (
                                digest,
                                digest,
                                path.name,
                                json.dumps(copied_signature),
                                self.config.revision(),
                                json.dumps(self.config.manifest_config()),
                                now,
                                now,
                            ),
                        )
                        self.ledger.event(digest, "queued")
                    self.ledger.db.execute(
                        "UPDATE observations SET handled=1 WHERE name=? AND signature=?",
                        (path.name, signature),
                    )
                    self.ledger.register_input(path.name, json.dumps(copied_signature), digest)
                    if attempts > 1:
                        self.ledger.event(identifier, "claim_recovered", digest)
                claim_committed = True
                if existing is None:
                    operation = "write_manifest"
                    self.write_manifest(digest)
                    LOG.info("job=%s transition=queued", digest)
                else:
                    LOG.info("job=%s duplicate_suppressed=true", existing["id"])
                operation = "resolve_input_review"
                review_path.unlink(missing_ok=True)
                sync_dir(self.config.review)
            except (OSError, UnsafeInput) as error:
                retryable = isinstance(error, OSError) and not claim_committed
                if retryable and attempts < self.config.max_attempts:
                    hint = "fix storage and restart; startup will retry this input"
                else:
                    hint = "fix storage and use retry-input if another attempt is appropriate"
                if operation == "seal_archive" and getattr(error, "errno", None) in {
                    errno.EINVAL,
                    errno.ENOTSUP,
                    errno.ENOSYS,
                }:
                    hint = (
                        "check archive exclusive-rename support; use physical local archive "
                        f"storage; {hint}"
                    )
                reason = (
                    str(error)
                    if isinstance(error, UnsafeInput)
                    else f"claim I/O failure operation={operation} errno={error.errno}; {hint}"
                )
                atomic_json(
                    review_path,
                    {
                        "status": "review",
                        "source_name": path.name,
                        "signature": signature,
                        "reason": reason,
                        "operation": operation,
                        "attempts": attempts,
                        "retryable": retryable,
                        "original_retained_in_inbox": True,
                    },
                )
                with self.ledger.db:
                    self.ledger.db.execute(
                        "UPDATE observations SET handled=1,retryable=? "
                        "WHERE name=? AND signature=?",
                        (retryable, path.name, signature),
                    )
                    self.ledger.event(identifier, "claim_failed", reason)
                LOG.error("input=%s transition=review reason=%s", identifier, reason)
            finally:
                temporary.unlink(missing_ok=True)

    def recover_inputs(self):
        observations = self.ledger.db.execute(
            "SELECT * FROM observations WHERE handled=2 "
            "OR (handled=1 AND (attempts=0 OR retryable=1))"
        ).fetchall()
        for observation in observations:
            identifier, review_path = self.input_review(
                observation["name"], observation["signature"]
            )
            attempts = max(1, observation["attempts"])
            retryable = bool(observation["retryable"])
            if observation["handled"] == 2:
                retryable = True
                atomic_json(
                    review_path,
                    {
                        "status": "review",
                        "source_name": observation["name"],
                        "signature": observation["signature"],
                        "reason": "claim interrupted; original retained in inbox",
                        "attempts": attempts,
                        "retryable": True,
                        "original_retained_in_inbox": True,
                    },
                )
                LOG.error("input=%s transition=review reason=interrupted_claim", identifier)
            elif observation["attempts"] == 0:
                # Schema v1 used handled=1 for both successful claims and all failures.
                try:
                    with os.fdopen(regular_fd(review_path), "r") as stream:
                        report = json.load(stream)
                    reason = report.get("reason", "") if isinstance(report, dict) else ""
                    retryable = (
                        isinstance(report, dict)
                        and report.get("status") == "review"
                        and report.get("source_name") == observation["name"]
                        and report.get("signature", observation["signature"])
                        == observation["signature"]
                        and isinstance(reason, str)
                        and reason.startswith(("claim I/O failure", "claim interrupted;"))
                    )
                except FileNotFoundError:
                    retryable = False
                except (OSError, ValueError, UnsafeInput) as error:
                    LOG.error(
                        "input=%s legacy_review_unreadable type=%s; retry-input may be required",
                        identifier,
                        type(error).__name__,
                    )
                    continue
            accepted = self.ledger.db.execute(
                "SELECT 1 FROM jobs WHERE source_name=? AND source_signature=? LIMIT 1",
                (observation["name"], observation["signature"]),
            ).fetchone()
            if accepted:
                retryable = False
            retry = retryable and attempts < self.config.max_attempts
            with self.ledger.db:
                self.ledger.db.execute(
                    "UPDATE observations SET handled=?,attempts=?,retryable=?,since=? WHERE name=?",
                    (
                        0 if retry else 1,
                        attempts,
                        retryable,
                        time.time() if retry else observation["since"],
                        observation["name"],
                    ),
                )
                if retry:
                    self.ledger.event(identifier, "startup_retry", f"attempt={attempts + 1}")
            if retry:
                LOG.info(
                    "input=%s transition=startup_retry next_attempt=%s",
                    identifier,
                    attempts + 1,
                )
            elif retryable:
                LOG.error(
                    "input=%s transition=review reason=claim_retry_budget_exhausted attempts=%s",
                    identifier,
                    attempts,
                )

    def recover(self):
        self.recover_inputs()
        jobs = self.ledger.db.execute(
            "SELECT * FROM jobs WHERE status NOT IN ('completed','review','dry_run')"
        ).fetchall()
        for job in jobs:
            for output in self.ledger.outputs(job["id"]):
                if output["status"] != "intent":
                    continue
                target = self.config.consume / output["name"]
                stage = self.config.staging / output["name"]
                if (
                    not stage.exists()
                    and not stage.is_symlink()
                    and target.is_file()
                    and not target.is_symlink()
                ):
                    if sha256(target) == output["sha256"]:
                        self.record_published(job["id"], output["name"])
                        continue
                self.quarantine(
                    job["id"],
                    "ambiguous delivery after intent; reconcile against consumer history",
                )
                break
            if self.ledger.job(job["id"])["status"] != "review":
                self.write_manifest(job["id"])
        for job in self.ledger.db.execute(
            "SELECT id FROM jobs WHERE status IN ('completed','review','dry_run')"
        ).fetchall():
            self.write_manifest(job["id"])
        self.cleanup_completed_inputs()

    def cleanup_completed_inputs(self):
        for job_id in self.input_cleanup.run():
            self.write_manifest(job_id)

    def record_published(self, job_id, name):
        with self.ledger.db:
            self.ledger.db.execute(
                "UPDATE outputs SET status='published' WHERE job_id=? AND name=?", (job_id, name)
            )
            self.ledger.event(job_id, "published", name)

    def publish(self, job_id, checkpoint=lambda event, output: None):
        job = self.ledger.job(job_id)
        outputs = self.ledger.outputs(job_id)
        proposal = json.loads(job["proposal"])
        validate_ranges(
            [(o["start"], o["end"]) for o in outputs],
            proposal["page_count"],
            proposal.get("blank_pages", []),
        )
        if all(output["status"] == "published" for output in outputs):
            self.ledger.transition(job_id, "completed")
            self.write_manifest(job_id)
            return
        self.source_unchanged(job)
        for output in outputs:
            if self.stopped():
                return
            if output["status"] == "published":
                continue
            if output["status"] != "ready":
                raise AmbiguousDelivery("unreconciled publication intent")
            # Keep the source intact until every output has a durable acknowledgment.
            path = self.config.inbox / job["source_name"]
            if fingerprint(path.lstat()) != tuple(json.loads(job["source_signature"])):
                raise UnsafeInput("inbox source changed during publication")
            stage = self.config.staging / output["name"]
            target = self.config.consume / output["name"]
            if sha256(stage) != output["sha256"]:
                raise UnsafeInput("staged PDF checksum mismatch")
            checkpoint("before_intent", output)
            with self.ledger.db:
                self.ledger.db.execute(
                    "UPDATE outputs SET status='intent' WHERE job_id=? AND name=?",
                    (job_id, output["name"]),
                )
                self.ledger.event(job_id, "publication_intent", output["name"])
            checkpoint("after_intent", output)
            try:
                rename_noreplace(stage, target)
            except OSError as error:
                raise AmbiguousDelivery(
                    f"publication failed errno={error.errno}; reconcile before retry"
                ) from error
            checkpoint("after_rename", output)
            self.record_published(job_id, output["name"])
            checkpoint("after_record", output)
            self.write_manifest(job_id)
        self.ledger.transition(job_id, "completed")
        self.write_manifest(job_id)
        LOG.info("job=%s transition=completed", job_id)

    def process(self, job_id):
        job = self.ledger.job(job_id)
        if job["status"] in TERMINAL or job["next_attempt"] > time.time():
            return
        started = time.monotonic()
        try:
            if job["config_revision"] != self.config.revision():
                raise UnsafeInput("configuration changed; review and explicitly reprocess")
            if job["status"] != "prepared":
                if job["attempts"] >= self.config.max_attempts:
                    raise UnsafeInput("analysis retry budget exhausted")
                self.source_unchanged(job)
                # Only uncommitted preparation is rebuilt. Never regenerate published parts.
                for stale in self.config.staging.glob(job_id + "-p*.pdf"):
                    stale.unlink()
                with self.ledger.db:
                    self.ledger.db.execute(
                        "UPDATE jobs SET attempts=attempts+1,status='preparing',updated=? "
                        "WHERE id=?",
                        (time.time(), job_id),
                    )
                    self.ledger.event(job_id, "preparing")
                proposal = self.analyzer.analyze(self.source(job), job_id)
                validate_ranges(
                    proposal["ranges"], proposal["page_count"], proposal.get("blank_pages", [])
                )
                self.source_unchanged(job)
                status = (
                    "dry_run"
                    if self.config.dry_run
                    else "review"
                    if proposal["review_required"]
                    else "completed"
                    if blank_only(proposal)
                    else "prepared"
                )
                with self.ledger.db:
                    self.ledger.db.execute(
                        "UPDATE jobs SET proposal=?,status=?,error=NULL,updated=? WHERE id=?",
                        (json.dumps(proposal), status, time.time(), job_id),
                    )
                    for output in proposal["outputs"]:
                        self.ledger.db.execute(
                            """INSERT INTO outputs(job_id,name,start,end,sha256)
                            VALUES(?,?,?,?,?)""",
                            (
                                job_id,
                                output["name"],
                                output["start"],
                                output["end"],
                                output["sha256"],
                            ),
                        )
                    self.ledger.event(job_id, status)
                    if blank_only(proposal):
                        self.ledger.event(job_id, "blank_only", "no nonblank pages to publish")
                self.write_manifest(job_id)
                LOG.info(
                    "job=%s pages=%s blank_pages=%s ranges=%s transition=%s analysis_seconds=%.3f",
                    job_id,
                    proposal["page_count"],
                    proposal.get("blank_pages", []),
                    proposal["ranges"],
                    status,
                    time.monotonic() - started,
                )
                if status in TERMINAL:
                    return
            self.publish(job_id)
        except InterruptedError:
            LOG.info("job=%s interrupted=true", job_id)
        except (UnsafeInput, AmbiguousDelivery) as error:
            self.quarantine(job_id, str(error))
        except (OSError, RuntimeError, TimeoutError) as error:
            reason = f"{type(error).__name__}; errno={getattr(error, 'errno', None)}"
            row = self.ledger.job(job_id)
            if any(o["status"] == "intent" for o in self.ledger.outputs(job_id)):
                self.quarantine(job_id, "I/O failure with publication intent; reconcile delivery")
            elif row["status"] == "prepared" or row["attempts"] >= self.config.max_attempts:
                self.quarantine(job_id, reason)
            else:
                with self.ledger.db:
                    self.ledger.db.execute(
                        "UPDATE jobs SET next_attempt=?,error=?,updated=? WHERE id=?",
                        (
                            time.time()
                            + self.config.retry_seconds * 2 ** max(0, row["attempts"] - 1),
                            reason,
                            time.time(),
                            job_id,
                        ),
                    )
                    self.ledger.event(job_id, "retry", reason)
                self.write_manifest(job_id)
                LOG.error("job=%s transition=retry reason=%s", job_id, reason)

    def cycle(self):
        self.scan()
        jobs = self.ledger.db.execute(
            """SELECT id FROM jobs WHERE status NOT IN ('completed','review','dry_run')
            AND next_attempt<=? ORDER BY created""",
            (time.time(),),
        ).fetchall()
        for job in jobs:
            if self.stopped():
                break
            self.process(job["id"])
        if not self.stopped():
            self.cleanup_completed_inputs()

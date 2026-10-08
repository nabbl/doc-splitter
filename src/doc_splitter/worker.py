import hashlib
import json
import logging
import stat
import time
import uuid

from .config import Config
from .fs import (
    AmbiguousDelivery,
    UnsafeInput,
    atomic_json,
    copy_source,
    fingerprint,
    rename_noreplace,
    sha256,
    sync_dir,
)
from .ledger import Ledger
from .pdf import validate_ranges

LOG = logging.getLogger(__name__)
TERMINAL = ("completed", "review", "dry_run")


class Worker:
    def __init__(self, config: Config, ledger: Ledger, analyzer, stopped=lambda: False):
        self.config = config
        self.ledger = ledger
        self.analyzer = analyzer
        self.stopped = stopped

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
                        "INSERT OR REPLACE INTO observations VALUES(?,?,?,0)",
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
                    "UPDATE observations SET handled=2 WHERE name=? AND signature=?",
                    (path.name, signature),
                )
            try:
                if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                    raise UnsafeInput("inbox entry is not a regular non-symlink file")
                if path.suffix.lower() != ".pdf":
                    raise UnsafeInput("unsupported input extension; only PDF is accepted")
                digest, copied_signature = copy_source(
                    path, temporary, self.config.max_source_mb * 1024 * 1024
                )
                folder = self.config.archive / digest
                folder.mkdir(mode=0o700, exist_ok=True)
                if folder.is_symlink():
                    raise UnsafeInput("archive directory is a symlink")
                archived = folder / "source.pdf"
                if archived.exists():
                    if sha256(archived) != digest:
                        raise UnsafeInput("immutable archive checksum mismatch")
                else:
                    pending = folder / "source.pending"
                    pending.unlink(missing_ok=True)
                    copied_hash, _ = copy_source(
                        temporary, pending, self.config.max_source_mb * 1024 * 1024
                    )
                    if copied_hash != digest:
                        raise UnsafeInput("archive copy checksum mismatch")
                    pending.chmod(0o440)
                    rename_noreplace(pending, archived)
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
                if existing is None:
                    self.write_manifest(digest)
                    LOG.info("job=%s transition=queued", digest)
                else:
                    LOG.info("job=%s duplicate_suppressed=true", existing["id"])
            except (OSError, UnsafeInput) as error:
                reason = (
                    str(error)
                    if isinstance(error, UnsafeInput)
                    else (f"claim I/O failure errno={error.errno}; fix storage and retry-input")
                )
                identifier = hashlib.sha256((path.name + signature).encode()).hexdigest()
                atomic_json(
                    self.config.review / ("input-" + identifier + ".json"),
                    {
                        "status": "review",
                        "source_name": path.name,
                        "signature": signature,
                        "reason": reason,
                        "original_retained_in_inbox": True,
                    },
                )
                with self.ledger.db:
                    self.ledger.db.execute(
                        "UPDATE observations SET handled=1 WHERE name=? AND signature=?",
                        (path.name, signature),
                    )
                LOG.error("input=%s transition=review reason=%s", identifier, reason)
            finally:
                temporary.unlink(missing_ok=True)

    def recover(self):
        interrupted = self.ledger.db.execute(
            "SELECT * FROM observations WHERE handled=2"
        ).fetchall()
        for observation in interrupted:
            identifier = hashlib.sha256(
                (observation["name"] + observation["signature"]).encode()
            ).hexdigest()
            atomic_json(
                self.config.review / ("input-" + identifier + ".json"),
                {
                    "status": "review",
                    "source_name": observation["name"],
                    "reason": "claim interrupted; original retained in inbox; use retry-input",
                },
            )
            with self.ledger.db:
                self.ledger.db.execute(
                    "UPDATE observations SET handled=1 WHERE name=?", (observation["name"],)
                )
            LOG.error("input=%s transition=review reason=interrupted_claim", identifier)
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
        validate_ranges([(o["start"], o["end"]) for o in outputs], proposal["page_count"])
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
            # Check source mutation between parts; never remove the inbox file in v1.
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
                validate_ranges(proposal["ranges"], proposal["page_count"])
                self.source_unchanged(job)
                status = (
                    "dry_run"
                    if self.config.dry_run
                    else "review"
                    if proposal["review_required"]
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
                self.write_manifest(job_id)
                LOG.info(
                    "job=%s pages=%s ranges=%s transition=%s analysis_seconds=%.3f",
                    job_id,
                    proposal["page_count"],
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

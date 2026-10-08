import hashlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path

from .fs import UnsafeInput, atomic_json, fingerprint, regular_fd, sha256, sync_dir
from .pdf import blank_only

LOG = logging.getLogger(__name__)


class InputCleanup:
    def __init__(self, config, ledger):
        self.config = config
        self.ledger = ledger
        self.attempted = set()

    def update(self, row, **values):
        with self.ledger.db:
            self.ledger.db.execute(
                "UPDATE input_cleanups SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE source_name=? AND source_signature=?",
                (*values.values(), row["source_name"], row["source_signature"]),
            )

    def verify_source(self, path, expected, digest, detached=False):
        with os.fdopen(regular_fd(path), "rb") as stream:
            before = fingerprint(os.fstat(stream.fileno()))
            # Rename changes ctime, but not identity, size or mtime.
            if (before[:4] if detached else before) != (expected[:4] if detached else expected):
                raise UnsafeInput("inbox source identity changed; file retained")
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if (
                actual != digest
                or fingerprint(os.fstat(stream.fileno())) != before
                or fingerprint(path.lstat()) != before
            ):
                raise UnsafeInput("inbox source changed during cleanup; file retained")

    def remove(self, row, checkpoint):
        source = self.config.inbox / row["source_name"]
        expected = tuple(json.loads(row["source_signature"]))
        archive = self.config.archive / row["source_hash"] / "source.pdf"
        if sha256(archive) != row["source_hash"]:
            raise UnsafeInput("archive checksum mismatch; inbox source retained")
        directory = row["directory"]
        if directory is None:
            try:
                self.verify_source(source, expected, row["source_hash"])
            except FileNotFoundError:
                return "input_already_absent"
            private = Path(tempfile.mkdtemp(prefix=".doc-splitter-cleanup-", dir=self.config.inbox))
            sync_dir(self.config.inbox)
            self.update(row, directory=private.name)
        else:
            if Path(directory).name != directory or not directory.startswith(
                ".doc-splitter-cleanup-"
            ):
                raise UnsafeInput("invalid recorded cleanup directory; file retained")
            private = self.config.inbox / directory
            if row["status"] == "detached" and not private.exists() and not private.is_symlink():
                sync_dir(self.config.inbox)
                return "input_deleted"
        info = private.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise UnsafeInput("cleanup directory must be private and owned by the worker")
        held = private / "source.pdf"
        if not held.exists() and not held.is_symlink():
            if row["status"] == "detached":
                private.rmdir()
                sync_dir(self.config.inbox)
                return "input_deleted"
            try:
                self.verify_source(source, expected, row["source_hash"])
            except FileNotFoundError:
                private.rmdir()
                sync_dir(self.config.inbox)
                return "input_already_absent"
            # Only cleanup uses plain rename into an exclusively created private directory.
            # Avoid requiring exclusive flags on inbox shares; handoff still uses RENAME_NOREPLACE.
            os.rename(source, held)
            sync_dir(private)
            sync_dir(self.config.inbox)
            checkpoint("after_detach")
        self.verify_source(held, expected, row["source_hash"], detached=True)
        self.update(row, status="detached")
        checkpoint("before_unlink")
        held.unlink()
        sync_dir(private)
        checkpoint("after_unlink")
        private.rmdir()
        sync_dir(self.config.inbox)
        return "input_deleted"

    def run(self, checkpoint=lambda _: None):
        if not self.config.delete_completed_inputs or self.config.dry_run:
            return []
        rows = self.ledger.db.execute(
            """SELECT c.*,j.id AS job_id FROM input_cleanups c JOIN jobs j ON j.id=(
                SELECT id FROM jobs WHERE source_hash=c.source_hash
                ORDER BY generation DESC LIMIT 1)
            WHERE c.status IN ('pending','detached') AND j.status='completed'
            AND NOT EXISTS(SELECT 1 FROM jobs active WHERE active.source_hash=c.source_hash
                AND active.status NOT IN ('completed','review','dry_run'))"""
        ).fetchall()
        changed = []
        for row in rows:
            key = (row["source_name"], row["source_signature"])
            if key in self.attempted:
                continue
            self.attempted.add(key)
            identifier = hashlib.sha256("".join(key).encode()).hexdigest()
            report = self.config.review / ("cleanup-" + identifier + ".json")
            try:
                outputs = self.ledger.outputs(row["job_id"])
                proposal = json.loads(self.ledger.job(row["job_id"])["proposal"])
                if (not outputs and not blank_only(proposal)) or any(
                    output["status"] != "published" for output in outputs
                ):
                    raise UnsafeInput(
                        "completed job lacks fully acknowledged outputs; input retained"
                    )
                event = self.remove(row, checkpoint)
                report.unlink(missing_ok=True)
                sync_dir(self.config.review)
                with self.ledger.db:
                    self.ledger.db.execute(
                        "UPDATE input_cleanups SET status='done' "
                        "WHERE source_name=? AND source_signature=?",
                        key,
                    )
                    self.ledger.event(row["job_id"], event, identifier)
                LOG.info("job=%s input=%s transition=%s", row["job_id"], identifier, event)
            except (OSError, UnsafeInput) as error:
                if isinstance(error, UnsafeInput):
                    self.update(row, status="review")
                    reason = str(error)
                else:
                    reason = f"cleanup I/O failure errno={error.errno}; fix storage and restart"
                current = self.ledger.db.execute(
                    "SELECT directory FROM input_cleanups "
                    "WHERE source_name=? AND source_signature=?",
                    key,
                ).fetchone()
                atomic_json(
                    report,
                    {
                        "status": "review",
                        "job_id": row["job_id"],
                        "source_name": row["source_name"],
                        "reason": reason,
                        "cleanup_directory": current["directory"],
                        "outputs_remain_published": True,
                    },
                )
                with self.ledger.db:
                    self.ledger.event(row["job_id"], "input_cleanup_failed", reason)
                LOG.error(
                    "job=%s input=%s transition=input_cleanup_failed reason=%s",
                    row["job_id"],
                    identifier,
                    reason,
                )
            changed.append(row["job_id"])
        return changed

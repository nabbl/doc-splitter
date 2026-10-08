import argparse
import fcntl
import json
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path

from .config import Config
from .fs import atomic_json, fingerprint, sha256, verify_layout
from .ledger import Ledger
from .runtime import Analyzer
from .worker import Worker

LOG = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, config):
        self.config = config
        self.last_tick = time.monotonic()
        self.last_write = 0
        self.phase = "startup"
        self.phase_started = time.time()
        self.ready = False
        self.closed = threading.Event()

    def tick(self, phase):
        self.last_tick = time.monotonic()
        if phase != self.phase:
            self.phase_started = time.time()
            self.phase = phase
        if self.last_tick - self.last_write >= 2:
            atomic_json(
                self.config.state / "health.json",
                {
                    "pid": os.getpid(),
                    "updated": time.time(),
                    "phase": phase,
                    "phase_started": self.phase_started,
                    "model_ready": self.ready,
                },
            )
            self.last_write = self.last_tick

    def watchdog(self):
        while not self.closed.wait(2):
            if time.monotonic() - self.last_tick > self.config.heartbeat_timeout:
                LOG.critical("worker_heartbeat_expired action=exit_for_restart")
                os._exit(70)


def health(config) -> int:
    try:
        data = json.loads((config.state / "health.json").read_text())
        os.kill(data["pid"], 0)
        if time.time() - data["updated"] > config.heartbeat_timeout:
            print("unhealthy: stale worker heartbeat")
            return 1
        if not data["model_ready"] or data["phase"] in {"startup", "model_startup", "stopping"}:
            print("not ready: " + data["phase"])
            return 1
        print("ready: " + data["phase"])
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("not ready: missing/invalid worker heartbeat")
        return 1


def reprocess(config, ledger, source_hash):
    if not re.fullmatch("[0-9a-f]{64}", source_hash):
        raise ValueError("source hash must be 64 lowercase hex characters")
    prior = ledger.db.execute(
        "SELECT * FROM jobs WHERE source_hash=? ORDER BY generation DESC LIMIT 1", (source_hash,)
    ).fetchone()
    if prior is None or prior["status"] not in {"completed", "review", "dry_run"}:
        raise ValueError("source must have a terminal job before administrative reprocessing")
    source = config.inbox / prior["source_name"]
    if sha256(source) != source_hash:
        raise ValueError(
            "restore the exact source PDF to its recorded inbox name before reprocessing"
        )
    generation = prior["generation"] + 1
    job_id = f"{source_hash}-r{generation:04d}"
    now = time.time()
    with ledger.db:
        ledger.db.execute(
            """INSERT INTO jobs(id,source_hash,generation,source_name,source_signature,
            config_revision,config,created,updated) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                source_hash,
                generation,
                prior["source_name"],
                json.dumps(fingerprint(source.lstat())),
                config.revision(),
                json.dumps(config.manifest_config()),
                now,
                now,
            ),
        )
        ledger.event(
            job_id, "administrative_reprocess", "operator accepted duplicate-ingestion risk"
        )
    Worker(config, ledger, None).write_manifest(job_id)
    print(job_id)


def reconcile(config, ledger, job_id, name):
    job = ledger.job(job_id)
    if job["status"] != "review":
        raise ValueError("reconciliation requires a quarantined job")
    output = next((o for o in ledger.outputs(job_id) if o["name"] == name), None)
    if output is None or output["status"] != "intent":
        raise ValueError("output does not have an ambiguous publication intent")
    worker = Worker(config, ledger, None)
    worker.record_published(job_id, name)
    with ledger.db:
        ledger.event(job_id, "operator_confirmed_delivered", name)
    outputs = ledger.outputs(job_id)
    if not any(o["status"] == "intent" for o in outputs):
        ledger.transition(job_id, "prepared")
    worker.write_manifest(job_id)
    print("delivery acknowledgment recorded; other outputs retain their recorded progress")


def main():
    parser = argparse.ArgumentParser(description="Local PDF boundary preprocessor")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("once")
    commands.add_parser("health")
    commands.add_parser("check")
    commands.add_parser("list")
    show = commands.add_parser("manifest")
    show.add_argument("job_id")
    repeat = commands.add_parser("reprocess")
    repeat.add_argument("source_hash")
    repeat.add_argument("--accept-duplicate-risk", action="store_true", required=True)
    retry = commands.add_parser("retry-input")
    retry.add_argument("name")
    delivered = commands.add_parser("ack-delivered")
    delivered.add_argument("job_id")
    delivered.add_argument("output_name")
    delivered.add_argument("--confirmed-in-consumer", action="store_true", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    try:
        config = Config.from_env()
        if args.command == "health":
            return health(config)
        if os.geteuid() == 0:
            raise ValueError("run the service and administration with the configured non-root UID")
        verify_layout(config)
        if args.command == "check":
            print("directory and mount layout valid; model/worker readiness is separate")
            return 0
        lock_fd = os.open(
            config.state / "worker.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(lock_fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "worker/state already in use; stop worker before administration"
                ) from None
            ledger = Ledger(config.state / "jobs.sqlite3")
            try:
                if args.command == "list":
                    for row in ledger.db.execute(
                        "SELECT id,status,error FROM jobs ORDER BY created"
                    ):
                        print(json.dumps(dict(row)))
                    return 0
                if args.command == "manifest":
                    print(json.dumps(ledger.manifest(args.job_id), indent=2))
                    return 0
                if args.command == "reprocess":
                    reprocess(config, ledger, args.source_hash)
                    return 0
                if args.command == "ack-delivered":
                    reconcile(config, ledger, args.job_id, args.output_name)
                    return 0
                if args.command == "retry-input":
                    if Path(args.name).name != args.name or args.name in {".", ".."}:
                        raise ValueError("retry-input accepts one inbox basename only")
                    with ledger.db:
                        cursor = ledger.db.execute(
                            "DELETE FROM observations WHERE name=?", (args.name,)
                        )
                        if not cursor.rowcount:
                            raise ValueError("no observation exists for that inbox name")
                    print("input observation reset; original remains untouched")
                    return 0
                stopped = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stopped.set())
                signal.signal(signal.SIGINT, lambda *_: stopped.set())
                heartbeat = Heartbeat(config)
                heartbeat.tick("startup")
                watchdog = threading.Thread(target=heartbeat.watchdog, daemon=True)
                watchdog.start()
                analyzer = Analyzer(config, heartbeat.tick, stopped.is_set)
                worker = Worker(config, ledger, analyzer, stopped.is_set)
                try:
                    worker.recover()
                    analyzer.start()
                    heartbeat.ready = True
                    while not stopped.is_set():
                        heartbeat.tick("scanning")
                        worker.cycle()
                        heartbeat.tick("idle")
                        if args.command == "once":
                            break
                        until = time.monotonic() + config.poll_seconds
                        while time.monotonic() < until and not stopped.wait(0.5):
                            heartbeat.tick("idle")
                finally:
                    heartbeat.ready = False
                    heartbeat.last_write = 0
                    heartbeat.tick("stopping")
                    analyzer.close()
                    heartbeat.closed.set()
            finally:
                ledger.close()
        return 0
    except InterruptedError:
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        LOG.error("service_failed type=%s reason=%s", type(error).__name__, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

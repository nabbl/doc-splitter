import dataclasses
import errno
import json

import pytest

from doc_splitter.fs import fingerprint, sha256
from doc_splitter.ledger import Ledger
from doc_splitter.worker import Worker

from .fixtures import legacy_failed_claim, text_pdf
from .test_worker import FakeAnalyzer, claim, prepared


def observation(ledger, name):
    return ledger.db.execute("SELECT * FROM observations WHERE name=?", (name,)).fetchone()


def test_upgrade_retries_existing_v1_claim_failure_without_touching_input(config):
    source = config.inbox / "existing-scan.pdf"
    text_pdf(source)
    before = fingerprint(source.stat())
    database = config.state / "jobs.sqlite3"
    report = legacy_failed_claim(database, source, config.review)
    ledger = Ledger(database)
    try:
        assert ledger.db.execute("PRAGMA user_version").fetchone()[0] == 3
        worker = Worker(config, ledger, FakeAnalyzer(config))
        worker.recover()
        assert observation(ledger, source.name)["handled"] == 0
        worker.cycle()
        assert ledger.job(sha256(source))["status"] == "completed"
        assert fingerprint(source.stat()) == before
        assert observation(ledger, source.name)["attempts"] == 2
        assert not report.exists()
    finally:
        ledger.close()


def test_startup_retries_once_per_restart_with_persistent_budget(config, ledger, monkeypatch):
    source = config.inbox / "source.pdf"
    text_pdf(source)
    calls = []

    def denied(*_):
        calls.append(True)
        raise PermissionError(errno.EACCES, "synthetic permission failure")

    monkeypatch.setattr("doc_splitter.worker.copy_source", denied)
    worker = Worker(config, ledger, None)
    worker.scan()
    for attempt in range(2, config.max_attempts + 1):
        worker.scan()
        assert len(calls) == attempt - 1
        restarted_ledger = Ledger(config.state / "jobs.sqlite3")
        try:
            restarted = Worker(config, restarted_ledger, None)
            restarted.recover()
            restarted.scan()
            restarted.scan()
            assert len(calls) == attempt
            assert observation(restarted_ledger, source.name)["attempts"] == attempt
        finally:
            restarted_ledger.close()
    worker.recover()
    worker.scan()
    assert len(calls) == config.max_attempts
    assert observation(ledger, source.name)["handled"] == 1
    assert list(config.review.glob("input-*.json"))
    assert source.exists()


def test_startup_retry_keeps_settling_and_marker_contract(config, ledger, monkeypatch):
    source = config.inbox / "source.pdf"
    text_pdf(source)
    marker = source.with_name(source.name + ".done")
    marker.touch()
    config = dataclasses.replace(config, settle_seconds=5, completion_mode="marker")
    worker = Worker(config, ledger, FakeAnalyzer(config))

    def full(*_):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr("doc_splitter.worker.copy_source", full)
        worker.scan(now=0)
        worker.scan(now=5)
    assert observation(ledger, source.name)["attempts"] == 1
    marker.unlink()
    monkeypatch.setattr("doc_splitter.worker.time.time", lambda: 100)
    worker.recover()
    worker.scan(now=106)
    assert observation(ledger, source.name)["attempts"] == 1
    marker.touch()
    worker.scan(now=102)
    assert observation(ledger, source.name)["attempts"] == 1
    worker.scan(now=106)
    assert observation(ledger, source.name)["attempts"] == 2
    assert ledger.job(sha256(source))["status"] == "queued"


def test_completed_and_legacy_successful_observations_stay_deduplicated(
    config, ledger, monkeypatch
):
    worker, job_id = claim(config, ledger)
    worker.process(job_id)
    before = [dict(output) for output in ledger.outputs(job_id)]
    for path in config.consume.iterdir():
        path.unlink()
    # A stale schema-v1 error report must not resurrect a successfully claimed source.
    row = ledger.job(job_id)
    _, report = worker.input_review(row["source_name"], row["source_signature"])
    report.write_text(
        json.dumps(
            {
                "status": "review",
                "source_name": row["source_name"],
                "signature": row["source_signature"],
                "reason": "claim I/O failure errno=22; fix storage and retry-input",
            }
        )
    )
    with ledger.db:
        ledger.db.execute("UPDATE observations SET attempts=0")
    monkeypatch.setattr(
        "doc_splitter.worker.copy_source", lambda *_: pytest.fail("completed source was reclaimed")
    )
    worker.recover()
    worker.cycle()
    assert [dict(output) for output in ledger.outputs(job_id)] == before
    assert not list(config.consume.iterdir())


def test_permanent_preclaim_failure_is_not_retried_on_restart(config, ledger, monkeypatch):
    source = config.inbox / "unsupported.docx"
    source.write_bytes(b"synthetic unsupported input")
    worker = Worker(config, ledger, None)
    worker.scan()
    row = observation(ledger, source.name)
    assert row["handled"] == 1 and row["retryable"] == 0 and row["attempts"] == 1
    monkeypatch.setattr(
        "doc_splitter.worker.copy_source", lambda *_: pytest.fail("permanent failure retried")
    )
    worker.recover()
    worker.scan()
    assert observation(ledger, source.name)["attempts"] == 1


def test_legacy_permanent_failure_does_not_become_retryable(config):
    source = config.inbox / "source.pdf"
    text_pdf(source)
    report = legacy_failed_claim(config.state / "jobs.sqlite3", source, config.review)
    data = json.loads(report.read_text())
    data["reason"] = "immutable archive checksum mismatch"
    report.write_text(json.dumps(data))
    ledger = Ledger(config.state / "jobs.sqlite3")
    try:
        Worker(config, ledger, None).recover()
        assert observation(ledger, source.name)["retryable"] == 0
        assert observation(ledger, source.name)["handled"] == 1
    finally:
        ledger.close()


def test_interrupted_claim_retries_without_losing_source(config, ledger):
    source = config.inbox / "interrupted.pdf"
    text_pdf(source)
    with ledger.db:
        ledger.db.execute(
            "INSERT INTO observations(name,signature,since,handled,attempts) VALUES(?,?,0,2,1)",
            (source.name, json.dumps(fingerprint(source.stat()))),
        )
    worker = Worker(config, ledger, FakeAnalyzer(config))
    worker.recover()
    worker.cycle()
    assert ledger.job(sha256(source))["status"] == "completed"
    assert observation(ledger, source.name)["attempts"] == 2


def test_ambiguous_delivery_is_not_requeued_as_an_input(config, ledger, monkeypatch):
    worker, job_id = prepared(config, ledger, monkeypatch)

    def crash(event, output):
        if event == "after_rename":
            (config.consume / output["name"]).unlink()
            raise SystemExit

    with pytest.raises(SystemExit):
        worker.publish(job_id, crash)
    worker.recover()
    worker.recover()
    worker.cycle()
    assert ledger.job(job_id)["status"] == "review"
    assert ledger.outputs(job_id)[0]["status"] == "intent"
    assert worker.analyzer.calls == 1


@pytest.mark.parametrize("status", ["completed", "review", "dry_run"])
def test_upgrade_preserves_terminal_jobs_and_output_history(config, monkeypatch, status):
    database = config.state / "jobs.sqlite3"
    original = Ledger(database)
    try:
        worker, job_id = claim(config, original)
        worker.process(job_id)
        original.transition(job_id, status)
        before = original.manifest(job_id)
        for path in config.consume.iterdir():
            path.unlink()
        # Reproduce the original four-column schema around an existing delivery ledger.
        with original.db:
            original.db.execute(
                "CREATE TABLE observations_v1 (name TEXT PRIMARY KEY, "
                "signature TEXT NOT NULL, since REAL NOT NULL, "
                "handled INTEGER NOT NULL DEFAULT 0)"
            )
            original.db.execute(
                "INSERT INTO observations_v1 SELECT name,signature,since,handled FROM observations"
            )
            original.db.execute("DROP TABLE observations")
            original.db.execute("ALTER TABLE observations_v1 RENAME TO observations")
            original.db.execute("PRAGMA user_version=1")
    finally:
        original.close()
    upgraded = Ledger(database)
    try:
        monkeypatch.setattr(
            "doc_splitter.worker.copy_source", lambda *_: pytest.fail("terminal source reclaimed")
        )
        analyzer = FakeAnalyzer(config)
        worker = Worker(config, upgraded, analyzer)
        worker.recover()
        worker.cycle()
        assert upgraded.manifest(job_id) == before
        assert not list(config.consume.iterdir())
        assert analyzer.calls == 0
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        upgraded.close()


def test_failure_after_claim_commit_does_not_reclaim_input(config, ledger, monkeypatch):
    source = config.inbox / "source.pdf"
    text_pdf(source)
    worker = Worker(config, ledger, FakeAnalyzer(config))

    def denied(*_):
        raise PermissionError(errno.EACCES, "synthetic manifest permission error")

    with monkeypatch.context() as patch:
        patch.setattr(worker, "write_manifest", denied)
        worker.scan()
    assert observation(ledger, source.name)["retryable"] == 0
    monkeypatch.setattr(
        "doc_splitter.worker.copy_source", lambda *_: pytest.fail("committed source reclaimed")
    )
    worker.recover()
    worker.cycle()
    assert ledger.job(sha256(source))["status"] == "completed"
    assert observation(ledger, source.name)["attempts"] == 1


def test_missing_legacy_review_does_not_guess_retryability(config, caplog):
    source = config.inbox / "source.pdf"
    text_pdf(source)
    database = config.state / "jobs.sqlite3"
    report = legacy_failed_claim(database, source, config.review)
    report.write_text("{invalid JSON")
    ledger = Ledger(database)
    try:
        worker = Worker(config, ledger, None)
        worker.recover()
        assert observation(ledger, source.name)["handled"] == 1
        assert "legacy_review_unreadable" in caplog.text
        report.unlink()
        worker.recover()
        assert observation(ledger, source.name)["handled"] == 1
        assert observation(ledger, source.name)["retryable"] == 0
    finally:
        ledger.close()

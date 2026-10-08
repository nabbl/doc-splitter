import dataclasses
import errno
import json
import multiprocessing
import os
import shutil
import signal

import pytest

from doc_splitter.cli import reconcile, reprocess
from doc_splitter.fs import UnsafeInput, copy_source, rename_noreplace, sha256, verify_layout
from doc_splitter.ledger import Ledger
from doc_splitter.pdf import prepare
from doc_splitter.worker import Worker

from .fixtures import text_pdf
from .test_model_pdf import FakeModel


class FakeAnalyzer:
    def __init__(self, config):
        self.config = config
        self.calls = 0

    def analyze(self, source, job_id):
        self.calls += 1
        return prepare(self.config, FakeModel(), source, job_id)


def claim(config, ledger, pages=4):
    path = config.inbox / "synthetic-test.pdf"
    text_pdf(path, pages)
    analyzer = FakeAnalyzer(config)
    worker = Worker(config, ledger, analyzer)
    worker.scan()
    job_id = ledger.db.execute("SELECT id FROM jobs").fetchone()["id"]
    return worker, job_id


def prepared(config, ledger, monkeypatch):
    worker, job_id = claim(config, ledger)
    with monkeypatch.context() as patch:
        patch.setattr(worker, "publish", lambda _: None)
        worker.process(job_id)
    assert ledger.job(job_id)["status"] == "prepared"
    return worker, job_id


def kill_at_publication_checkpoint(config, job_id, checkpoint, consumer):
    ledger = Ledger(config.state / "jobs.sqlite3")

    def crash(event, output):
        if event == checkpoint:
            if consumer:
                (config.consume / output["name"]).unlink()
            os.kill(os.getpid(), signal.SIGKILL)

    Worker(config, ledger, None).publish(job_id, crash)


def test_normal_and_duplicate_handling(config, ledger):
    worker, job_id = claim(config, ledger)
    before = sha256(config.inbox / "synthetic-test.pdf")
    worker.cycle()
    assert ledger.job(job_id)["status"] == "completed"
    assert len(list(config.consume.glob("*.pdf"))) == 2
    assert sha256(config.inbox / "synthetic-test.pdf") == before
    assert sha256(config.archive / job_id / "source.pdf") == before
    shutil.copyfile(config.inbox / "synthetic-test.pdf", config.inbox / "duplicate.pdf")
    worker.cycle()
    assert ledger.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert worker.analyzer.calls == 1


def test_slow_write_and_marker(config, ledger):
    config = dataclasses.replace(config, settle_seconds=5, completion_mode="marker")
    source = config.inbox / "slow.pdf"
    source.write_bytes(b"%PDF")
    worker = Worker(config, ledger, FakeAnalyzer(config))
    worker.scan(now=10)
    worker.scan(now=20)
    assert ledger.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    text_pdf(source)
    worker.scan(now=21)
    source.with_name(source.name + ".done").touch()
    worker.scan(now=24)
    assert ledger.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    worker.scan(now=27)
    assert ledger.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_temporary_and_unsupported_inputs(config, ledger):
    (config.inbox / "upload.pdf.part").write_bytes(b"partial")
    (config.inbox / "unsupported.docx").write_bytes(b"unsupported")
    Worker(config, ledger, None).scan()
    assert ledger.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert len(list(config.review.iterdir())) == 1


def test_symlink_and_layout_rejected(config, ledger):
    (config.inbox / "escape.pdf").symlink_to("/etc/passwd")
    Worker(config, ledger, None).scan()
    assert len(list(config.review.iterdir())) == 1
    with pytest.raises(ValueError, match="disjoint"):
        dataclasses.replace(config, work=config.inbox / "nested").validate()
    with pytest.raises(ValueError, match="symlink"):
        dataclasses.replace(config, work=config.inbox / "escape.pdf").validate()
    verify_layout(config)


def test_source_mutation_quarantines_without_publication(config, ledger):
    worker, job_id = claim(config, ledger)
    text_pdf(config.inbox / "synthetic-test.pdf", 5)
    worker.process(job_id)
    assert ledger.job(job_id)["status"] == "review"
    assert not list(config.consume.iterdir())


def test_mutation_during_copy(config, monkeypatch):
    source = config.inbox / "mutable.pdf"
    source.write_bytes(b"original")
    original_fsync = os.fsync

    def mutate(fd):
        source.write_bytes(b"changed!")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", mutate)
    with pytest.raises(UnsafeInput, match="mutated"):
        copy_source(source, config.work / "copy.pdf", 100)


def test_exclusive_rename_never_overwrites(config):
    source, destination = config.staging / "a", config.consume / "b"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        rename_noreplace(source, destination)
    assert destination.read_bytes() == b"old"
    assert source.read_bytes() == b"new"


def test_destination_collision_quarantines(config, ledger, monkeypatch):
    worker, job_id = prepared(config, ledger, monkeypatch)
    output = ledger.outputs(job_id)[0]
    (config.consume / output["name"]).write_bytes(b"unrelated")
    worker.process(job_id)
    assert ledger.job(job_id)["status"] == "review"
    assert (config.consume / output["name"]).read_bytes() == b"unrelated"


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES, errno.EXDEV])
def test_output_failure_never_falls_back_to_unsplit(config, ledger, monkeypatch, code):
    worker, job_id = prepared(config, ledger, monkeypatch)

    def fail_rename(*_):
        raise OSError(code, "synthetic publication failure")

    monkeypatch.setattr("doc_splitter.worker.rename_noreplace", fail_rename)
    worker.process(job_id)
    assert ledger.job(job_id)["status"] == "review"
    assert not list(config.consume.iterdir())
    assert ledger.outputs(job_id)[0]["status"] == "intent"


@pytest.mark.parametrize(
    ("checkpoint", "consumer", "expected"),
    [
        ("before_intent", False, "completed"),
        ("after_intent", False, "review"),
        ("after_rename", False, "completed"),
        ("after_rename", True, "review"),
        ("after_record", True, "completed"),
    ],
)
def test_crash_publication_windows(config, ledger, monkeypatch, checkpoint, consumer, expected):
    _, job_id = prepared(config, ledger, monkeypatch)
    child = multiprocessing.get_context("spawn").Process(
        target=kill_at_publication_checkpoint,
        args=(config, job_id, checkpoint, consumer),
    )
    child.start()
    child.join(timeout=15)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("publication checkpoint child hung")
    assert child.exitcode == -signal.SIGKILL
    child.close()
    # Separate connection reads durable state, like a restarted worker.
    restarted_ledger = Ledger(config.state / "jobs.sqlite3")
    try:
        analyzer = FakeAnalyzer(config)
        restarted = Worker(config, restarted_ledger, analyzer)
        restarted.recover()
        restarted.process(job_id)
        assert restarted_ledger.job(job_id)["status"] == expected
        assert analyzer.calls == 0
        if expected == "review":
            assert list(config.review.glob(job_id + ".json"))
    finally:
        restarted_ledger.close()


def test_immediate_consumer_does_not_cause_regeneration(config, ledger, monkeypatch):
    worker, job_id = prepared(config, ledger, monkeypatch)
    original_rename = rename_noreplace
    consumed = []

    def consume_immediately(source, destination):
        original_rename(source, destination)
        consumed.append(destination.name)
        destination.unlink()

    monkeypatch.setattr("doc_splitter.worker.rename_noreplace", consume_immediately)
    worker.process(job_id)
    worker.recover()
    worker.cycle()
    assert ledger.job(job_id)["status"] == "completed"
    assert len(consumed) == 2 and worker.analyzer.calls == 1
    assert not list(config.consume.iterdir())


def test_disk_failures_have_bounded_retries(config, ledger, monkeypatch):
    worker, job_id = claim(config, ledger)

    def disk_full(*_):
        raise OSError(errno.ENOSPC, "test disk full")

    monkeypatch.setattr(worker.analyzer, "analyze", disk_full)
    for _ in range(config.max_attempts + 1):
        with ledger.db:
            ledger.db.execute("UPDATE jobs SET next_attempt=0")
        worker.process(job_id)
    assert ledger.job(job_id)["status"] == "review"
    assert ledger.job(job_id)["attempts"] == config.max_attempts
    assert not list(config.consume.iterdir())


def test_claim_permissions_failure_is_retained_and_reviewed(config, ledger, monkeypatch):
    source = config.inbox / "source.pdf"
    text_pdf(source)

    def denied(*_):
        raise PermissionError(errno.EACCES, "test denied")

    monkeypatch.setattr("doc_splitter.worker.copy_source", denied)
    worker = Worker(config, ledger, None)
    worker.scan()
    worker.scan()
    assert source.exists()
    assert len(list(config.review.iterdir())) == 1
    report = json.loads(next(config.review.iterdir()).read_text())
    assert report["operation"] == "copy_to_work"
    assert "operation=copy_to_work" in report["reason"]


def test_unsupported_archive_rename_is_diagnosed_and_retryable(config, ledger, monkeypatch):
    source = config.inbox / "source.pdf"
    text_pdf(source)
    digest = sha256(source)
    worker = Worker(config, ledger, FakeAnalyzer(config))

    def unsupported(*_):
        raise OSError(errno.EINVAL, "synthetic unsupported rename flag")

    with monkeypatch.context() as patch:
        patch.setattr("doc_splitter.worker.rename_noreplace", unsupported)
        worker.scan()
    report = json.loads(next(config.review.iterdir()).read_text())
    assert report["operation"] == "seal_archive"
    assert "errno=22" in report["reason"]
    assert "archive exclusive-rename support" in report["reason"]
    assert sha256(source) == digest
    assert (config.archive / digest / "source.pending").exists()
    assert not (config.archive / digest / "source.pdf").exists()
    assert ledger.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert not list(config.consume.iterdir())
    # Startup retries the same untouched input after the storage configuration is fixed.
    worker.recover()
    worker.cycle()
    assert ledger.job(digest)["status"] == "completed"
    assert sha256(config.archive / digest / "source.pdf") == digest
    assert not (config.archive / digest / "source.pending").exists()
    assert not list(config.review.glob("input-*.json"))


def test_interrupted_analysis_restarts_before_any_publication(config, ledger):
    worker, job_id = claim(config, ledger)
    with ledger.db:
        ledger.db.execute("UPDATE jobs SET status='preparing',attempts=1")
    (config.staging / f"{job_id}-p000001-000002.pdf").write_bytes(b"partial")
    worker.recover()
    worker.process(job_id)
    assert ledger.job(job_id)["status"] == "completed"
    assert ledger.job(job_id)["attempts"] == 2


def test_interrupted_claim_is_not_silently_lost(config, ledger):
    with ledger.db:
        ledger.db.execute(
            "INSERT INTO observations(name,signature,since,handled) VALUES('source.pdf','[]',0,2)"
        )
    Worker(config, ledger, None).recover()
    assert len(list(config.review.iterdir())) == 1


def test_reprocess_is_explicit_and_audited(config, ledger):
    worker, job_id = claim(config, ledger)
    worker.process(job_id)
    reprocess(config, ledger, job_id)
    new_id = job_id + "-r0001"
    worker.process(new_id)
    assert ledger.job(new_id)["status"] == "completed"
    assert len(list(config.consume.iterdir())) == 4
    assert any(e["event"] == "administrative_reprocess" for e in ledger.manifest(new_id)["events"])


def test_reconciliation_preserves_other_output_progress(config, ledger, monkeypatch):
    worker, job_id = prepared(config, ledger, monkeypatch)
    first = ledger.outputs(job_id)[0]

    def crash(event, output):
        if event == "after_rename":
            (config.consume / output["name"]).unlink()
            raise SystemExit

    with pytest.raises(SystemExit):
        worker.publish(job_id, crash)
    worker.recover()
    reconcile(config, ledger, job_id, first["name"])
    worker.process(job_id)
    assert ledger.job(job_id)["status"] == "completed"
    assert len(list(config.consume.iterdir())) == 1


def test_config_change_requires_explicit_review(config, ledger):
    _, job_id = claim(config, ledger)
    changed = dataclasses.replace(config, threshold=0.7)
    Worker(changed, ledger, FakeAnalyzer(changed)).process(job_id)
    assert ledger.job(job_id)["status"] == "review"
    assert (
        "configuration changed"
        in json.loads((config.review / (job_id + ".json")).read_text())["error"]
    )

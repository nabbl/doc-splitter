import dataclasses
import errno
import multiprocessing
import os
import shutil
import signal

import pytest

from doc_splitter.cli import reprocess
from doc_splitter.fs import sha256, verify_layout
from doc_splitter.ledger import Ledger
from doc_splitter.worker import Worker

from .fixtures import text_pdf
from .test_worker import FakeAnalyzer, claim, prepared


def enable(config):
    return dataclasses.replace(config, delete_completed_inputs=True)


def run_cleanup(config, ledger):
    worker = Worker(enable(config), ledger, FakeAnalyzer(enable(config)))
    worker.recover()
    return worker


def test_completed_input_is_removed_only_after_all_outputs(config, ledger, monkeypatch):
    worker, job_id = prepared(enable(config), ledger, monkeypatch)
    source = config.inbox / "synthetic-test.pdf"
    digest = sha256(source)
    worker.cleanup_completed_inputs()
    assert source.exists()

    def checkpoint(event, _):
        assert source.exists(), event

    worker.publish(job_id, checkpoint)
    worker.cleanup_completed_inputs()
    assert not source.exists()
    assert sha256(worker.source(ledger.job(job_id))) == digest
    assert ledger.job(job_id)["status"] == "completed"
    assert all(row["status"] == "published" for row in ledger.outputs(job_id))
    assert any(event["event"] == "input_deleted" for event in ledger.manifest(job_id)["events"])
    assert not list(config.inbox.iterdir())


def test_default_retains_inputs_and_toggle_preserves_inference_revision(config, ledger):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    assert (config.inbox / "synthetic-test.pdf").exists()
    assert config.revision() == enable(config).revision()
    run_cleanup(config, ledger)
    assert not (config.inbox / "synthetic-test.pdf").exists()
    assert ledger.job(job_id)["status"] == "completed"


def test_upgrade_backfills_completed_input_without_reprocessing(config, ledger):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    before = [dict(row) for row in ledger.outputs(job_id)]
    for path in config.consume.iterdir():
        path.unlink()
    with ledger.db:
        ledger.db.execute("DROP TABLE input_cleanups")
        ledger.db.execute("PRAGMA user_version=2")
    ledger.close()
    upgraded = Ledger(config.state / "jobs.sqlite3")
    try:
        worker = run_cleanup(config, upgraded)
        worker.cycle()
        assert not (config.inbox / "synthetic-test.pdf").exists()
        assert [dict(row) for row in upgraded.outputs(job_id)] == before
        assert not list(config.consume.iterdir())
        assert worker.analyzer.calls == 0
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        upgraded.close()


@pytest.mark.parametrize("status", ["queued", "preparing", "prepared", "review", "dry_run"])
def test_incomplete_or_reviewed_jobs_keep_original(config, ledger, status):
    worker, job_id = claim(enable(config), ledger)
    ledger.transition(job_id, status)
    worker.cleanup_completed_inputs()
    assert (config.inbox / "synthetic-test.pdf").exists()
    assert not list(config.inbox.glob(".doc-splitter-cleanup-*"))


def test_dry_run_mode_never_cleans_prior_completed_files(config, ledger):
    worker, _ = claim(config, ledger)
    worker.cycle()
    Worker(dataclasses.replace(enable(config), dry_run=True), ledger, None).recover()
    assert (config.inbox / "synthetic-test.pdf").exists()


def test_duplicate_is_cleaned_without_republishing(config, ledger):
    worker, job_id = claim(enable(config), ledger)
    worker.cycle()
    before = [dict(row) for row in ledger.outputs(job_id)]
    for output in config.consume.iterdir():
        output.unlink()
    shutil.copyfile(worker.source(ledger.job(job_id)), config.inbox / "duplicate.pdf")
    worker.cycle()
    assert not list(config.inbox.iterdir())
    assert [dict(row) for row in ledger.outputs(job_id)] == before
    assert worker.analyzer.calls == 1
    assert not list(config.consume.iterdir())


def test_ambiguous_delivery_keeps_source(config, ledger, monkeypatch):
    worker, job_id = prepared(enable(config), ledger, monkeypatch)

    def crash(event, output):
        if event == "after_rename":
            (config.consume / output["name"]).unlink()
            raise SystemExit

    with pytest.raises(SystemExit):
        worker.publish(job_id, crash)
    worker.recover()
    assert ledger.job(job_id)["status"] == "review"
    assert (config.inbox / "synthetic-test.pdf").exists()


def test_replacement_before_cleanup_is_not_deleted(config, ledger):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    source = config.inbox / "synthetic-test.pdf"
    text_pdf(source, 1, label="NEW SCAN")
    digest = sha256(source)
    run_cleanup(config, ledger)
    assert sha256(source) == digest
    assert ledger.job(job_id)["status"] == "completed"
    assert list(config.review.glob("cleanup-*.json"))


def test_replacement_during_detach_is_retained_privately(config, ledger, monkeypatch):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    rename = os.rename
    replacement = config.inbox / ".replacement"
    text_pdf(replacement, 1, label="NEW SCAN")
    digest = sha256(replacement)

    def swap_then_rename(origin, destination):
        os.replace(replacement, origin)
        rename(origin, destination)

    monkeypatch.setattr("doc_splitter.cleanup.os.rename", swap_then_rename)
    run_cleanup(config, ledger)
    held = next(config.inbox.glob(".doc-splitter-cleanup-*/source.pdf"))
    assert sha256(held) == digest
    assert list(config.review.glob("cleanup-*.json"))
    assert ledger.job(job_id)["status"] == "completed"


def test_new_file_after_detach_is_never_unlinked(config, ledger):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    source = config.inbox / "synthetic-test.pdf"
    cleaner = Worker(enable(config), ledger, None).input_cleanup
    replacement_digest = None

    def upload_after_detach(event):
        nonlocal replacement_digest
        if event == "after_detach":
            text_pdf(source, 1, label="NEW SCAN")
            replacement_digest = sha256(source)

    cleaner.run(upload_after_detach)
    assert sha256(source) == replacement_digest
    assert sha256(worker.source(ledger.job(job_id))) == ledger.job(job_id)["source_hash"]


@pytest.mark.parametrize("failure", ["rename", "unlink"])
def test_cleanup_io_error_does_not_undo_completion_and_retries_on_restart(
    config, ledger, monkeypatch, failure
):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    config = enable(config)

    def denied(*_):
        raise PermissionError(errno.EACCES, "synthetic permission failure")

    with monkeypatch.context() as patch:
        if failure == "rename":
            patch.setattr("doc_splitter.cleanup.os.rename", denied)
        else:
            original_unlink = type(config.inbox).unlink

            def fail_held_unlink(path, *args, **kwargs):
                if path.name == "source.pdf" and path.parent.name.startswith(
                    ".doc-splitter-cleanup-"
                ):
                    denied()
                return original_unlink(path, *args, **kwargs)

            patch.setattr(type(config.inbox), "unlink", fail_held_unlink)
        restarted = run_cleanup(config, ledger)
        restarted.cleanup_completed_inputs()
    assert ledger.job(job_id)["status"] == "completed"
    assert list(config.review.glob("cleanup-*.json"))
    assert (
        ledger.db.execute(
            "SELECT count(*) FROM events WHERE event='input_cleanup_failed'"
        ).fetchone()[0]
        == 1
    )
    run_cleanup(config, ledger)
    assert not list(config.inbox.iterdir())
    assert not list(config.review.glob("cleanup-*.json"))


def kill_during_cleanup(config, checkpoint):
    ledger = Ledger(config.state / "jobs.sqlite3")

    def crash(event):
        if event == checkpoint:
            os.kill(os.getpid(), signal.SIGKILL)

    Worker(config, ledger, None).input_cleanup.run(crash)


@pytest.mark.parametrize("checkpoint", ["after_detach", "before_unlink", "after_unlink"])
def test_crash_recovers_private_cleanup_without_touching_replacement(config, ledger, checkpoint):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    config = enable(config)
    process = multiprocessing.get_context("spawn").Process(
        target=kill_during_cleanup, args=(config, checkpoint)
    )
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("cleanup checkpoint child hung")
    assert process.exitcode == -signal.SIGKILL
    process.close()
    source = config.inbox / "synthetic-test.pdf"
    text_pdf(source, 1, label="NEW SCAN")
    replacement_digest = sha256(source)
    restarted = Ledger(config.state / "jobs.sqlite3")
    try:
        run_cleanup(config, restarted)
        assert sha256(source) == replacement_digest
        assert restarted.job(job_id)["status"] == "completed"
        assert not list(config.inbox.glob(".doc-splitter-cleanup-*"))
    finally:
        restarted.close()


def test_archive_corruption_prevents_cleanup(config, ledger):
    worker, job_id = claim(config, ledger)
    worker.cycle()
    archive = worker.source(ledger.job(job_id))
    archive.chmod(0o600)
    archive.write_bytes(b"synthetic corruption")
    run_cleanup(config, ledger)
    assert (config.inbox / "synthetic-test.pdf").exists()
    assert list(config.review.glob("cleanup-*.json"))


def test_cleanup_does_not_require_exclusive_rename_flags_in_inbox(config, ledger, monkeypatch):
    worker, _ = claim(config, ledger)
    worker.cycle()
    monkeypatch.setattr(
        "doc_splitter.worker.rename_noreplace",
        lambda *_: pytest.fail("cleanup attempted exclusive handoff rename"),
    )
    run_cleanup(config, ledger)
    assert not list(config.inbox.iterdir())


def test_cleanup_requires_writable_inbox_only_when_enabled(config, monkeypatch):
    access = os.access
    monkeypatch.setattr(
        "doc_splitter.fs.os.access",
        lambda path, mode: False if path == config.inbox else access(path, mode),
    )
    verify_layout(config)
    with pytest.raises(ValueError, match="inbox must be writable"):
        verify_layout(enable(config))


def test_reprocess_explains_how_to_restore_cleaned_up_source(config, ledger):
    worker, job_id = claim(enable(config), ledger)
    worker.cycle()
    with pytest.raises(ValueError, match="restore the exact source PDF"):
        reprocess(enable(config), ledger, job_id)


def test_active_reprocess_protects_the_restored_original(config, ledger):
    worker, job_id = claim(enable(config), ledger)
    worker.cycle()
    source = config.inbox / "synthetic-test.pdf"
    shutil.copyfile(worker.source(ledger.job(job_id)), source)
    reprocess(enable(config), ledger, job_id)
    run_cleanup(config, ledger)
    assert source.exists()
    worker.cycle()
    assert ledger.job(job_id + "-r0001")["status"] == "completed"
    assert not source.exists()

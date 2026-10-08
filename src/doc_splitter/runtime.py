import fcntl
import logging
import multiprocessing
import os
import signal
import threading
import time

from .fs import UnsafeInput

LOG = logging.getLogger(__name__)


def analyzer_main(config, connection, parent_pid):
    os.setsid()

    def parent_watchdog():
        while True:
            time.sleep(0.25)
            if os.getppid() != parent_pid:
                os.killpg(os.getpid(), signal.SIGKILL)

    threading.Thread(target=parent_watchdog, daemon=True).start()
    lock_fd = os.open(
        config.state / "analyzer.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    from .model import Model
    from .pdf import check_ocr, prepare

    ready = False
    try:
        check_ocr(config)
        model = Model(config)
        model.warmup()
        ready = True
        connection.send(("ready", None))
        while True:
            source, job_id = connection.recv()
            try:
                result = prepare(config, model, source, job_id)
                connection.send(("result", result))
            except UnsafeInput as error:
                connection.send(("permanent", str(error)))
            except Exception as error:
                # Native-library exceptions may include document text or private filenames.
                connection.send(
                    ("transient", f"{type(error).__name__}; errno={getattr(error, 'errno', None)}")
                )
    except Exception as error:
        if not ready and isinstance(error, ValueError):
            LOG.error("model_startup_configuration_failed reason=%s", error)
        else:
            LOG.error(
                "analyzer_failed type=%s errno=%s",
                type(error).__name__,
                getattr(error, "errno", None),
            )
        raise SystemExit(1) from None
    finally:
        connection.close()
        os.close(lock_fd)


class Analyzer:
    def __init__(self, config, tick, stopped):
        self.config = config
        self.tick = tick
        self.stopped = stopped
        self.process = None
        self.connection = None

    def start(self):
        if self.process is not None and self.process.is_alive():
            return
        self.close()
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=analyzer_main, args=(self.config, child, os.getpid()))
        self.process.start()
        child.close()
        kind, _ = self.wait(self.config.startup_timeout, "model_startup")
        if kind != "ready":
            raise RuntimeError("analyzer did not become ready")

    def wait(self, timeout, phase):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            self.tick(phase)
            if self.stopped():
                self.close()
                raise InterruptedError("shutdown requested")
            if self.connection.poll(0.25):
                try:
                    return self.connection.recv()
                except EOFError:
                    self.close()
                    raise RuntimeError("analysis process exited unexpectedly") from None
            if not self.process.is_alive():
                self.close()
                raise RuntimeError("analysis process exited unexpectedly")
        self.close()
        raise TimeoutError(f"{phase} exceeded configured time limit")

    def analyze(self, source, job_id):
        self.start()
        self.connection.send((source, job_id))
        kind, result = self.wait(self.config.job_timeout, "analysis")
        if kind == "permanent":
            raise UnsafeInput(result)
        if kind != "result":
            raise RuntimeError(result)
        return result

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    self.process.terminate()
                self.process.join(timeout=5)
                if self.process.is_alive():
                    os.killpg(self.process.pid, signal.SIGKILL)
            self.process.join(timeout=5)
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

import dataclasses
import hashlib
import io

import pytest

from doc_splitter.config import Config
from doc_splitter.model import snapshot


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("threshold", float("nan")),
        ("threshold", 1),
        ("batch_size", 9),
        ("max_pages", 129),
        ("ocr_languages", "eng;echo unsafe"),
        ("completion_mode", "guess"),
        ("model_revision", "main"),
        ("heartbeat_timeout", 0),
    ],
)
def test_invalid_config_is_explicit(config, field, value):
    with pytest.raises(ValueError):
        dataclasses.replace(config, **{field: value}).validate()


def test_environment_requires_explicit_roots(monkeypatch):
    monkeypatch.delenv("SPLIT_INBOX", raising=False)
    with pytest.raises(ValueError, match="SPLIT_INBOX"):
        Config.from_env()


def test_interrupted_download_recovers_and_warm_cache_stays_offline(config, monkeypatch):
    content = b"synthetic pinned model artifact"
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr("doc_splitter.model.ARTIFACTS", {"test.onnx": digest})

    class Interrupted(io.BytesIO):
        def read(self, *_):
            raise ConnectionError("synthetic interrupted transfer")

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Interrupted(content))
    with pytest.raises(ConnectionError):
        snapshot(config)
    root = config.model_cache / config.model_revision
    assert not (root / "test.onnx").exists()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(content))
    snapshot(config)
    assert (root / "test.onnx").read_bytes() == content
    assert not (root / "test.onnx.part").exists()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network called"))
    assert snapshot(config) == root


def test_wrong_artifact_hash_never_marks_cache_ready(config, monkeypatch):
    monkeypatch.setattr("doc_splitter.model.ARTIFACTS", {"test.onnx": "0" * 64})
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(b"bad artifact"))
    with pytest.raises(RuntimeError, match="checksum"):
        snapshot(config)
    assert not (config.model_cache / config.model_revision / "test.onnx").exists()

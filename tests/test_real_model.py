import dataclasses
import os
import resource
import time
from pathlib import Path

import numpy as np
import pytest

from doc_splitter.model import Model
from doc_splitter.pdf import prepare

from .fixtures import image_pdf, merged_pdf, text_pdf

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def real_model(tmp_path_factory):
    from doc_splitter.config import Config

    cache = os.getenv("TEST_MODEL_CACHE")
    if not cache:
        pytest.skip("set TEST_MODEL_CACHE to run the real pinned-model acceptance tests")
    root = tmp_path_factory.mktemp("real-model")
    roots = {}
    for name in ("inbox", "consume", "staging", "archive", "review", "work", "state"):
        roots[name] = root / name
        roots[name].mkdir()
    config = Config(**roots, model_cache=Path(cache), ocr_languages="eng", render_dpi=100)
    started = time.monotonic()
    model = Model(config)
    model.warmup()
    print(f"\nREAL_MODEL startup_seconds={time.monotonic() - started:.3f}")
    return config, model


@pytest.mark.parametrize("pages", [1, 4, 41])
def test_real_sequence_inference(real_model, tmp_path, pages):
    config, model = real_model
    source = tmp_path / "synthetic.pdf"
    if pages == 4:
        merged_pdf(source)
    else:
        text_pdf(source, pages, rotate=True)
    started = time.monotonic()
    result = prepare(config, model, source, f"real-{pages}")
    assert len(result["scores"]) == pages
    assert sum(end - start + 1 for start, end in result["ranges"]) == pages
    assert all(np.isfinite(result["scores"]))
    print(
        f"\nREAL_MODEL pages={pages} seconds={time.monotonic() - started:.3f} "
        f"maxrss_platform_units={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss} "
        f"ranges={result['ranges']}"
    )


def test_real_scan_and_ocr(real_model, tmp_path):
    config, model = real_model
    source = tmp_path / "synthetic-scan.pdf"
    image_pdf(source)
    started = time.monotonic()
    result = prepare(config, model, source, "real-scan")
    assert result["ranges"] == [(1, 1)]
    print(f"\nREAL_MODEL image_scan_seconds={time.monotonic() - started:.3f}")


def test_warm_snapshot_makes_no_network_calls(real_model, monkeypatch):
    config, _ = real_model

    def forbidden(*_, **__):
        pytest.fail("warm model attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    model = Model(dataclasses.replace(config, threads=1))
    model.warmup()

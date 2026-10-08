import dataclasses
import itertools
import json
import math

import numpy as np
import pikepdf
import pytest

from doc_splitter.fs import UnsafeInput
from doc_splitter.model import calibrated_marginals, page_ranges
from doc_splitter.pdf import prepare, validate_export, validate_ranges

from .fixtures import image_pdf, text_pdf


class FakeModel:
    def __init__(self, starts=(1, 3)):
        self.starts = starts
        self.batch_lengths = []

    def encode(self, images, texts):
        self.batch_lengths.append(len(images))
        return len(images)

    def boundaries(self, embeddings):
        return np.array(
            [0.9 if index + 1 in self.starts else 0.1 for index in range(sum(embeddings))]
        )


def test_smoothing_and_calibration_match_enumerated_chain():
    logits = [30.0, -0.2, 0.8, -0.6]
    crf = {"trans": [[0.2, -0.1], [-0.4, 0.3]], "start": [0.1, -0.1], "end": [-0.3, 0.2]}
    weights = []
    for tags in itertools.product((0, 1), repeat=4):
        log_weight = crf["start"][tags[0]] + crf["end"][tags[-1]]
        log_weight += sum(logits[index] * tag for index, tag in enumerate(tags))
        log_weight += sum(crf["trans"][tags[i - 1]][tags[i]] for i in range(1, 4))
        weights.append((tags, math.exp(log_weight)))
    denominator = sum(weight for _, weight in weights)
    raw = np.array([sum(w for tags, w in weights if tags[i]) / denominator for i in range(4)])
    raw = np.clip(raw, 1e-12, 1 - 1e-12)
    expected = 1 / (1 + np.exp(-(0.516 * np.log(raw) - 0.402 * np.log1p(-raw) - 0.155)))
    np.testing.assert_allclose(calibrated_marginals(np.array([0, *logits[1:]]), crf), expected)


@pytest.mark.parametrize("count", [1, 4, 41, 128])
def test_full_page_coverage_and_bounded_batches(config, count):
    source = config.work / "synthetic.pdf"
    text_pdf(source, count, rotate=True)
    model = FakeModel()
    result = prepare(config, model, source, "test")
    assert result["page_count"] == count
    assert len(result["scores"]) == count
    assert max(model.batch_lengths) <= config.batch_size
    assert sum(end - start + 1 for start, end in result["ranges"]) == count
    for output in result["outputs"]:
        validate_export(source, config.staging / output["name"], output["start"], output["end"])


@pytest.mark.parametrize("kind", ["corrupt", "encrypted", "empty", "oversized"])
def test_bad_pdfs_are_explicitly_rejected(config, kind):
    source = config.work / "bad.pdf"
    if kind == "corrupt":
        source.write_bytes(b"not a pdf")
    elif kind == "empty":
        with pikepdf.Pdf.new() as pdf:
            pdf.save(source)
    else:
        text_pdf(source, 129 if kind == "oversized" else 1)
        if kind == "encrypted":
            encrypted = config.work / "encrypted.pdf"
            with pikepdf.open(source) as pdf:
                pdf.save(encrypted, encryption=pikepdf.Encryption(owner="owner", user="password"))
            source = encrypted
    with pytest.raises(UnsafeInput):
        prepare(config, FakeModel(), source, "bad")
    assert not list(config.staging.iterdir())


def test_existing_text_does_not_call_ocr(config, monkeypatch):
    source = config.work / "text.pdf"
    text_pdf(source)
    monkeypatch.setattr(
        "doc_splitter.pdf.subprocess.run", lambda *a, **k: pytest.fail("OCR called")
    )
    prepare(config, FakeModel(), source, "text")


def test_image_only_uses_local_ocr(config):
    source = config.work / "image.pdf"
    image_pdf(source)

    class OcrModel(FakeModel):
        def encode(self, images, texts):
            assert "SYNTHETIC" in texts[0].upper()
            return super().encode(images, texts)

    result = prepare(config, OcrModel(), source, "scan")
    assert result["ranges"] == [(1, 1)]


def test_dry_run_and_review_do_not_export(config):
    source = config.work / "text.pdf"
    text_pdf(source, 4)
    result = prepare(dataclasses.replace(config, dry_run=True), FakeModel(), source, "dry")
    assert not result["outputs"]
    result = prepare(dataclasses.replace(config, review_margin=0.45), FakeModel(), source, "review")
    assert result["review_required"] and not result["outputs"]
    assert not list(config.staging.iterdir())


def test_first_page_always_starts_and_ranges_reject_gaps():
    assert page_ranges(np.array([0.0, 0.1, 0.9]), 0.5) == [(1, 2), (3, 3)]
    for ranges in ([], [(2, 3)], [(1, 2), (2, 3)], [(1, 1), (3, 3)]):
        with pytest.raises(UnsafeInput):
            validate_ranges(ranges, 3)


def test_nonfinite_scores_rejected():
    with pytest.raises(UnsafeInput):
        page_ranges(np.array([np.nan]), 0.5)


def test_model_artifact_metadata_is_json_serializable():
    from doc_splitter.model import ARTIFACTS

    assert len(json.loads(json.dumps(ARTIFACTS))) == 8

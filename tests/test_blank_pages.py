import dataclasses
import json

import numpy as np
import pikepdf
import pytest
from PIL import Image, ImageDraw

from doc_splitter.fs import UnsafeInput, sha256
from doc_splitter.pdf import is_blank_page, prepare, validate_export, validate_ranges
from doc_splitter.worker import Worker

from .fixtures import duplex_pdf
from .test_model_pdf import FakeModel
from .test_worker import FakeAnalyzer


@pytest.mark.parametrize("starts,expected", [((1,), [(2, 4)]), ((1, 2), [(2, 2), (4, 4)])])
def test_blank_duplex_backs_are_omitted_before_inference_and_export(config, starts, expected):
    source = config.work / "duplex.pdf"
    duplex_pdf(source)
    digest = sha256(source)
    model = FakeModel(starts)
    result = prepare(dataclasses.replace(config, remove_blank_pages=True), model, source, "test")
    assert result["page_count"] == 5
    assert result["retained_pages"] == [2, 4]
    assert result["blank_pages"] == [1, 3, 5]
    assert result["ranges"] == expected
    assert result["scores"][0] is None and result["scores"][2] is None
    assert sum(model.batch_lengths) == 2
    pages = []
    for output in result["outputs"]:
        pages.extend(output["source_pages"])
        validate_export(
            source,
            config.staging / output["name"],
            output["start"],
            output["end"],
            result["blank_pages"],
        )
        with pikepdf.open(config.staging / output["name"]) as pdf:
            assert len(pdf.pages) == len(output["source_pages"])
    assert pages == [2, 4]
    assert sha256(source) == digest


def test_disabled_filter_preserves_existing_page_coverage(config):
    source = config.work / "duplex.pdf"
    duplex_pdf(source)
    result = prepare(config, FakeModel(), source, "test")
    assert result["blank_pages"] == []
    assert result["retained_pages"] == [1, 2, 3, 4, 5]
    assert sum(end - start + 1 for start, end in result["ranges"]) == 5


def test_all_blank_archive_and_remove_without_inference_or_publication(config, ledger):
    config = dataclasses.replace(config, remove_blank_pages=True, delete_completed_inputs=True)
    source = config.inbox / "blank.pdf"
    duplex_pdf(source, all_blank=True)
    digest = sha256(source)

    class NoInference(FakeModel):
        def encode(self, images, texts):
            pytest.fail("all-blank scan reached model inference")

        def boundaries(self, embeddings):
            pytest.fail("all-blank scan reached boundary prediction")

    class Analyzer:
        def analyze(self, path, job_id):
            return prepare(config, NoInference(), path, job_id)

    worker = Worker(config, ledger, Analyzer())
    worker.cycle()
    assert ledger.job(digest)["status"] == "completed"
    assert not source.exists()
    assert sha256(worker.source(ledger.job(digest))) == digest
    assert not ledger.outputs(digest)
    assert not list(config.consume.iterdir())
    assert not list(config.staging.iterdir())
    proposal = json.loads(ledger.job(digest)["proposal"])
    assert proposal["blank_pages"] == [1, 2, 3, 4, 5]
    assert proposal["ranges"] == [] and proposal["scores"] == [None] * 5
    worker.recover()
    worker.cycle()
    assert not list(config.consume.iterdir())


def test_filtered_outputs_publish_then_cleanup_with_consumer_removal(config, ledger, monkeypatch):
    config = dataclasses.replace(config, remove_blank_pages=True, delete_completed_inputs=True)
    source = config.inbox / "duplex.pdf"
    duplex_pdf(source)
    digest = sha256(source)
    worker = Worker(config, ledger, FakeAnalyzer(config))
    worker.cycle()
    assert ledger.job(digest)["status"] == "completed"
    assert not source.exists()
    outputs = ledger.outputs(digest)
    assert len(outputs) == 1 and (outputs[0]["start"], outputs[0]["end"]) == (2, 4)
    for path in config.consume.iterdir():
        with pikepdf.open(path) as pdf:
            assert len(pdf.pages) == 2
        path.unlink()
    worker.recover()
    worker.cycle()
    assert not list(config.consume.iterdir())


def test_dry_run_records_blanks_but_keeps_source(config, ledger):
    config = dataclasses.replace(
        config, remove_blank_pages=True, delete_completed_inputs=True, dry_run=True
    )
    source = config.inbox / "blank.pdf"
    duplex_pdf(source, all_blank=True)
    worker = Worker(config, ledger, FakeAnalyzer(config))
    worker.cycle()
    assert ledger.job(sha256(source))["status"] == "dry_run"
    assert source.exists()
    assert not list(config.consume.iterdir())


@pytest.mark.parametrize("background", [255, 248, 235])
def test_uniform_near_white_scan_and_isolated_specks_are_blank(background):
    with Image.new("RGB", (1000, 1000), (background,) * 3) as image:
        draw = ImageDraw.Draw(image)
        draw.point([(10, 10), (500, 500), (700, 800)], fill="black")
        assert is_blank_page(image, "")


@pytest.mark.parametrize("kind", ["faint", "tiny", "photo", "dark", "border", "text_layer", "ocr"])
def test_nonblank_uncertain_and_faint_content_is_kept(kind):
    with Image.new("RGB", (1000, 1000), "white") as image:
        draw = ImageDraw.Draw(image)
        text = ""
        if kind == "faint":
            draw.text((200, 400), "Faint handwritten information", fill=(247,) * 3, font_size=20)
        elif kind == "tiny":
            draw.text((200, 400), "7", fill="black", font_size=14)
        elif kind == "photo":
            noise = np.random.default_rng(1).integers(0, 256, (1000, 1000, 3), dtype=np.uint8)
            image.paste(Image.fromarray(noise))
        elif kind == "dark":
            draw.rectangle((0, 0, 999, 999), fill=(180,) * 3)
        elif kind == "border":
            draw.line((0, 0, 0, 999), fill="black")
        else:
            text = "7" if kind == "ocr" else "Important text layer"
        assert not is_blank_page(image, text)


def test_retained_page_partition_rejects_loss_duplication_and_invalid_omissions():
    validate_ranges([(2, 4)], 5, [1, 3, 5])
    validate_ranges([], 3, [1, 2, 3])
    for ranges, blank in [
        ([(2, 2)], [1, 3, 5]),
        ([(2, 4), (4, 4)], [1, 3, 5]),
        ([(1, 4)], [1, 3, 5]),
        ([(2, 4)], [1, 1, 3, 5]),
        ([(2, 4)], [1, 3, 6]),
    ]:
        with pytest.raises(UnsafeInput):
            validate_ranges(ranges, 5, blank)

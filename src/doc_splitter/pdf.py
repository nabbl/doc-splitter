import math
import os
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium

from .config import Config
from .fs import UnsafeInput, sha256, sync_dir
from .model import Model, page_ranges


def validate_ranges(ranges: list, count: int) -> None:
    expected = 1
    for start, end in ranges:
        if start != expected or end < start or end > count:
            raise UnsafeInput("ranges are not a contiguous, non-overlapping page partition")
        expected = end + 1
    if expected != count + 1 or not ranges:
        raise UnsafeInput("ranges do not cover every page")


def check_ocr(config: Config) -> None:
    result = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=15, check=True
    )
    available = set(result.stdout.splitlines()[1:])
    missing = set(config.ocr_languages.split("+")) - available
    if missing:
        raise ValueError("missing Tesseract language data: " + ", ".join(sorted(missing)))


def open_pdf(path: Path, max_pages: int):
    try:
        pdf = pikepdf.open(path, attempt_recovery=False, suppress_warnings=True)
    except (pikepdf.PasswordError, pikepdf.PdfError) as error:
        raise UnsafeInput("encrypted, corrupt or unsupported PDF") from error
    if pdf.is_encrypted or not len(pdf.pages) or len(pdf.pages) > max_pages:
        pdf.close()
        raise UnsafeInput(f"encrypted, empty or exceeds configured {max_pages}-page limit")
    if pdf.check_pdf_syntax():
        pdf.close()
        raise UnsafeInput("PDF syntax validation failed")
    return pdf


def render(page, config: Config):
    width, height = page.get_size()
    scale = config.render_dpi / 72
    pixels = math.ceil(width * scale) * math.ceil(height * scale)
    if width <= 0 or height <= 0 or pixels > config.max_render_pixels:
        raise UnsafeInput("page dimensions exceed rendering pixel limit")
    bitmap = page.render(scale=scale)
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def text_or_ocr(page, image, config: Config) -> str:
    with closing(page.get_textpage()) as textpage:
        text = textpage.get_text_bounded()
    if text.strip():
        return text
    with tempfile.TemporaryDirectory(prefix="ocr-", dir=config.work) as temporary:
        png = Path(temporary) / "page.png"
        image.save(png)
        try:
            result = subprocess.run(
                ["tesseract", str(png), "stdout", "-l", config.ocr_languages, "--psm", "3"],
                capture_output=True,
                timeout=config.ocr_timeout,
                check=True,
                text=True,
                env={**os.environ, "OMP_THREAD_LIMIT": str(config.threads)},
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise UnsafeInput("local OCR failed or exceeded its timeout") from error
    return result.stdout


def export_range(source, start: int, end: int, destination: Path) -> None:
    with pikepdf.Pdf.new() as output:
        output.pages.extend(source.pages[start - 1 : end])
        output.save(destination, deterministic_id=True)
    destination.chmod(0o640)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_dir(destination.parent)


def validate_export(source_path: Path, output_path: Path, start: int, end: int) -> None:
    with pikepdf.open(source_path) as source, pikepdf.open(output_path) as output:
        if len(output.pages) != end - start + 1 or output.check_pdf_syntax():
            raise UnsafeInput("export page count or syntax mismatch")
        for index, exported in enumerate(output.pages):
            original = source.pages[start - 1 + index]
            if (
                list(exported.mediabox) != list(original.mediabox)
                or list(exported.cropbox) != list(original.cropbox)
                or int(exported.obj.get("/Rotate", 0)) != int(original.obj.get("/Rotate", 0))
            ):
                raise UnsafeInput("export changed page geometry")
    with (
        closing(pdfium.PdfDocument(source_path)) as source,
        closing(pdfium.PdfDocument(output_path)) as output,
    ):
        for index in range(len(output)):
            with closing(source[start - 1 + index]) as a, closing(output[index]) as b:
                with closing(a.render(scale=1)) as ra, closing(b.render(scale=1)) as rb:
                    if not np.array_equal(ra.to_numpy(), rb.to_numpy()):
                        raise UnsafeInput("export visual comparison failed")


def prepare(config: Config, model: Model, source: Path, job_id: str) -> dict:
    embeddings = []
    with open_pdf(source, config.max_pages) as original:
        count = len(original.pages)
        with closing(pdfium.PdfDocument(source)) as document:
            if len(document) != count:
                raise UnsafeInput("PDF parsers disagree about page count")
            for offset in range(0, count, config.batch_size):
                images, texts = [], []
                try:
                    for index in range(offset, min(count, offset + config.batch_size)):
                        with closing(document[index]) as page:
                            image = render(page, config)
                            images.append(image)
                            texts.append(text_or_ocr(page, image, config))
                    embeddings.append(model.encode(images, texts))
                finally:
                    for image in images:
                        image.close()
        scores = model.boundaries(embeddings)
        if len(scores) != count:
            raise UnsafeInput("inference did not return every page")
        ranges = page_ranges(scores, config.threshold)
        validate_ranges(ranges, count)
        proposed = {
            "page_count": count,
            "scores": scores.tolist(),
            "ranges": ranges,
            "outputs": [],
            "review_required": bool(
                config.review_margin
                and any(
                    abs(score - config.threshold) <= config.review_margin for score in scores[1:]
                )
            ),
        }
        if config.dry_run or proposed["review_required"]:
            return proposed
        for start, end in ranges:
            name = f"{job_id}-p{start:06d}-{end:06d}.pdf"
            stage = config.staging / name
            if stage.exists() or stage.is_symlink():
                raise UnsafeInput("unexpected preexisting stage output")
            export_range(original, start, end, stage)
            validate_export(source, stage, start, end)
            proposed["outputs"].append(
                {
                    "name": name,
                    "start": start,
                    "end": end,
                    "sha256": sha256(stage),
                }
            )
        return proposed

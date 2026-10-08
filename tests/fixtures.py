import hashlib
import json
import sqlite3
from pathlib import Path

import pikepdf
from PIL import Image, ImageDraw

from doc_splitter.fs import atomic_json, fingerprint


def text_pdf(path: Path, pages: int = 1, label: str = "SYNTHETIC TEST", rotate: bool = False):
    with pikepdf.Pdf.new() as pdf:
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
            )
        )
        for index in range(pages):
            page = pdf.add_blank_page(page_size=(360 + index % 2 * 10, 480))
            page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            content = (
                f"BT /F1 16 Tf 24 430 Td ({label} page {index + 1}) Tj "
                "0 -30 Td (Not a real document. Mechanical acceptance only.) Tj ET"
            )
            page.obj.Contents = pdf.make_stream(content.encode("ascii"))
            if rotate:
                page.obj.Rotate = (index % 4) * 90
                page.obj.CropBox = pikepdf.Array([10, 10, 340, 470])
        pdf.save(path, deterministic_id=True)


def image_pdf(path: Path):
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((70, 150), "SYNTHETIC TEST SCAN", fill="black", font_size=38)
    draw.text((70, 230), "Not a real household document", fill="black", font_size=30)
    image.save(path, "PDF", resolution=150)
    image.close()


def duplex_pdf(path: Path, all_blank=False):
    blank = path.with_suffix(".blank.pdf")
    text = path.with_suffix(".text.pdf")
    with Image.new("RGB", (900, 1200), (248, 248, 248)) as image:
        image.save(blank, "PDF", resolution=150)
    text_pdf(text, 2, label="SYNTHETIC DUPLEX", rotate=True)
    try:
        with pikepdf.Pdf.new() as result, pikepdf.open(blank) as back, pikepdf.open(text) as front:
            for index in range(5):
                result.pages.append(
                    back.pages[0] if all_blank or index % 2 == 0 else front.pages[index // 2]
                )
            result.save(path, deterministic_id=True)
    finally:
        blank.unlink()
        text.unlink()


def merged_pdf(path: Path):
    first = path.with_suffix(".invoice.pdf")
    second = path.with_suffix(".letter.pdf")
    text_pdf(first, 2, label="SYNTHETIC INVOICE 123", rotate=True)
    text_pdf(second, 2, label="SYNTHETIC LETTER ABC", rotate=True)
    try:
        with pikepdf.Pdf.new() as merged, pikepdf.open(first) as a, pikepdf.open(second) as b:
            merged.pages.extend(a.pages)
            merged.pages.extend(b.pages)
            merged.save(path, deterministic_id=True)
    finally:
        first.unlink()
        second.unlink()


def legacy_failed_claim(database: Path, source: Path, review: Path):
    signature = json.dumps(fingerprint(source.stat()))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE observations (name TEXT PRIMARY KEY, signature TEXT NOT NULL, "
            "since REAL NOT NULL, handled INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute("INSERT INTO observations VALUES(?,?,0,1)", (source.name, signature))
        connection.execute("PRAGMA user_version=1")
    identifier = hashlib.sha256((source.name + signature).encode()).hexdigest()
    record = review / ("input-" + identifier + ".json")
    atomic_json(
        record,
        {
            "status": "review",
            "source_name": source.name,
            "signature": signature,
            "reason": "claim I/O failure errno=22; fix storage and retry-input",
            "original_retained_in_inbox": True,
        },
    )
    return record

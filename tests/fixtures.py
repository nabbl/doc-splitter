from pathlib import Path

import pikepdf
from PIL import Image, ImageDraw


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

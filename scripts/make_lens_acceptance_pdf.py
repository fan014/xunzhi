from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent.parent / "samples"


def _arrow(draw: ImageDraw.ImageDraw, start, end, color="#1d2430", width=3):
    draw.line([start, end], fill=color, width=width)
    draw.polygon(
        [
            (end[0], end[1]),
            (end[0] - 12, end[1] - 6),
            (end[0] - 12, end[1] + 6),
        ],
        fill=color,
    )


def _lens_page_image(path: Path, condition: str) -> None:
    width, height = 960, 560
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    axis_y = 300
    lens_x = 480
    draw.line([(40, axis_y), (width - 40, axis_y)], fill="#6b7785", width=3)
    draw.line([(lens_x, 60), (lens_x, 500)], fill="#315c93", width=5)
    draw.text((lens_x + 14, 70), "convex lens", fill="#315c93")

    f_left = lens_x - 130
    f_right = lens_x + 130
    twof_left = lens_x - 260
    twof_right = lens_x + 260
    for x, label in ((f_left, "F"), (twof_left, "2F"), (f_right, "F'"), (twof_right, "2F'")):
        draw.ellipse([x - 4, axis_y - 4, x + 4, axis_y + 4], fill="#1d2430")
        draw.text((x - 8, axis_y + 10), label, fill="#1d2430")

    if condition == "u_gt_2f":
        object_x = twof_left - 120
        image_x = lens_x + 190
        object_height = 120
        image_height = 55
    else:
        object_x = lens_x - 190
        image_x = lens_x + 300
        object_height = 80
        image_height = 150

    _arrow(draw, (object_x, axis_y), (object_x, axis_y - object_height))
    draw.text((object_x - 26, axis_y - object_height - 18), "object", fill="#1d2430")
    _arrow(draw, (image_x, axis_y), (image_x, axis_y + image_height), color="#a35b2b")
    draw.text((image_x - 24, axis_y + image_height + 8), "image", fill="#a35b2b")

    draw.line([(object_x, axis_y), (lens_x, axis_y)], fill="#315c93", width=2)
    draw.text(((object_x + lens_x) / 2 - 28, axis_y - 22), "u", fill="#315c93")
    draw.line([(lens_x, axis_y), (image_x, axis_y)], fill="#a35b2b", width=2)
    draw.text(((lens_x + image_x) / 2 - 6, axis_y - 22), "v", fill="#a35b2b")

    if condition == "u_gt_2f":
        caption = "Condition A: u > 2f, image between f and 2f, inverted and smaller"
        result = "Result: real, inverted, diminished image"
    else:
        caption = "Condition B: f < u < 2f, image beyond 2f, inverted and larger"
        result = "Result: real, inverted, enlarged image"
    draw.text((40, 500), caption, fill="#1d2430")
    draw.text((40, 524), result, fill="#1d2430")
    img.save(path, format="PNG")


def build(out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    first = out_dir / "lens_u_gt_2f.png"
    second = out_dir / "lens_f_lt_u_lt_2f.png"
    _lens_page_image(first, "u_gt_2f")
    _lens_page_image(second, "f_lt_u_lt_2f")

    pdf_path = out_dir / "lens_two_conditions.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((40, 40), "Convex Lens Imaging - Two Conditions", fontsize=16)
    page.insert_text((40, 64), "Page 1: object distance u > 2f", fontsize=11)
    page.insert_image(pymupdf.Rect(40, 80, 800, 540), filename=str(first))

    page = doc.new_page(width=842, height=595)
    page.insert_text((40, 40), "Convex Lens Imaging - Two Conditions", fontsize=16)
    page.insert_text((40, 64), "Page 2: object distance f < u < 2f", fontsize=11)
    page.insert_image(pymupdf.Rect(40, 80, 800, 540), filename=str(second))

    doc.save(pdf_path)
    doc.close()
    return pdf_path


def main() -> None:
    path = build()
    print(path)


if __name__ == "__main__":
    main()
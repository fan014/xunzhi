from __future__ import annotations

import io
from dataclasses import dataclass, field

import pymupdf


@dataclass
class PdfBlock:
    text: str
    rect: list[float]


@dataclass
class PdfPage:
    page_no: int
    char_count: int
    has_text: bool
    is_scanned: bool
    blocks: list[PdfBlock] = field(default_factory=list)


def open_document(path: str):
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"pdf unreadable: {exc}") from exc
    if doc.page_count <= 0:
        doc.close()
        raise ValueError("pdf has no pages")
    return doc


def open_document_bytes(data: bytes):
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"pdf unreadable: {exc}") from exc
    if doc.page_count <= 0:
        doc.close()
        raise ValueError("pdf has no pages")
    return doc


def page_count(doc) -> int:
    return doc.page_count


def read_outline(doc) -> list[tuple[int, str, int]]:
    try:
        raw = doc.get_toc(simple=True)
    except Exception:  # noqa: BLE001
        return []
    rows: list[tuple[int, str, int]] = []
    for entry in raw or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        level, title, page = entry[0], entry[1], entry[2]
        title = str(title or "").strip()
        if not title or not isinstance(page, int) or page < 1:
            continue
        rows.append((int(level or 1), title, page))
    return rows


def validate_range(start: int | None, end: int | None, total: int, max_pages: int = 40) -> tuple[int | None, int | None]:
    if start is None and end is None:
        if total > max_pages:
            raise ValueError(f"page range required when total pages exceed {max_pages}")
        return None, None
    if start is None or end is None:
        raise ValueError("page_start and page_end must be provided together")
    if not (1 <= start <= end <= total):
        raise ValueError(f"page range must be within 1..{total}")
    if end - start + 1 > max_pages:
        raise ValueError(f"page range must not exceed {max_pages} pages")
    return start, end


def _normalized(value: float, total: float) -> float:
    return round(min(1.0, max(0.0, value / total if total else 0.0)), 4)


def extract_page(page, page_no: int, scanned_char_threshold: int = 30) -> PdfPage:
    raw = page.get_text("blocks")
    width = float(page.rect.width) or 1.0
    height = float(page.rect.height) or 1.0
    blocks: list[PdfBlock] = []
    char_count = 0
    for item in raw:
        text = (item[4] or "").strip()
        if not text:
            continue
        x0, y0, x1, y1 = item[:4]
        rect = [
            _normalized(x0, width),
            _normalized(y0, height),
            _normalized(x1, width),
            _normalized(y1, height),
        ]
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            continue
        blocks.append(PdfBlock(text=text, rect=rect))
        char_count += len(text)
    has_text = char_count >= scanned_char_threshold
    covered = 0.0
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                covered += max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    except Exception:
        covered = 0.0
    is_scanned = (not has_text) and covered / (width * height) > 0.5
    return PdfPage(page_no, char_count, has_text, is_scanned, blocks)


def render_page(page, max_side: int = 1600) -> bytes:
    rect = page.rect
    longest = max(float(rect.width), float(rect.height)) or 1.0
    zoom = min(2.5, max(1.0, max_side / longest))
    matrix = pymupdf.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return pix.tobytes("png")


def render_thumb(page, max_side: int = 360) -> bytes:
    rect = page.rect
    longest = max(float(rect.width), float(rect.height)) or 1.0
    zoom = max(0.2, max_side / longest)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def page_image_boxes(page) -> list[list[float]]:
    width = float(page.rect.width) or 1.0
    height = float(page.rect.height) or 1.0
    boxes: list[list[float]] = []
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if not bbox:
                continue
            box = [
                _normalized(bbox[0], width),
                _normalized(bbox[1], height),
                _normalized(bbox[2], width),
                _normalized(bbox[3], height),
            ]
            if box[2] > box[0] and box[3] > box[1]:
                boxes.append(box)
    except Exception:
        return []
    return boxes


def open_bytes(path: str) -> io.BytesIO:
    with open(path, "rb") as fh:
        return io.BytesIO(fh.read())
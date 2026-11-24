from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TextBlock:
    text: str
    start: int
    end: int
    ordinal: int
    rects: list[list[float]] = field(default_factory=list)


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("text is not decodable as utf-8 or gbk")


def _pieces(text: str, max_chars: int) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text):
        chunk = match.group(0)
        if not chunk.strip():
            continue
        if len(chunk) <= max_chars:
            pieces.append((match.start(), match.end()))
            continue
        for sentence in re.finditer(r"[^。！？；.!?;\n]+[。！？；.!?;]?", chunk):
            start = match.start() + sentence.start()
            end = match.start() + sentence.end()
            if not text[start:end].strip():
                continue
            if end - start <= max_chars:
                pieces.append((start, end))
                continue
            cursor = start
            while cursor < end:
                pieces.append((cursor, min(end, cursor + max_chars)))
                cursor += max_chars
    return pieces


def split_text(text: str, min_chars: int = 300, max_chars: int = 800) -> list[TextBlock]:
    if not text.strip():
        raise ValueError("text is empty")
    blocks: list[TextBlock] = []
    buffer_start: int | None = None
    buffer_end = 0

    def flush() -> None:
        nonlocal buffer_start, buffer_end
        if buffer_start is None:
            return
        start = buffer_start
        end = buffer_end
        while start < end and text[start] in "\r\n":
            start += 1
        while end > start and text[end - 1] in "\r\n":
            end -= 1
        body = text[start:end]
        if body.strip():
            ordinal = len(blocks) + 1
            height = min(0.08, max(0.02, len(body) / 4000))
            blocks.append(
                TextBlock(
                    text=body,
                    start=start,
                    end=end,
                    ordinal=ordinal,
                    rects=[
                        [
                            0.06,
                            round(0.1 + 0.02 * ordinal, 4),
                            0.94,
                            round(0.1 + 0.02 * ordinal + height, 4),
                        ]
                    ],
                )
            )
        buffer_start = None
        buffer_end = 0

    for start, end in _pieces(text, max_chars):
        if buffer_start is None:
            buffer_start, buffer_end = start, end
            continue
        length = (buffer_end - buffer_start) + (end - start)
        if length > max_chars and (buffer_end - buffer_start) >= min_chars:
            flush()
            buffer_start, buffer_end = start, end
        else:
            buffer_end = end
    flush()
    return blocks


def estimate_page_rect(ordinal: int) -> list[float]:
    top = 0.08 + (ordinal % 12) * 0.07
    return [0.06, round(min(0.9, top), 4), 0.94, round(min(0.98, top + 0.06), 4)]
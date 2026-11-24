from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
IMAGES = ROOT / "images"
PDF = ROOT / "pdf"
TEXT = ROOT / "text"
BROKEN = ROOT / "broken"


def topology_image() -> Image.Image:
    img = Image.new("RGB", (900, 560), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 70, 200, 150], outline="#1f6b4a", width=3)
    draw.text((75, 100), "PC-A", fill="#17211c")
    draw.rectangle([40, 360, 200, 440], outline="#1f6b4a", width=3)
    draw.text((75, 390), "PC-B", fill="#17211c")
    draw.rectangle([620, 70, 800, 150], outline="#1f6b4a", width=3)
    draw.text((655, 100), "PC-C", fill="#17211c")
    draw.rectangle([330, 230, 500, 300], outline="#2f6fa8", width=3)
    draw.text((370, 255), "SW1", fill="#17211c")
    draw.rectangle([640, 330, 820, 410], outline="#a35b2b", width=3)
    draw.text((690, 360), "R1", fill="#17211c")

    draw.line([200, 110, 330, 250], fill="#5b6b62", width=3)
    draw.line([200, 400, 330, 280], fill="#5b6b62", width=3)
    draw.line([500, 285, 640, 350], fill="#5b6b62", width=3)
    draw.line([730, 330, 720, 150], fill="#5b6b62", width=3)

    draw.text((60, 220), "192.168.1.0/24", fill="#5b6b62")
    draw.text((760, 210), "192.168.2.0/24", fill="#5b6b62")
    draw.text((60, 500), "Topology: two subnets joined by router R1; PC-A and PC-B share SW1", fill="#5b6b62")
    return img


def write_images() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    img = topology_image()
    img.save(IMAGES / "topology.png", format="PNG")
    img.save(IMAGES / "topology.jpg", format="JPEG", quality=90)
    img.save(IMAGES / "topology.webp", format="WEBP", quality=90)


def write_pdf() -> None:
    PDF.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Computer Networks - Switching Basics", fontsize=18)
    page.insert_text((72, 140), "A switch forwards frames by destination MAC address.", fontsize=11)
    page.insert_text((72, 165), "Hosts in the same subnet share one broadcast domain.", fontsize=11)
    page.insert_text((72, 190), "PC-A and PC-B are connected to SW1.", fontsize=11)

    vector = doc.new_page(width=595, height=842)
    vector.insert_text((72, 90), "Subnet topology (vector drawing)", fontsize=16)
    vector.draw_rect(pymupdf.Rect(80, 150, 200, 230), color=(0.12, 0.42, 0.29), width=2)
    vector.draw_rect(pymupdf.Rect(360, 400, 500, 480), color=(0.18, 0.44, 0.66), width=2)
    vector.draw_line(pymupdf.Point(200, 190), pymupdf.Point(360, 440), color=(0.35, 0.4, 0.38), width=2)
    vector.insert_text((100, 195), "PC-A", fontsize=11)
    vector.insert_text((385, 445), "SW1", fontsize=11)
    vector.insert_text((230, 190), "192.168.1.0/24", fontsize=10)

    scan = doc.new_page(width=595, height=842)
    scan_image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(scan_image)
    draw.rectangle([120, 200, 520, 320], outline="#1f6b4a", width=8)
    draw.rectangle([700, 700, 1080, 860], outline="#2f6fa8", width=8)
    draw.line([520, 260, 700, 740], fill="#5b6b62", width=8)
    draw.text((160, 240), "PC-A", fill="#17211c")
    draw.text((760, 760), "SW1", fill="#17211c")
    buffer = io.BytesIO()
    scan_image.save(buffer, format="PNG")
    scan.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=buffer.getvalue())

    doc.save(PDF / "sample_3pages.pdf")
    doc.close()


def write_text() -> None:
    TEXT.mkdir(parents=True, exist_ok=True)
    note = (
        "交换机转发基础\n\n"
        "交换机根据目的 MAC 地址在同一网段内转发帧。\n\n"
        "同一网段内的主机通过交换机直接通信，不需要经过路由器。\n\n"
        "跨网段通信时，主机需要把数据交给默认网关，由路由器转发到目标网段。\n\n"
        "常见混淆：把“跨设备”当成“跨网段”。PC-A 与 PC-B 虽然不同设备，但同接 SW1，仍属同一网段。\n"
    )
    (TEXT / "note.txt").write_text(note, encoding="utf-8")
    (TEXT / "note.md").write_text("# 交换机转发基础\n\n" + note, encoding="utf-8")
    (TEXT / "note_gbk.txt").write_bytes(note.encode("gbk"))


def write_broken() -> None:
    BROKEN.mkdir(parents=True, exist_ok=True)
    (BROKEN / "fake.pdf").write_bytes(b"not a real pdf at all")
    (BROKEN / "empty.txt").write_bytes(b"")


def main() -> None:
    write_images()
    write_pdf()
    write_text()
    write_broken()
    print("fixtures written under", ROOT)


if __name__ == "__main__":
    main()
"""真实 DeepSeek 验收入口（隔离、可选 A/B）。

设计约束：
- 在导入 app.config / app.main / 数据库 / 模型模块之前，强制把数据目录切到程序自建的临时目录；
  不尊重外部 XUNZHI_DATA_DIR，避免触及个人记录。
- 默认只跑一组快速冒烟；--ab 复用同一套资料，仅改变 progress_text，跑场景 A/B。
- 无 Key 直接安全退出：不创建数据目录、不发送请求、不输出密钥。
- 报告只在显式 --report 路径写出；退出时清理临时目录。

用法：
  .venv\\Scripts\\python scripts\\smoke_real.py
  .venv\\Scripts\\python scripts\\smoke_real.py --ab
  .venv\\Scripts\\python scripts\\smoke_real.py --ab --review --report <路径>
  .venv\\Scripts\\python scripts\\smoke_real.py --ab --report "%TEMP%\\xunzhi-ab-report.json"
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SMOKE_PREFIX = "xunzhi-smoke-"
TOPOLOGY = ROOT / "tests" / "fixtures" / "images" / "topology.png"
NOTE = ROOT / "tests" / "fixtures" / "text" / "note.txt"

PROGRESS_A = "我学完了交换机转发，还没学路由协议。同一网段和跨网段发包容易混，想用这张图练一下。"
PROGRESS_B = "我刚开始学习，只想认清图中的设备和作用。"

REVIEW_PAGE_START = 12
REVIEW_PAGE_END = 28
REVIEW_QUESTION_COUNT = 5
REVIEW_DEVICE_PAGE = 28
REVIEW_SAMPLE_SEED = 20261120
REVIEW_PNG_NAME = "review_topology.png"
REVIEW_EXTRA = "保持我前面所说的学习范围。请各安排一道必须通过观察实际连线才可作答的题：一道看 PDF 物理第 28 页的彩色设备连线，一道看独立拓扑 PNG 的连线；单纯问颜色图例不算。图上的结论引用对应图的实际证据编号，标记读图的每题至少引用所用图片，文字图例不替代图像引用。其余题按当前进度。"

TERMINAL = {"succeeded", "failed", "insufficient_evidence", "cancelled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实 DeepSeek 隔离验收（不进入自动测试）")
    parser.add_argument("--ab", action="store_true", help="同资料不同进度 A/B 两组")
    parser.add_argument("--review", action="store_true", help="正式质量评审输入：PDF 12–28 页 + 拓扑图，A/B 各 5 题")
    parser.add_argument("--report", type=Path, default=None, help="显式写出 JSON 评测报告")
    parser.add_argument("--review-assets", type=Path, default=None, help="可选：保留评审 PDF/PNG 的目录")
    parser.add_argument("--timeout", type=float, default=180.0, help="每组生成的有界等待秒数")
    args = parser.parse_args()
    if args.review and not args.ab:
        parser.error("--review 需要与 --ab 一起使用")
    return args


def question_count_for(args: argparse.Namespace) -> int:
    return REVIEW_QUESTION_COUNT if args.review else 3


def load_dotenv_only() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(ROOT / ".env")


def prepare_isolated_env() -> Path:
    """必须在导入 app 之前调用；强制覆盖外部 XUNZHI_DATA_DIR。"""
    root = Path(tempfile.mkdtemp(prefix=SMOKE_PREFIX)).resolve()
    os.environ["XUNZHI_DATA_DIR"] = str(root)
    os.environ["XUNZHI_ALLOW_FAKE"] = "0"
    os.environ.pop("XUNZHI_FAKE_MODE", None)
    os.environ["XUNZHI_INSTANCE_ID"] = f"smoke-{uuid.uuid4().hex[:8]}"
    return root


def cleanup_isolated_env(root: Path) -> bool:
    target = Path(root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if target.parent != temp_root or not target.name.startswith(SMOKE_PREFIX):
        return False
    if not target.exists():
        return True
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


REVIEW_PAGE_TEXT = [
    "Device identity: hosts send and receive data; a switch connects local devices; a router joins different networks. Legend: blue squares are hosts; green rectangle is a switch; orange rectangle is a router; lines are direct links.",
    "Device roles: a switch forwards frames inside a local network; a router forwards between networks; hosts initiate and receive communication.",
    "Direct links: a drawing may show hosts connected to a local device; treat only drawn connections as present and do not infer links that are not shown.",
    "Same subnet: hosts that share a subnet can communicate through a local switch; compare their address ranges to decide.",
    "Different subnet: when the destination is outside the local subnet, traffic requires a gateway and a router; follow the actual drawing for the path.",
    "Default gateway: a host sends traffic to its gateway when the destination is outside its local subnet.",
    "Switch forwarding: a switch uses the destination MAC address to choose the local port.",
    "MAC table: a switch records MAC address to port mappings for local forwarding.",
    "Local address resolution: a host resolves a local IP address to a link address for the local segment only; this does not carry traffic between networks.",
    "Glossary: a switch connects local devices; a router joins different networks; a gateway is the local exit.",
    "Reading the diagram: identify each device from its label and role; do not assume a connection that is not drawn.",
    "Reading the diagram: a direct line describes only the two endpoints it touches; it does not establish a subnet by itself.",
    "Device role check: a switch and a router have different roles; location alone does not define a role.",
    "Same subnet check: compare local address ranges; a same-subnet path uses a switch, not a router.",
    "Different subnet check: a cross-subnet path uses a gateway and a router; do not expand beyond the stated scope.",
    "Common confusion: a cable between two devices does not by itself mean the same subnet.",
    "Summary: identify devices, roles, direct links, same or different subnet, gateway, and local forwarding.",
]


def generate_review_assets(base_dir: Path) -> tuple[Path, Path]:
    """在隔离临时目录生成评审专用 PDF 与拓扑 PNG，不依赖个人资料。"""
    import pymupdf
    from PIL import Image, ImageDraw

    assets = Path(base_dir) / "review_inputs"
    assets.mkdir(parents=True, exist_ok=True)
    pdf_path = assets / "network_handbook.pdf"
    png_path = assets / "review_topology.png"

    doc = pymupdf.open()
    for page_no in range(1, 29):
        page = doc.new_page(width=595, height=842)
        if page_no == REVIEW_DEVICE_PAGE:
            blue = (0.18, 0.44, 0.66)
            green = (0.12, 0.42, 0.29)
            orange = (0.64, 0.36, 0.17)
            page.draw_rect(pymupdf.Rect(70, 140, 170, 220), color=blue, fill=blue, width=2)
            page.draw_rect(pymupdf.Rect(70, 480, 170, 560), color=blue, fill=blue, width=2)
            page.draw_rect(pymupdf.Rect(250, 260, 380, 360), color=green, fill=green, width=2)
            page.draw_rect(pymupdf.Rect(470, 120, 580, 210), color=orange, fill=orange, width=2)
            page.draw_rect(pymupdf.Rect(470, 500, 580, 590), color=blue, fill=blue, width=2)
            page.draw_line(pymupdf.Point(170, 180), pymupdf.Point(250, 290), color=(0.2, 0.2, 0.2), width=4)
            page.draw_line(pymupdf.Point(170, 520), pymupdf.Point(250, 330), color=(0.2, 0.2, 0.2), width=4)
            page.draw_line(pymupdf.Point(380, 300), pymupdf.Point(470, 170), color=(0.2, 0.2, 0.2), width=4)
            page.draw_line(pymupdf.Point(525, 210), pymupdf.Point(525, 500), color=(0.2, 0.2, 0.2), width=4)
            continue
        if REVIEW_PAGE_START <= page_no <= REVIEW_PAGE_END:
            index = page_no - REVIEW_PAGE_START
            page.insert_text((54, 70), f"Review handout - page {page_no}", fontsize=15)
            page.insert_textbox(
                pymupdf.Rect(54, 110, 540, 760),
                REVIEW_PAGE_TEXT[index],
                fontsize=12,
                lineheight=1.4,
            )
        else:
            page.insert_text((54, 90), "Scope-excluded comparison page.", fontsize=13)
            page.insert_textbox(
                pymupdf.Rect(54, 120, 540, 760),
                "This page is outside the selected physical pages 12-28 and is not used in this review round.",
                fontsize=11,
            )
    doc.save(pdf_path)
    doc.close()

    image = Image.new("RGB", (1000, 620), "white")
    draw = ImageDraw.Draw(image)
    boxes = {
        "PC-A": (40, 70, 200, 150),
        "PC-C": (40, 300, 200, 380),
        "SW1": (330, 180, 500, 260),
        "R1": (660, 230, 830, 310),
        "PC-B": (800, 430, 960, 510),
    }
    for name, box in boxes.items():
        color = "#2f6fa8" if name.startswith("PC-") else "#1f6b4a" if name == "SW1" else "#a35b2b"
        draw.rectangle(box, outline=color, width=4)
        draw.text((box[0] + 28, box[1] + 30), name, fill="#17211c")
    draw.line([200, 110, 330, 220], fill="#5b6b62", width=4)
    draw.line([200, 340, 330, 240], fill="#5b6b62", width=4)
    draw.line([500, 230, 660, 270], fill="#5b6b62", width=4)
    draw.line([745, 310, 880, 430], fill="#5b6b62", width=4)
    draw.text((210, 175), "192.168.1.0/24", fill="#5b6b62")
    draw.text((680, 175), "192.168.2.0/24", fill="#5b6b62")
    draw.text((40, 570), "PC-A and PC-C share SW1; SW1 connects to R1; PC-B is on R1's other side.", fill="#5b6b62")
    image.save(png_path, format="PNG")
    return pdf_path, png_path


def review_sources(client, base_dir: Path) -> tuple[list[dict], dict[str, tuple[int, int]]]:
    pdf_path, png_path = generate_review_assets(base_dir)
    source_pdf = upload(client, pdf_path)
    source_png = upload(client, png_path)
    return [source_pdf, source_png], {
        source_pdf["id"]: (REVIEW_PAGE_START, REVIEW_PAGE_END)
    }


def upload(client, path: Path) -> dict:
    with open(path, "rb") as fh:
        response = client.post(
            "/api/sources", files={"file": (path.name, fh, "application/octet-stream")}
        )
    if response.status_code != 201:
        raise RuntimeError(f"上传失败 {path.name}: {response.status_code}")
    return response.json()["source"]


def wait_for_job(client, job_id: str, timeout: float) -> tuple[dict, float]:
    started = time.monotonic()
    deadline = started + max(1.0, timeout)
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in TERMINAL:
            return job, round(time.monotonic() - started, 2)
        time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))
    client.post(f"/api/jobs/{job_id}/cancel")
    return {"status": "timeout", "job_id": job_id, "pack_id": None}, round(time.monotonic() - started, 2)


def citation_meta(client, citation: dict, source_names: set[str]) -> dict:
    response = client.get(f"/api/citations/{citation['id']}")
    payload = response.json()
    if response.status_code != 200 or payload.get("status") != "ok":
        raise RuntimeError(f"引用不可用: {citation['id']} -> {payload.get('status')}")
    source_name = payload.get("source_name")
    if source_name not in source_names:
        raise RuntimeError(f"引用来源不在本次资料内: {source_name}")
    asset_expected = bool(payload.get("asset_expected"))
    asset = None
    if asset_expected:
        asset = client.get(f"/api/citations/{citation['id']}/asset")
        asset_openable = (
            asset.status_code == 200
            and (asset.headers.get("content-type") or "").startswith("image/")
        )
    else:
        asset_openable = None
    return {
        "citation_id": citation["id"],
        "kind": payload.get("kind"),
        "label": payload.get("label"),
        "claim": payload.get("claim"),
        "source_name": source_name,
        "page_no": payload.get("page_no"),
        "quote": payload.get("quote"),
        "boxes": payload.get("boxes") or [],
        "scanned": payload.get("scanned", False),
        "image_description": payload.get("image_description"),
        "text_alternative": payload.get("text_alternative"),
        "text_alternative_source": payload.get("text_alternative_source"),
        "asset_expected": asset_expected,
        "asset_openable": asset_openable,
        "asset_status": asset.status_code if asset is not None else None,
        "asset_mime": asset.headers.get("content-type") if asset is not None else None,
        "asset_bytes": len(asset.content) if asset is not None else None,
    }


def evaluate(
    client,
    sources: list[dict],
    progress: str,
    timeout: float,
    question_count: int = 3,
    page_ranges: dict[str, tuple[int, int]] | None = None,
    require_image_question: bool = False,
) -> dict:
    source_names = {item["display_name"] for item in sources}
    page_ranges = page_ranges or {}
    source_specs = []
    for item in sources:
        start, end = page_ranges.get(item["id"], (None, None))
        source_specs.append(
            {
                "source_id": item["id"],
                "page_start": start,
                "page_end": end,
            }
        )
    created = client.post(
        "/api/packs",
        json={
            "idempotency_key": f"smoke-{uuid.uuid4().hex}",
            "sources": source_specs,
            "progress_text": progress,
            "difficulty": "consolidate",
            "question_count": question_count,
            "consent_ack": True,
        },
    )
    if created.status_code != 202:
        return {"status": "request_rejected", "http_status": created.status_code, "progress_text": progress}

    job_id = created.json()["job_id"]
    job, elapsed = wait_for_job(client, job_id, timeout)
    if job["status"] != "succeeded":
        return {
            "status": job["status"],
            "error": job.get("error"),
            "elapsed_s": elapsed,
            "progress_text": progress,
        }

    pack_id = job["pack_id"]
    before = client.get(f"/api/packs/{pack_id}").json()
    hidden_before_submit = (not before.get("answer_revealed")) and all(
        "answer_index" not in item and "explanation" not in item for item in before["questions"]
    )

    submitted = client.post(
        f"/api/packs/{pack_id}/submit",
        json={"idempotency_key": f"smoke-submit-{uuid.uuid4().hex}", "answers": []},
    )
    if submitted.status_code != 200:
        return {"status": "submit_failed", "http_status": submitted.status_code, "progress_text": progress}
    result = submitted.json()
    detail = client.get(f"/api/packs/{pack_id}").json()
    detail_questions = {item["id"]: item for item in detail.get("questions", [])}

    questions = result.get("questions") or []
    checks = {
        "question_count_match": len(questions) == question_count,
        "four_distinct_options": all(
            len({option["text"] for option in item.get("options", [])}) == 4 for item in questions
        ),
        "answer_index_in_range": all(0 <= item["answer_index"] <= 3 for item in questions),
        "explanations_present": all(bool(item.get("explanation")) for item in questions),
        "hidden_before_submit": hidden_before_submit,
        "citations_mapped": False,
        "assets_openable": False,
    }

    meta_by_id: dict[str, dict] = {}
    for item in questions:
        for citation in item.get("citations", []):
            if citation["id"] not in meta_by_id:
                meta_by_id[citation["id"]] = citation_meta(client, citation, source_names)
    for section in detail.get("handout", []):
        for citation in section.get("citations", []):
            if citation["id"] not in meta_by_id:
                meta_by_id[citation["id"]] = citation_meta(client, citation, source_names)
    mapped_citations = list(meta_by_id.values())
    checks["citations_mapped"] = bool(mapped_citations) and all(
        citation["source_name"] in source_names for citation in mapped_citations
    )
    asset_citations = [citation for citation in mapped_citations if citation["asset_expected"]]
    checks["assets_openable"] = all(
        citation["asset_openable"] is True for citation in asset_citations
    )
    if require_image_question:
        image_questions = [
            item
            for item in questions
            if detail_questions.get(item.get("question_id"), {}).get("is_image_based")
        ]
        checks["has_image_question"] = bool(image_questions)
        checks["image_question_has_image_ref"] = bool(image_questions) and any(
            any(
                meta_by_id.get(citation["id"], {}).get("kind") in {"page_image", "image_region"}
                for citation in item.get("citations", [])
            )
            for item in image_questions
        )

    def brief(citation_id: str) -> dict:
        meta = meta_by_id[citation_id]
        return {
            "citation_id": meta["citation_id"],
            "type": meta["kind"],
            "source_name": meta["source_name"],
            "page_no": meta["page_no"],
            "quote": meta["quote"],
            "claim": meta.get("claim"),
            "image_description": meta.get("image_description"),
            "text_alternative": meta.get("text_alternative"),
            "scanned": meta.get("scanned", False),
            "boxes": meta["boxes"],
            "asset_expected": meta["asset_expected"],
            "asset_openable": meta["asset_openable"],
            "asset_status": meta["asset_status"],
            "asset_mime": meta["asset_mime"],
            "asset_bytes": meta["asset_bytes"],
        }

    handout = [
        {
            "section_type": section.get("section_type"),
            "heading": section.get("heading"),
            "body": section.get("body"),
            "citations": [brief(citation["id"]) for citation in section.get("citations", [])],
        }
        for section in detail.get("handout", [])
    ]
    evidence_sources = sorted({item["source_name"] for item in mapped_citations})
    pages = sorted(
        {
            item["page_no"]
            for item in mapped_citations
            if item["page_no"] is not None
        }
    )

    return {
        "status": "succeeded",
        "elapsed_s": elapsed,
        "job_id": job_id,
        "pack_id": pack_id,
        "progress_text": progress,
        "difficulty": "consolidate",
        "question_count": question_count,
        "scope": detail.get("scope"),
        "sources": detail.get("sources", []),
        "handout": handout,
        "questions": [
            {
                "ordinal": item["ordinal"],
                "stem": item["stem"],
                "options": item["options"],
                "answer_index": item["answer_index"],
                "explanation": item["explanation"],
                "knowledge_point": item.get("knowledge_point"),
                "is_image_based": detail_questions.get(item.get("question_id"), {}).get("is_image_based"),
                "citations": [brief(citation["id"]) for citation in item.get("citations", [])],
            }
            for item in questions
        ],
        "score": result.get("score"),
        "checks": checks,
        "evidence_sources": evidence_sources,
        "evidence_pages": pages,
    }


def compare_ab(run_a: dict, run_b: dict) -> dict:
    if run_a.get("status") != "succeeded" or run_b.get("status") != "succeeded":
        return {"available": False, "reason": "任一组未成功生成，无法比较"}
    bodies_a = [section.get("body") or "" for section in run_a["handout"]]
    bodies_b = [section.get("body") or "" for section in run_b["handout"]]
    stems_a = [item["stem"] for item in run_a["questions"]]
    stems_b = [item["stem"] for item in run_b["questions"]]
    changed_sections = sum(1 for left, right in zip_longest(bodies_a, bodies_b) if left != right)
    changed_questions = sum(1 for left, right in zip_longest(stems_a, stems_b) if left != right)
    return {
        "available": True,
        "changed_handout_sections": changed_sections,
        "changed_questions": changed_questions,
        "same_evidence_source_set": set(run_a["evidence_sources"]) == set(run_b["evidence_sources"]),
        "note": "自动只检查结构、引用映射与可观察差异数量；贴合度与差异质量待人工评测",
    }


def build_l3_sample(run: dict, seed: int = REVIEW_SAMPLE_SEED) -> dict:
    if run.get("status") != "succeeded":
        return {"ready": False, "seed": seed, "items": [], "missing": ["该学习包未成功生成"]}
    candidates: list[dict] = []
    seen: set[str] = set()
    for section in run.get("handout", []):
        for citation in section.get("citations", []):
            citation_id = citation.get("citation_id")
            if not citation_id or citation_id in seen:
                continue
            seen.add(citation_id)
            candidates.append(
                {
                    **citation,
                    "bucket": "handout",
                    "context": {
                        "section_type": section.get("section_type"),
                        "heading": section.get("heading"),
                        "body": section.get("body"),
                    },
                }
            )
    for question in run.get("questions", []):
        for citation in question.get("citations", []):
            citation_id = citation.get("citation_id")
            if not citation_id or citation_id in seen:
                continue
            seen.add(citation_id)
            candidates.append(
                {
                    **citation,
                    "bucket": "question",
                    "context": {
                        "ordinal": question.get("ordinal"),
                        "stem": question.get("stem"),
                        "explanation": question.get("explanation"),
                    },
                }
            )

    required_types = ("text_span", "page_image", "image_region")
    eligible_candidates = [
        candidate
        for candidate in candidates
        if (
            candidate.get("type") == "text_span"
            or (
                candidate.get("type") == "page_image"
                and candidate.get("page_no") == REVIEW_DEVICE_PAGE
            )
            or (
                candidate.get("type") == "image_region"
                and candidate.get("page_no") is None
                and candidate.get("source_name") == REVIEW_PNG_NAME
            )
        )
    ]
    candidates = eligible_candidates
    missing: list[str] = []
    for kind in required_types:
        if not any(candidate.get("type") == kind for candidate in candidates):
            detail = (
                "（必须来自物理第 28 页整页图）"
                if kind == "page_image"
                else "（必须来自独立拓扑 PNG 的图像区域）"
                if kind == "image_region"
                else ""
            )
            missing.append(f"缺少类型: {kind}{detail}")

    rng = random.Random(seed)
    selected: list[dict] = []
    handout_count = 0
    question_count = 0
    for kind in required_types:
        choices = [
            candidate
            for candidate in candidates
            if candidate.get("type") == kind
            and candidate["citation_id"] not in {item["citation_id"] for item in selected}
            and (
                (candidate["bucket"] == "handout" and handout_count < 5)
                or (candidate["bucket"] == "question" and question_count < 5)
            )
        ]
        if not choices:
            continue
        choice = choices[rng.randrange(len(choices))]
        selected.append(choice)
        if choice["bucket"] == "handout":
            handout_count += 1
        else:
            question_count += 1

    for bucket, target in (("handout", 5), ("question", 5)):
        count = sum(1 for item in selected if item["bucket"] == bucket)
        remaining = [
            candidate
            for candidate in candidates
            if candidate["bucket"] == bucket
            and candidate["citation_id"] not in {item["citation_id"] for item in selected}
        ]
        need = target - count
        if need > 0:
            if len(remaining) < need:
                missing.append(f"{'讲义' if bucket == 'handout' else '题目'}候选不足，无法凑齐 {target} 条")
            else:
                selected.extend(rng.sample(remaining, need))

    handout_count = sum(1 for item in selected if item["bucket"] == "handout")
    question_count = sum(1 for item in selected if item["bucket"] == "question")
    selected_types = {item.get("type") for item in selected}
    ready = (
        handout_count == 5
        and question_count == 5
        and all(kind in selected_types for kind in required_types)
        and len({item["citation_id"] for item in selected}) == 10
        and not missing
    )
    return {
        "ready": ready,
        "seed": seed,
        "items": selected,
        "missing": missing,
        "note": "ready 仅表示同一学习包已具备 5+5、三类覆盖的可人工抽样列表；不代表 H/L/D 评分。",
    }


def write_report(path: Path, payload: dict) -> None:
    target = path.resolve()

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".xunzhi-report-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def dispose_isolated_engine() -> bool:
    """释放本进程的 SQLAlchemy engine，确保临时 SQLite 文件可被删除。"""
    try:
        from app.infra.db import engine

        engine.dispose()
        return True
    except Exception:
        return False


def main() -> int:
    args = parse_args()
    load_dotenv_only()
    if not (os.getenv("DEEPSEEK_API_KEY") or "").strip():
        print("[smoke] 未配置 DEEPSEEK_API_KEY：未发送请求，未读取用户资料。")
        return 2
    if not args.review and (not TOPOLOGY.exists() or not NOTE.exists()):
        print("[smoke] 缺少自制样例；请先运行 tests/fixtures/make_fixtures.py。")
        return 2

    try:
        temp_root = prepare_isolated_env()
    except Exception:
        print("[smoke] 无法创建隔离临时目录。")
        return 1
    payload = None
    failure_code = 1
    engine_disposed = False
    expected_success = 2 if args.ab else 1
    question_count = question_count_for(args)
    progress_a = PROGRESS_A + (f" {REVIEW_EXTRA}" if args.review else "")
    progress_b = PROGRESS_B + (f" {REVIEW_EXTRA}" if args.review else "")
    review_assets_root = args.review_assets if args.review_assets else temp_root
    try:
        from fastapi.testclient import TestClient

        from app.config import settings
        from app.main import app

        try:
            with TestClient(app) as client:
                if args.review:
                    sources, page_ranges = review_sources(client, review_assets_root)
                else:
                    sources, page_ranges = [upload(client, TOPOLOGY), upload(client, NOTE)], {}
                run_a = evaluate(
                    client,
                    sources,
                    progress_a,
                    args.timeout,
                    question_count=question_count,
                    page_ranges=page_ranges,
                    require_image_question=args.review,
                )
                if args.ab:
                    run_b = evaluate(
                        client,
                        sources,
                        progress_b,
                        args.timeout,
                        question_count=question_count,
                        page_ranges=page_ranges,
                        require_image_question=args.review,
                    )
                    result = {"runs": {"A": run_a, "B": run_b}, "comparison": compare_ab(run_a, run_b)}
                else:
                    result = {"runs": {"A": run_a}, "comparison": None}
        finally:
            engine_disposed = dispose_isolated_engine()

        if args.review:
            for run in result["runs"].values():
                if run.get("status") == "succeeded":
                    sample = build_l3_sample(run)
                    run["l3_sample"] = sample
                    run["checks"]["l3_sample_ready"] = sample["ready"]

        checks_ok = all(
            item.get("status") == "succeeded" and all(item.get("checks", {}).values())
            for item in result["runs"].values()
        )
        payload = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "mode": "review" if args.review else ("ab" if args.ab else "smoke"),
            "model": settings.model,
            "key_configured": True,
            "checks_ok": checks_ok,
            "isolation": {
                "ephemeral_data_dir": True,
                "user_data_untouched": True,
                "engine_disposed": engine_disposed,
                "cleanup_on_exit": None,
                "cleanup_ok": None,
            },
            "timeout_s": args.timeout,
            "human_review_pending": [
                "H-1 讲义是否贴合进度",
                "H-2 已学是否略写",
                "H-3 未学是否不展开",
                "H-4 薄弱点是否为主",
                "H-5 术语是否匹配",
                "L3 引用支持度抽样",
                "D1–D6 质量认定",
                "A/B 可观察差异",
            ],
            **result,
        }
        if args.review:
            payload["review"] = {
                "pdf_page_range": [REVIEW_PAGE_START, REVIEW_PAGE_END],
                "question_count": REVIEW_QUESTION_COUNT,
                "difficulty": "consolidate",
                "sampling_seed": REVIEW_SAMPLE_SEED,
                "assets_retained": bool(args.review_assets),
                "inputs": result["runs"]["A"].get("sources", []),
                "l3_status": "not_evaluated",
                "formal_scoring": "pending",
                "note": "引用可定位不等于 L3 支持度；本报告不填写人工评分。",
            }
    except KeyboardInterrupt:
        print("[smoke] 已取消；临时数据目录将清理。")
        failure_code = 130
    except Exception as exc:
        print(f"[smoke] 失败：{type(exc).__name__}")
        failure_code = 1
    finally:
        cleanup_ok = cleanup_isolated_env(temp_root)

    if payload is None:
        print(f"[smoke] 临时目录清理{'成功' if cleanup_ok else '失败'}。")
        return 1 if not cleanup_ok else failure_code

    payload["isolation"]["cleanup_ok"] = cleanup_ok
    payload["isolation"]["cleanup_on_exit"] = cleanup_ok
    if not cleanup_ok:
        payload["isolation"]["cleanup_error"] = "临时目录仍存在，可能被文件句柄占用"

    statuses = [item.get("status") for item in payload["runs"].values()]
    overall_ok = (
        checks_ok
        and statuses.count("succeeded") == expected_success
        and engine_disposed
        and cleanup_ok
    )
    if args.report:
        try:
            write_report(args.report, payload)
        except Exception:
            print("[smoke] 报告写入失败；评测结果未视为通过。")
            return 1
    print(
        json.dumps(
            {
                "mode": payload["mode"],
                "statuses": statuses,
                "questions": {key: len(value.get("questions", [])) for key, value in payload["runs"].items()},
                "comparison": payload.get("comparison"),
                "checks_ok": checks_ok,
                "engine_disposed": engine_disposed,
                "cleanup_ok": cleanup_ok,
                "report": str(args.report.resolve()) if args.report else None,
                "human_review_pending": "见报告或本脚本固定输出",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

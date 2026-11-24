from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "tests" / ".ui-shift-data"
PORT = 8031
BASE = f"http://127.0.0.1:{PORT}"

os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ["DEEPSEEK_API_KEY"] = ""

import httpx
import uvicorn
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from app.domain import schedule as schedule_service
from app.domain.models import (
    Evidence,
    LearningPack,
    LearningPlanVersion,
    PackPlanVersion,
    PackSource,
    Question,
    ScheduleTask,
    SourceMaterial,
    now_iso,
)
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from app.main import app
from tests.stage4_helpers import make_png

SHOTS = ROOT / "screenshots"


def server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    uvicorn.Server(config).run()


def wait_health(client: httpx.Client) -> None:
    for _ in range(80):
        try:
            if client.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not start")


def modules():
    return [
        {
            "module_id": f"mod_ui_{index}",
            "title": f"模块{index}",
            "knowledge_point_ids": [f"kp_ui_{index}_1", f"kp_ui_{index}_2"],
            "core_points": [f"概念{index}一", f"概念{index}二"],
            "suggested_question_count": 4,
            "source_names": ["日历验收资料"],
        }
        for index in range(1, 4)
    ]


def seed_pack(db, source, version_id):
    pack = LearningPack(
        id=new_id("pack"),
        display_title="日历验收学习内容",
        source_kind="real",
        difficulty="consolidate",
        question_count=12,
        job_id=f"job_ui_{time.time_ns()}",
        handout_word_count=0,
        model_name="test",
        prompt_version="test",
        created_at=now_iso(),
    )
    db.add(pack)
    db.flush()
    stored = db.get(SourceMaterial, source["id"])

    db.add(
        PackSource(
            pack_id=pack.id,
            source_id=source["id"],
            page_start=None,
            page_end=None,
            page_count_used=None,
            source_name_snapshot=stored.display_name,
            source_sha256_snapshot=stored.sha256,
            source_kind_snapshot=stored.kind,
        )
    )
    questions = []
    ordinal = 1
    for module in modules():
        for index in range(4):
            stem = f"{module['title']} 第 {index + 1} 题"
            question = Question(
                id=new_id("question"),
                pack_id=pack.id,
                ordinal=ordinal,
                stem=stem,
                options_json=json.dumps(
                    [
                        {"key": "A", "text": "选项 A"},
                        {"key": "B", "text": "选项 B"},
                        {"key": "C", "text": "选项 C"},
                        {"key": "D", "text": "选项 D"},
                    ],
                    ensure_ascii=False,
                ),
                answer_index=0,
                explanation="验收解析",
                knowledge_point=module["title"],
                module_id=module["module_id"],
                difficulty_tag="consolidate",
                is_image_based=False,
                stem_hash=hashlib.sha256(f"{pack.id}-{stem}".encode("utf-8")).hexdigest(),
                created_at=now_iso(),
            )
            db.add(question)
            questions.append(question)
            ordinal += 1
    db.add(
        PackPlanVersion(
            pack_id=pack.id,
            plan_version_id=version_id,
            adopted_at=now_iso(),
        )
    )
    db.commit()
    return pack.id, questions


def main() -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(exist_ok=True)
    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    report = {"base": BASE, "screenshots": [], "checks": {}}
    with httpx.Client(base_url=BASE, timeout=60) as client:
        wait_health(client)
        today = schedule_service.today_local()
        image = make_png(DATA_DIR / "calendar_ui.png", (44, 122, 88))
        source = client.post(
            "/api/sources",
            files={"file": ("calendar_ui.png", image.read_bytes(), "image/png")},
        ).json()["source"]
        plan = client.post(
            "/api/plans",
            json={
                "title": "日历网页验收计划",
                "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
                "goal": "验证继续学习、前移、改期与简报",
                "points": [],
                "question_count": 12,
            },
        ).json()
        version_id = plan["current_version_id"]
        with SessionLocal() as db:
            version = db.get(LearningPlanVersion, version_id)
            version.modules_json = json.dumps(modules(), ensure_ascii=False)
            version.question_count = 12
            db.commit()
            pack_id, questions = seed_pack(db, source, version_id)
        confirmed = client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm")
        assert confirmed.status_code == 200, confirmed.text
        saved = client.post(
            f"/api/plans/{plan['id']}/versions/{version_id}/schedule",
            json={
                "start_date": today.isoformat(),
                "duration_days": 14,
                "daily_minutes": 30,
            },
        )
        assert saved.status_code == 200, saved.text
        by_date: dict[str, list[dict]] = {}
        with SessionLocal() as db:
            for row in db.execute(
                ScheduleTask.__table__.select().where(ScheduleTask.plan_version_id == version_id)
            ).mappings():
                by_date.setdefault(row["scheduled_date"], []).append(dict(row))
        day1 = by_date[today.isoformat()]
        day2 = by_date[(today + timedelta(days=1)).isoformat()]
        reading = next(item for item in day1 if item["task_type"] == "reading")
        practice = next(item for item in day2 if item["task_type"] == "practice")
        review = next(item for item in day2 if item["task_type"] != "practice")

        assert client.post(f"/api/schedule/tasks/{reading['id']}/complete").status_code == 200
        page = client.get(f"/api/packs/{pack_id}?task_id={practice['id']}").json()
        ids = [item["id"] for item in page["questions"]]
        submitted = client.post(
            f"/api/packs/{pack_id}/submit",
            json={
                "idempotency_key": f"ui-submit-{time.time_ns()}",
                "task_id": practice["id"],
                "answers": [{"question_id": qid, "selected_index": 0} for qid in ids],
            },
        )
        assert submitted.status_code == 200, submitted.text
        assert client.post(f"/api/schedule/tasks/{review['id']}/complete").status_code == 200

        calendar = client.get(
            f"/api/schedule?plan_id={plan['id']}&date={today.isoformat()}"
        ).json()
        report["checks"]["shift_available"] = calendar["shift"]["available"]
        report["checks"]["shift_days"] = calendar["shift"]["shift_days"]
        report["checks"]["today_all_done"] = calendar["today_all_done"]
        report["checks"]["next_task"] = bool(calendar["next_task"])
        report["checks"]["brief_summary"] = calendar["brief"]["summary"]
        report["checks"]["brief_has_advice"] = bool(calendar["brief"]["advice"])
        assert calendar["shift"]["available"] is True
        assert calendar["today_all_done"] is True
        assert calendar["next_task"]
        assert calendar["brief"]["practice"]["total_questions"] == 4

        from selenium.webdriver.edge.options import Options as EdgeOptions

        edge = EdgeOptions()
        edge.add_argument("--headless=new")
        edge.add_argument("--window-size=1280,900")
        driver = webdriver.Edge(options=edge)
        wait = WebDriverWait(driver, 20)
        try:
            driver.get(f"{BASE}/calendar?plan_id={plan['id']}&date={today.isoformat()}")
            wait.until(lambda d: d.find_element(By.ID, "shift-panel").is_displayed())
            driver.save_screenshot(str(SHOTS / "phase2_shift_before.png"))
            report["screenshots"].append("screenshots/phase2_shift_before.png")

            driver.find_element(By.ID, "shift-view").click()
            wait.until(lambda d: d.find_element(By.ID, "shift-confirm").is_displayed())
            changes = driver.find_element(By.ID, "shift-changes").text
            report["checks"]["shift_changes_text"] = changes
            assert "→" in changes
            driver.save_screenshot(str(SHOTS / "phase2_shift_preview.png"))
            report["screenshots"].append("screenshots/phase2_shift_preview.png")

            driver.find_element(By.ID, "shift-confirm").click()
            wait.until(lambda d: not d.find_element(By.ID, "shift-panel").is_displayed())
            driver.save_screenshot(str(SHOTS / "phase2_shift_after.png"))
            report["screenshots"].append("screenshots/phase2_shift_after.png")

            after = client.get(f"/api/schedule?plan_id={plan['id']}").json()
            report["checks"]["shift_applied"] = True
            report["checks"]["dates_after"] = sorted(after["month_days"].keys())

            tomorrow = (today + timedelta(days=1)).isoformat()
            driver.get(f"{BASE}/calendar?plan_id={plan['id']}&date={tomorrow}")
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".schedule-task-actions button"))
            reschedule_buttons = driver.find_elements(
                By.CSS_SELECTOR, ".schedule-task-actions button"
            )
            target_button = next(
                (button for button in reschedule_buttons if button.text.strip() == "改期"),
                None,
            )
            assert target_button is not None
            target_button.click()
            wait.until(lambda d: d.find_element(By.ID, "reschedule-dialog").is_displayed())
            target = (today + timedelta(days=3)).isoformat()
            driver.execute_script(
                "const input=document.getElementById('reschedule-date');"
                f"input.value='{target}';"
                "input.dispatchEvent(new Event('change'));"
            )
            wait.until(lambda d: "目标日期" in d.find_element(By.ID, "reschedule-preview").text)
            report["checks"]["reschedule_preview"] = driver.find_element(
                By.ID, "reschedule-preview"
            ).text
            driver.save_screenshot(str(SHOTS / "phase2_reschedule_dialog.png"))
            report["screenshots"].append("screenshots/phase2_reschedule_dialog.png")
            driver.find_element(By.ID, "reschedule-save").click()
            wait.until(lambda d: not d.find_element(By.ID, "reschedule-dialog").is_displayed())
            wait.until(lambda d: "已移至" in d.find_element(By.ID, "calendar-feedback").text)
            report["checks"]["reschedule_feedback"] = driver.find_element(
                By.ID, "calendar-feedback"
            ).text
            driver.save_screenshot(str(SHOTS / "phase2_reschedule_after.png"))
            report["screenshots"].append("screenshots/phase2_reschedule_after.png")

            driver.get(f"{BASE}/calendar?plan_id={plan['id']}&date={today.isoformat()}")
            wait.until(lambda d: d.find_element(By.ID, "brief-panel").is_displayed())
            brief = driver.find_element(By.ID, "brief-panel")
            driver.execute_script("arguments[0].scrollIntoView();", brief)
            report["checks"]["brief_text"] = driver.find_element(By.ID, "brief-summary").text
            report["checks"]["brief_stats"] = driver.find_element(By.ID, "brief-stats").text
            report["checks"]["continue_next"] = bool(
                driver.find_elements(By.XPATH, "//a[contains(text(), '继续下一项')]")
            )
            report["checks"]["brief_button"] = bool(
                driver.find_elements(By.XPATH, "//button[contains(text(), '看看今天学得怎么样')]")
            )
            driver.save_screenshot(str(SHOTS / "phase2_brief.png"))
            report["screenshots"].append("screenshots/phase2_brief.png")
        finally:
            driver.quit()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
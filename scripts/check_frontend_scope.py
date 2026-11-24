from __future__ import annotations

import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import httpx
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

BASE = "http://127.0.0.1:8010"
DB = "data_acceptance_long/xunzhi.sqlite3"


def _browser():
    from selenium.webdriver.edge.options import Options as EdgeOptions

    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=390,844")
    return webdriver.Edge(options=options)


def main() -> None:
    con = sqlite3.connect(DB)
    kb_id = con.execute("select id from knowledge_base where name like '%46%' order by created_at desc").fetchone()[0]
    con.close()
    detail = httpx.get(f"{BASE}/api/knowledge-bases/{kb_id}", timeout=60).json()
    item = next(row for row in detail["items"] if row["display_name"].startswith("course_2"))
    chapter = next(ch for ch in item["chapters"] if ch["page_start"] <= 67 <= ch["page_end"])

    driver = _browser()
    wait = WebDriverWait(driver, 20)
    result = {}
    try:
        url = (
            f"{BASE}/?kb={kb_id}&source={item['source_id']}"
            f"&start={chapter['page_start']}&end={chapter['page_end']}"
        )
        driver.get(url)
        wait.until(lambda d: d.find_element(By.ID, "kb-scope-group").is_displayed())
        wait.until(lambda d: d.find_element(By.ID, "kb-items").find_elements(By.CSS_SELECTOR, ".kb-choice"))
        result["selected_radio"] = driver.find_element(
            By.CSS_SELECTOR, 'input[name="kb_scope"][value="selected"]'
        ).is_selected()
        result["kb_select_value"] = driver.find_element(By.ID, "kb-select").get_attribute("value")
        checked = [
            box
            for box in driver.find_elements(By.CSS_SELECTOR, "#kb-items .kb-choice input[type=checkbox]")
            if box.is_selected()
        ]
        result["checked_source_rows"] = len(checked)
        result["checked_row_text"] = checked[0].find_element(By.XPATH, "..").text if checked else ""
        ranges = driver.find_elements(By.CSS_SELECTOR, "#kb-items .range input[type=number]")
        result["range_values"] = [field.get_attribute("value") for field in ranges]
        result["chapter_buttons"] = len(driver.find_elements(By.CSS_SELECTOR, "#kb-items .chapter"))

        driver.find_element(By.CSS_SELECTOR, 'input[name="kb_scope"][value="all"]').click()
        wait.until(
            lambda d: not d.find_elements(By.CSS_SELECTOR, "#kb-items .range")
        )
        result["ranges_after_all"] = len(driver.find_elements(By.CSS_SELECTOR, "#kb-items .range"))
        result["datasets_after_all"] = len(
            driver.find_elements(By.CSS_SELECTOR, "#kb-items .kb-choice")
        )

        driver.get(f"{BASE}/knowledge-bases/{kb_id}")
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#kb-items .chapter"))
        first_chapter = driver.find_elements(By.CSS_SELECTOR, "#kb-items .chapter")[0]
        first_label = first_chapter.text
        first_chapter.click()
        wait.until(lambda d: "/?kb=" in d.current_url)
        query = parse_qs(urlparse(driver.current_url).query)
        result["detail_chapter_click"] = {
            "label": first_label,
            "kb": query.get("kb", [None])[0],
            "source": query.get("source", [None])[0],
            "start": query.get("start", [None])[0],
            "end": query.get("end", [None])[0],
        }
    finally:
        driver.quit()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
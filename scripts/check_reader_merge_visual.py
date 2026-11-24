from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "reader_merge_fixture.html"
IMAGE = ROOT / "tests" / ".reader_fixture_topology.png"
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)


def make_topology():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1600, 1000), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 48)
        small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
        small = font
    # 大面积白边中放置小拓扑，验证区域裁剪能把节点和标签放大。
    draw.rectangle((450, 350, 1150, 700), outline="black", width=5)
    draw.rectangle((500, 400, 650, 490), fill="#eaf1fa", outline="#315c93", width=4)
    draw.text((515, 420), "PC-A", fill="#1d2430", font=small)
    draw.rectangle((775, 440, 925, 550), fill="#f3f7fc", outline="#315c93", width=4)
    draw.text((805, 475), "SW1", fill="#1d2430", font=font)
    draw.rectangle((1000, 400, 1150, 490), fill="#eaf1fa", outline="#315c93", width=4)
    draw.text((1015, 420), "PC-B", fill="#1d2430", font=small)
    draw.line((650, 445, 775, 485), fill="#315c93", width=5)
    draw.line((925, 485, 1000, 445), fill="#315c93", width=5)
    draw.text((690, 430), "Gi0/1", fill="#315c93", font=small)
    draw.text((930, 430), "Gi0/2", fill="#315c93", font=small)
    image.save(IMAGE)


def main():
    make_topology()
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    options = webdriver.EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--allow-file-access-from-files")
    options.add_argument("--window-size=1280,900")
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 20)
    report = {"screenshots": [], "checks": {}}
    try:
        driver.get(FIXTURE.as_uri())
        wait.until(lambda d: d.find_element(By.ID, "reader-title").text)
        report["checks"]["merged_page_count"] = driver.find_element(By.ID, "reader-count").text
        report["checks"]["merged_title"] = driver.find_element(By.ID, "reader-title").text
        report["checks"]["current_page_id"] = driver.execute_script(
            "return window.XunzhiReader.currentPageId()"
        )
        report["checks"]["subheads"] = [
            item.text for item in driver.find_elements(By.CSS_SELECTOR, "#reader-text h3")
        ]
        report["checks"]["folded_sources"] = len(
            driver.find_elements(By.CSS_SELECTOR, "#reader-text details.page-sources")
        )
        report["checks"]["source_rows"] = len(
            driver.find_elements(By.CSS_SELECTOR, "#reader-text .page-source-row")
        )
        modes = driver.find_elements(By.CSS_SELECTOR, ".visual-mode")
        report["checks"]["modes"] = [item.text for item in modes]
        assert len(modes) == 2
        modes[1].click()
        next_step = next(item for item in driver.find_elements(By.CSS_SELECTOR, ".visual-controls button") if item.text.strip() == "下一步")
        next_step.click()
        wait.until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, ".visual-edge.active")) >= 1
        )
        report["checks"]["active_edges_first_step"] = len(
            driver.find_elements(By.CSS_SELECTOR, ".visual-edge.active")
        )
        next_step = next(item for item in driver.find_elements(By.CSS_SELECTOR, ".visual-controls button") if item.text.strip() == "下一步")
        next_step.click()
        wait.until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, ".visual-edge.active")) >= 2
        )
        report["checks"]["active_edges"] = len(
            driver.find_elements(By.CSS_SELECTOR, ".visual-edge.active")
        )
        driver.execute_script(
            "const s=document.querySelector('.visual-quiz select'); s.value='flood';"
        )
        driver.find_element(By.CSS_SELECTOR, ".visual-quiz button").click()
        wait.until(lambda d: d.find_element(By.CSS_SELECTOR, ".visual-feedback").text)
        report["checks"]["feedback"] = driver.find_element(
            By.CSS_SELECTOR, ".visual-feedback"
        ).text
        assert "正确" in report["checks"]["feedback"]
        driver.save_screenshot(str(SHOTS / "phase3c_switch_interaction.png"))
        report["screenshots"].append("screenshots/phase3c_switch_interaction.png")

        driver.execute_script("window.XunzhiReader.render('mod_img')")
        wait.until(lambda d: d.find_element(By.ID, "reader-title").text == "拓扑图中的 PC-A、SW1、PC-B")
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".source-asset-frame.cropped"))
        report["checks"]["cropped"] = True
        report["checks"]["asset_buttons"] = [
            item.text
            for item in driver.find_elements(By.CSS_SELECTOR, ".asset-controls button")
        ]
        driver.save_screenshot(str(SHOTS / "phase3c_source_crop.png"))
        report["screenshots"].append("screenshots/phase3c_source_crop.png")
        driver.find_element(By.XPATH, "//button[contains(text(), '放大')]").click()
        report["checks"]["full_after_zoom"] = driver.execute_script(
            "return document.querySelector('.source-asset-frame').classList.contains('show-full')"
        )
        driver.save_screenshot(str(SHOTS / "phase3c_source_full.png"))
        report["screenshots"].append("screenshots/phase3c_source_full.png")
    finally:
        driver.quit()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
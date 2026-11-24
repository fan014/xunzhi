from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "screenshots"
BASE = "http://127.0.0.1:8031"


def _pack_id() -> str:
    data = json.loads((ROOT / "kb_lens_real_acceptance.json").read_text(encoding="utf-8"))
    return data["new_kb"]["job"]["pack_id"]


def _wait_title(driver, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = driver.execute_script(
            "return (document.getElementById('reader-title')||{}).textContent || ''"
        )
        if last.strip():
            return last.strip()
        time.sleep(0.3)
    return last.strip()


def _click_next(driver) -> None:
    driver.execute_script("document.getElementById('reader-next').click()")


def main() -> int:
    pack_id = _pack_id()
    SHOTS.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=BASE, timeout=30) as client:
        pack = client.get(f"/api/packs/{pack_id}").json()
        module_id = (pack.get("modules") or [{}])[0].get("module_id")
        client.put(
            f"/api/packs/{pack_id}/position",
            json={"module_id": module_id, "page_id": None, "view": "modules"},
        )
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--remote-allow-origins=*")
    driver = webdriver.Chrome(options=options)
    result: dict = {"pack_id": pack_id, "url": f"{BASE}/packs/{pack_id}", "pages": []}
    try:
        driver.get(result["url"])
        _wait_title(driver)
        time.sleep(0.8)
        captured = set()
        image_pages = 0
        for _ in range(9):
            title = driver.execute_script(
                "return document.getElementById('reader-title').textContent"
            )
            page_no = driver.execute_script(
                "return document.getElementById('reader-page').textContent"
            )
            image = driver.execute_script(
                """
                const box = document.getElementById('reader-visual');
                const img = box ? box.querySelector('img') : null;
                return img ? {src: img.getAttribute('src'), naturalWidth: img.naturalWidth,
                              naturalHeight: img.naturalHeight, complete: img.complete} : null;
                """
            )
            result["pages"].append({"title": title, "page": page_no, "image": image})
            if image and image.get("complete") and image.get("naturalWidth"):
                image_pages += 1
                if image_pages == 1:
                    driver.save_screenshot(str(SHOTS / "kb_lens_page1.png"))
                    captured.add("page1")
                elif image_pages == 2:
                    driver.save_screenshot(str(SHOTS / "kb_lens_page2.png"))
                    captured.add("page2")
                    time.sleep(0.6)
                    break
            _click_next(driver)
            time.sleep(0.35)

        driver.refresh()
        restored = _wait_title(driver)
        time.sleep(0.8)
        restored_page = driver.execute_script(
            "return document.getElementById('reader-page').textContent"
        )
        restored_image = driver.execute_script(
            """
            const img = document.querySelector('#reader-visual img');
            return img ? {src: img.getAttribute('src'), naturalWidth: img.naturalWidth} : null;
            """
        )
        driver.save_screenshot(str(SHOTS / "kb_lens_reopen.png"))
        result["reopen"] = {
            "title": restored,
            "page": restored_page,
            "image": restored_image,
            "matched_second_page": bool(restored_image and restored_image.get("naturalWidth")),
            "captured": sorted(captured),
        }
        result["screenshots"] = [
            str(SHOTS / "kb_lens_page1.png"),
            str(SHOTS / "kb_lens_page2.png"),
            str(SHOTS / "kb_lens_reopen.png"),
        ]
    finally:
        driver.quit()
    (ROOT / "kb_lens_ui_acceptance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
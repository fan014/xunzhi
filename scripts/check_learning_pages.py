from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def api(client: httpx.Client, path: str):
    response = client.get(f"{BASE}{path}")
    if response.status_code >= 400:
        raise RuntimeError(f"GET {path} -> {response.status_code} {response.text[:500]}")
    return response.json()


def main(pack_id: str) -> None:
    with httpx.Client(timeout=30) as client:
        detail = api(client, f"/api/packs/{pack_id}")
        rows = []
        print(f"pack={detail['id']} title={detail['display_title']} modules={len(detail['modules'])}")
        for module in detail["modules"]:
            print(f"\nmodule={module['module_id']} title={module.get('title')} pages={len(module.get('pages') or [])}")
            for index, page in enumerate(module.get("pages") or [], start=1):
                visual = page.get("visual") or {}
                page_type = page.get("page_type") or visual.get("type") or "text"
                print(f"  {index}. {page['page_id']} [{page_type}] {page.get('title')}")
                if page_type == "comparison":
                    for card in visual.get("cards") or []:
                        print(f"     card: {card.get('title')} | {card.get('condition')} -> {card.get('result')}")
                if page_type == "source_asset":
                    url = visual.get("asset_url")
                    status = None
                    if url:
                        response = client.get(f"{BASE}{url}")
                        status = response.status_code
                    rows.append(
                        {
                            "page_id": page["page_id"],
                            "source_ref": visual.get("source_ref"),
                            "citation_id": visual.get("citation_id"),
                            "source_name": visual.get("source_name"),
                            "page_no": visual.get("page_no"),
                            "boxes": visual.get("boxes"),
                            "asset_url": url,
                            "http_status": status,
                        }
                    )
                    print(
                        f"     source_asset ref={visual.get('source_ref')} citation={visual.get('citation_id')} "
                        f"page={visual.get('page_no')} status={status}"
                    )
            recap = module.get("recap") or {}
            print(f"  recap summary={recap.get('summary')}")
            for rule in recap.get("rules") or []:
                print(f"     rule: {rule.get('condition')} -> {rule.get('result')} | 误区: {rule.get('pitfall')}")
            if recap.get("cross_module"):
                print(f"     cross_module: {recap.get('cross_module')}")
        print("\nSOURCE_ASSET_TABLE")
        print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("用法：python scripts/check_learning_pages.py <pack_id>")
    main(sys.argv[1])
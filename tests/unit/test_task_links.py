from __future__ import annotations

from app.api.routes import _attach_task_pages


def _task(task_type, module_id="mod_a", point_ids=None, url=None):
    return {
        "task_type": task_type,
        "module_id": module_id,
        "knowledge_point_ids": point_ids or ["kp_a"],
        "action_url": url or f"/packs/pack_1?module_id={module_id}&view=modules",
    }


def test_task_links_use_type_specific_targets():
    reading = _task("reading", point_ids=["kp_a", "kp_b"])
    review = _task("review", point_ids=["kp_a"])
    practice = _task(
        "practice",
        url="/packs/pack_1?task_id=sch_1&view=questions",
    )
    page_index = {
        "mod_a": [
            {"page_id": "opportunity-cost-mechanism", "knowledge_point_ids": ["kp_a"]},
            {"page_id": "sunk-cost-mechanism", "knowledge_point_ids": ["kp_b"]},
        ]
    }
    _attach_task_pages([reading, review, practice], page_index)

    assert reading["action_url"].endswith(
        "module_id=mod_a&view=modules&page_id=opportunity-cost-mechanism"
    )
    assert reading["page_target"] == "page"
    assert review["action_url"].endswith("page_id=recap-mod_a")
    assert review["page_target"] == "recap"
    assert practice["action_url"] == "/packs/pack_1?task_id=sch_1&view=questions"
    assert "page_target" not in practice


def test_old_content_review_still_targets_derived_recap():
    review = _task("review", module_id="mod_legacy")
    _attach_task_pages([review], {})
    assert review["action_url"].endswith("module_id=mod_legacy&view=modules&page_id=recap-mod_legacy")


def test_review_without_module_keeps_module_entry():
    review = _task("review", module_id="")
    _attach_task_pages([review], {})
    assert review["action_url"] == "/packs/pack_1?module_id=&view=modules"


def test_reading_without_page_index_falls_back_to_module():
    reading = _task("reading")
    _attach_task_pages([reading], {})
    assert reading["action_url"] == "/packs/pack_1?module_id=mod_a&view=modules"
    assert "page_target" not in reading
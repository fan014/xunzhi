from __future__ import annotations

from sqlalchemy import delete

from app.domain.models import AppSetting
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _reset_learning_positions() -> None:
    with SessionLocal() as db:
        db.execute(
            delete(AppSetting).where(
                AppSetting.key.in_(["learning.positions", "learning.last"])
            )
        )
        db.commit()


def test_position_and_last_learning_track_draft_and_fallback(client, tmp_path):
    _reset_learning_positions()
    _, first = helpers.generate_pack(client, tmp_path, color=(20, 120, 80))
    _, second = helpers.generate_pack(client, tmp_path, color=(80, 60, 160))
    question = second["questions"][0]
    client.put(
        f"/api/packs/{first['id']}/position",
        json={"module_id": None, "view": "questions"},
    )

    saved = client.put(f"/api/packs/{second['id']}/draft", json={"answers": [
        {"question_id": question["id"], "selected_index": 0}
    ]})
    assert saved.status_code == 200, saved.text
    position = client.put(
        f"/api/packs/{second['id']}/position",
        json={"module_id": None, "view": "questions"},
    )
    assert position.status_code == 200, position.text
    detail = client.get(f"/api/packs/{second['id']}").json()
    assert detail["position"]["view"] == "questions"

    last = client.get("/api/learning/last").json()
    assert last["available"] is True
    assert last["pack_id"] == second["id"]
    assert last["view"] == "questions"
    assert last["answer_state"] == "draft"
    assert last["answered_count"] == 1
    assert last["url"] == f"/packs/{second['id']}"

    deleted = client.request("DELETE", f"/api/packs/{second['id']}", json={"confirm": True})
    assert deleted.status_code == 200, deleted.text
    fallback = client.get("/api/learning/last").json()
    assert fallback["available"] is True
    assert fallback["fallback"] is True
    assert fallback["pack_id"] == first["id"]
    assert fallback["view"] == "questions"
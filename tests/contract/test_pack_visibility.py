from __future__ import annotations

from tests.stage4_helpers import generate_pack, submit_wrong_once


def test_unsubmitted_detail_never_reveals_answers(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    assert pack["answer_revealed"] is False
    for question in pack["questions"]:
        assert "answer_index" not in question
        assert "explanation" not in question
        assert "citations" not in question

    draft = client.put(
        f"/api/packs/{pack['id']}/draft",
        json={"answers": [{"question_id": pack["questions"][0]["id"], "selected_index": 2}]},
    )
    assert draft.status_code == 200
    assert "answer_index" not in draft.text
    assert "explanation" not in draft.text


def test_submitted_detail_reveals_answers_and_citation_after_pack_delete_404(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    submit_wrong_once(client, pack)
    revealed = client.get(f"/api/packs/{pack['id']}")
    assert revealed.status_code == 200
    payload = revealed.json()
    assert payload["answer_revealed"] is True
    assert all("answer_index" in item for item in payload["questions"])
    citation = next(
        item["citations"][0] for item in payload["questions"] if item.get("citations")
    )
    assert client.get(f"/api/citations/{citation['id']}").status_code == 200

    client.request("DELETE", f"/api/packs/{pack['id']}", json={"confirm": True})
    assert client.get(f"/api/citations/{citation['id']}").status_code == 404
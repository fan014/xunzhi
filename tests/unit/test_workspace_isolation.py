from __future__ import annotations

from pathlib import Path

from app.infra import workspace


def test_default_user_takes_over_existing_root(tmp_path: Path):
    registry = workspace.ensure_registry(tmp_path)
    assert registry["current_user_id"] == workspace.DEFAULT_USER_ID
    default = next(item for item in registry["users"] if item["is_default"])
    assert workspace.workspace_path(default, tmp_path) == tmp_path.resolve()
    assert workspace.users_path(tmp_path).exists()


def test_new_user_gets_isolated_workspace_and_stable_id(tmp_path: Path):
    workspace.ensure_registry(tmp_path)
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "a.txt").write_text("A", encoding="utf-8")
    created = workspace.create_user("学习者 B", tmp_path)
    target = workspace.workspace_path(created, tmp_path)
    assert target == (tmp_path / "users" / created["id"]).resolve()
    assert target.exists()
    assert not (target / "sources" / "a.txt").exists()
    assert not (target / "xunzhi.sqlite3").exists()

    renamed = workspace.rename_user_by_id(created["id"], "学习者 B2", tmp_path)
    assert renamed["id"] == created["id"]
    assert renamed["workspace"] == created["workspace"]
    registry = workspace.load_registry(tmp_path)
    assert registry["current_user_id"] == workspace.DEFAULT_USER_ID
    assert any(item["display_name"] == "学习者 B2" for item in registry["users"])


def test_validate_workspace_and_missing_user(tmp_path: Path):
    workspace.ensure_registry(tmp_path)
    created = workspace.create_user("C", tmp_path)
    ok, message = workspace.validate_workspace(created, tmp_path)
    assert ok and message == ""
    assert workspace.user_by_id("missing", tmp_path) is None
    try:
        workspace.set_current("missing", tmp_path)
    except ValueError as exc:
        assert str(exc) == "user_not_found"
    else:
        raise AssertionError("missing user should fail")
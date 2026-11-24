from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine

from app.domain.models import Base
from app.infra import migrations
from app.infra.migrations import MigrationError, TARGET_SCHEMA_VERSION, migrate


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'schema.sqlite').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    return engine


def _columns(engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}


def _versions(engine) -> list[str]:
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("SELECT version FROM schema_migration ORDER BY version").fetchall()
    return [row[0] for row in rows]


def test_launch_state_column_on_new_db(tmp_path):
    engine = _engine(tmp_path)
    migrate(engine)
    assert "launch_state" in _columns(engine, "generation_job")
    assert TARGET_SCHEMA_VERSION == "11"
    versions = _versions(engine)
    assert versions.count("4") == 1


def test_launch_state_added_to_registered_v4_db(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE generation_job DROP COLUMN launch_state")
        conn.exec_driver_sql(
            "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
            "VALUES ('4', '2026-11-22T00:00:00Z', 0, 0, 'legacy v4')"
        )
    assert "launch_state" not in _columns(engine, "generation_job")
    migrate(engine)
    assert "launch_state" in _columns(engine, "generation_job")
    assert _versions(engine).count("4") == 1
    assert TARGET_SCHEMA_VERSION == "11"


def test_migrate_twice_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    first = migrate(engine)
    second = migrate(engine)
    assert first["version"] == "11"
    assert second["version"] == "11"
    assert second["applied"] is False
    assert _versions(engine).count("4") == 1


def test_launch_state_migration_failure_raises_explicit_code(tmp_path, monkeypatch):
    engine = _engine(tmp_path)

    def boom(conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(migrations, "_ensure_v4_columns", boom)
    with pytest.raises(MigrationError) as exc:
        migrate(engine)
    assert exc.value.code == "launch_state_migration_failed"


def test_missing_column_after_ensure_raises(tmp_path, monkeypatch):
    engine = _engine(tmp_path)

    def noop(conn):
        return None

    monkeypatch.setattr(migrations, "_ensure_v4_columns", noop)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE generation_job DROP COLUMN launch_state")
    with pytest.raises(MigrationError) as exc:
        migrate(engine)
    assert exc.value.code == "launch_state_column_missing"
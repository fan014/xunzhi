from __future__ import annotations

from sqlalchemy import create_engine, text

from app.domain.models import Base
from app.infra.migrations import migrate, read_schema_state


def test_schedule_migration_preserves_existing_plan_rows(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'v10.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE schedule_task")
        conn.exec_driver_sql("ALTER TABLE learning_plan_version DROP COLUMN schedule_json")
        conn.execute(
            text(
                "INSERT INTO learning_plan(id, title, status, created_at, updated_at) "
                "VALUES ('plan_v10', '旧计划', 'confirmed', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO learning_plan_version("
                "id, plan_id, status, origin, adjustment_json, sources_json, scope_json, "
                "items_json, created_at, confirmed_at"
                ") VALUES ('pv_v10', 'plan_v10', 'confirmed', 'manual', '[]', '[]', '{}', '[]', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )

    result = migrate(engine)
    assert result["version"] == "11"
    assert result["applied"] is True
    assert read_schema_state(engine)["schema_version"] == "11"

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(learning_plan_version)")
        }
        task_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(schedule_task)")
        }
        attempt_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(attempt)")
        }
        indexes = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'")
        }
        row = conn.exec_driver_sql(
            "SELECT id, schedule_json FROM learning_plan_version WHERE id = 'pv_v10'"
        ).fetchone()
        versions = [
            item[0]
            for item in conn.exec_driver_sql(
                "SELECT version FROM schema_migration ORDER BY version"
            )
        ]
    assert "schedule_task" in tables
    assert "schedule_json" in version_columns
    assert {"planned_question_count", "question_ids_json", "question_pack_id"} <= task_columns
    assert "schedule_task_id" in attempt_columns
    assert "uq_attempt_draft_whole" in indexes
    assert "uq_attempt_draft_task" in indexes
    assert "uq_attempt_draft_per_pack" not in indexes
    assert row == ("pv_v10", None)
    assert versions.count("11") == 1

    second = migrate(engine)
    assert second["applied"] is False
    with engine.connect() as conn:
        count = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_migration WHERE version = '11'"
        ).fetchone()[0]
        plan_count = conn.exec_driver_sql("SELECT COUNT(*) FROM learning_plan").fetchone()[0]
    assert count == 1
    assert plan_count == 1
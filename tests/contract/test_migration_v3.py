from __future__ import annotations

from sqlalchemy import create_engine, text

from app.domain.models import Base
from app.infra.migrations import TARGET_SCHEMA_VERSION, migrate, read_schema_state

V2_DDL = (
    """
    CREATE TABLE source_material (
        id VARCHAR PRIMARY KEY,
        display_name VARCHAR NOT NULL,
        original_name VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        ext VARCHAR NOT NULL,
        mime VARCHAR NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        page_count INTEGER,
        status VARCHAR NOT NULL,
        stored_path VARCHAR NOT NULL,
        cleanup_pending INTEGER NOT NULL,
        created_at VARCHAR NOT NULL,
        deleted_at VARCHAR
    )
    """,
    """
    CREATE TABLE learning_plan (
        id VARCHAR PRIMARY KEY,
        title VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        current_version_id VARCHAR,
        created_at VARCHAR NOT NULL,
        updated_at VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE learning_plan_version (
        id VARCHAR PRIMARY KEY,
        plan_id VARCHAR NOT NULL,
        previous_version_id VARCHAR,
        status VARCHAR NOT NULL,
        sources_json TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        items_json TEXT NOT NULL,
        question_count INTEGER,
        handout_advice TEXT,
        created_at VARCHAR NOT NULL,
        confirmed_at VARCHAR
    )
    """,
    """
    CREATE TABLE question (
        id VARCHAR PRIMARY KEY,
        pack_id VARCHAR NOT NULL,
        ordinal INTEGER NOT NULL,
        stem TEXT NOT NULL,
        options_json TEXT NOT NULL,
        answer_index INTEGER NOT NULL,
        explanation TEXT NOT NULL,
        knowledge_point VARCHAR NOT NULL,
        difficulty_tag VARCHAR NOT NULL,
        is_image_based BOOLEAN NOT NULL,
        stem_hash VARCHAR(64) NOT NULL,
        created_at VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE pack_plan_version (
        pack_id VARCHAR PRIMARY KEY,
        plan_version_id VARCHAR NOT NULL,
        adopted_at VARCHAR NOT NULL
    )
    """,
)


def _v2_engine(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'v2.sqlite3').as_posix()}")
    with engine.begin() as conn:
        for ddl in V2_DDL:
            conn.execute(text(ddl))
    legacy_tables = {"source_material", "learning_plan", "learning_plan_version", "question", "pack_plan_version"}
    Base.metadata.create_all(
        engine,
        tables=[table for table in Base.metadata.sorted_tables if table.name not in legacy_tables],
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                "VALUES ('2', '2026-01-01T00:00:00Z', 0, 0, 'legacy')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO source_material(id, display_name, original_name, kind, ext, mime, "
                "size_bytes, sha256, status, stored_path, cleanup_pending, created_at) "
                "VALUES ('src_old', '旧资料', '旧资料.txt', 'txt', 'txt', 'text/plain', 1, :sha, "
                "'active', '/tmp/old', 0, '2026-01-01T00:00:00Z')"
            ),
            {"sha": "a" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO learning_plan(id, title, status, current_version_id, created_at, updated_at) "
                "VALUES ('plan_old', '旧计划', 'confirmed', 'pv_old', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO learning_plan_version(id, plan_id, status, sources_json, scope_json, "
                "items_json, question_count, created_at, confirmed_at) "
                "VALUES ('pv_old', 'plan_old', 'confirmed', :sources, '{}', '[]', 3, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            ),
            {"sources": '[{"source_id": "src_old", "page_start": null, "page_end": null}]'},
        )
    return engine


def test_fresh_schema_has_v3_objects(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'fresh.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    assert TARGET_SCHEMA_VERSION == "11"
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "knowledge_point_mapping",
            "self_assessment",
            "learning_evidence",
            "learner_profile_snapshot",
        } <= tables
        version_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(learning_plan_version)")
        }
        assert {
            "origin",
            "base_version_id",
            "suggestion_key",
            "adjustment_json",
            "assessment_id",
        } <= version_columns
        indexes = {
            row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "uq_plan_version_suggestion" in indexes
        assert "ix_evidence_point_time" in indexes
        assert "ix_profile_source_status" in indexes
    result = migrate(engine)
    assert result["version"] == "11"
    assert result["applied"] is True
    assert read_schema_state(engine)["schema_version"] == "11"


def test_v2_database_upgrades_and_is_idempotent(tmp_path):
    engine = _v2_engine(tmp_path)
    result = migrate(engine)
    assert result["applied"] is True

    with engine.connect() as conn:
        version_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(learning_plan_version)")
        }
        plan_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(learning_plan)")}
        question_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(question)")}
        adoption_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(pack_plan_version)")
        }
        primary = conn.exec_driver_sql(
            "SELECT primary_source_id FROM learning_plan WHERE id = 'plan_old'"
        ).fetchone()[0]
        versions = [row[0] for row in conn.exec_driver_sql("SELECT version FROM schema_migration ORDER BY version")]
        plan_before = conn.exec_driver_sql("SELECT COUNT(*) FROM learning_plan").fetchone()[0]
    assert "origin" in version_columns
    assert "suggestion_key" in version_columns
    assert "assessment_id" in version_columns
    assert "primary_source_id" in plan_columns
    assert "origin_question_id" in question_columns
    assert "delivery_json" in adoption_columns
    assert primary == "src_old"
    assert sorted(versions, key=int) == ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11"]

    second = migrate(engine)
    assert second["applied"] is False
    with engine.connect() as conn:
        plan_after = conn.exec_driver_sql("SELECT COUNT(*) FROM learning_plan").fetchone()[0]
        records = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_migration WHERE version = '3'"
        ).fetchone()[0]
    assert plan_after == plan_before
    assert records == 1


def test_fresh_migrate_creates_two_empty_structural_tables(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    migrate(engine)
    with engine.connect() as conn:
        mappings = conn.exec_driver_sql("SELECT COUNT(*) FROM knowledge_point_mapping").fetchone()[0]
        assessments = conn.exec_driver_sql("SELECT COUNT(*) FROM self_assessment").fetchone()[0]
    assert mappings == 0
    assert assessments == 0
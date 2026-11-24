from __future__ import annotations

from sqlalchemy import create_engine, text

from app.domain.models import Base
from app.infra.migrations import migrate, read_schema_state

LEGACY_REVIEW_FOCUS = """
CREATE TABLE review_focus (
    id VARCHAR PRIMARY KEY,
    source_pack_id VARCHAR NOT NULL,
    source_attempt_id VARCHAR NOT NULL,
    question_id VARCHAR NOT NULL,
    knowledge_point VARCHAR NOT NULL,
    suggestion VARCHAR,
    edited_text VARCHAR,
    ordinal INTEGER NOT NULL,
    is_deleted BOOLEAN NOT NULL,
    used_in_pack_id VARCHAR
)
"""


def _seed(engine, linked: bool) -> None:
    sha = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO source_material(id, display_name, original_name, kind, ext, mime, "
                "size_bytes, sha256, page_count, status, stored_path, cleanup_pending, created_at) "
                "VALUES ('src_old', '旧资料', '旧资料.txt', 'txt', 'txt', 'text/plain', 10, :sha, "
                "NULL, 'active', '/tmp/old', 0, '2026-01-01T00:00:00Z')"
            ),
            {"sha": sha},
        )
        conn.execute(
            text(
                "INSERT INTO learning_pack(id, display_title, source_kind, difficulty, question_count, "
                "job_id, handout_word_count, model_name, prompt_version, created_at) "
                "VALUES ('pack_old', '旧包', 'real', 'consolidate', 1, 'job_old', 0, "
                "'deepseek-flash', 'prompt_v1', '2026-01-01T00:00:00Z')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO pack_source(pack_id, source_id, page_count_used, source_name_snapshot, "
                "source_sha256_snapshot, source_kind_snapshot) "
                "VALUES ('pack_old', 'src_old', NULL, '旧资料', :sha, 'txt')"
            ),
            {"sha": sha},
        )
        conn.execute(
            text(
                "INSERT INTO question(id, pack_id, ordinal, stem, options_json, answer_index, "
                "explanation, knowledge_point, difficulty_tag, is_image_based, stem_hash, created_at) "
                "VALUES ('q_old', 'pack_old', 1, '旧题', '[]', 0, '旧解析', '同网段判断', "
                "'consolidate', 0, :hash, '2026-01-01T00:00:00Z')"
            ),
            {"hash": "b" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO attempt(id, pack_id, status, idempotency_key, correct_count, wrong_count, "
                "unanswered_count, total_count, submitted_at, created_at) "
                "VALUES ('att_old', 'pack_old', 'submitted', 'legacy-key', 1, 0, 0, 1, "
                "NULL, '2026-01-01T00:00:00Z')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO attempt_answer(attempt_id, question_id, selected_index, is_correct, answered_at) "
                "VALUES ('att_old', 'q_old', 0, 1, '2026-01-01T00:00:00Z')"
            )
        )
        if linked:
            conn.execute(
                text(
                    "INSERT INTO knowledge_point(id, source_id, display_name, name_normalized, status, "
                    "created_at, updated_at) VALUES ('kp_old', 'src_old', '同网段判断', '同网段判断', "
                    "'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO question_knowledge_point(question_id, knowledge_point_id, role, "
                    "reliability, created_at) VALUES ('q_old', 'kp_old', 'primary', 'confirmed', "
                    "'2026-01-01T00:00:00Z')"
                )
            )


def _legacy_engine(tmp_path, linked: bool):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.sqlite3').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text(LEGACY_REVIEW_FOCUS))
    Base.metadata.create_all(engine)
    _seed(engine, linked)
    return engine


def test_migration_backfills_pending_and_is_idempotent(tmp_path):
    engine = _legacy_engine(tmp_path, linked=False)
    result = migrate(engine)
    assert result["applied"] is True
    assert (result["linked_count"], result["pending_count"]) == (0, 1)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(review_focus)")}
        evidence = conn.exec_driver_sql(
            "SELECT knowledge_point_id, result, reliability, submitted_at FROM learning_evidence"
        ).fetchall()
    assert "knowledge_point_id" in columns
    assert evidence == [(None, "correct", "pending", None)]

    second = migrate(engine)
    assert second["applied"] is False
    with engine.connect() as conn:
        count = conn.exec_driver_sql("SELECT COUNT(*) FROM learning_evidence").fetchone()[0]
        records = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_migration WHERE version = '2'"
        ).fetchone()[0]
    assert count == 1
    assert records == 1
    state = read_schema_state(engine)
    assert state["schema_version"] == "11"
    assert state["migration_version"] == "11"
    assert state["linked_count"] == 0
    assert state["pending_count"] == 1


def test_migration_links_reliable_point(tmp_path):
    engine = _legacy_engine(tmp_path, linked=True)
    result = migrate(engine)
    assert (result["linked_count"], result["pending_count"]) == (1, 0)
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT knowledge_point_id, reliability FROM learning_evidence"
        ).fetchone()
    assert row == ("kp_old", "confirmed")
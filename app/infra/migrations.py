from __future__ import annotations

import json
import logging

from sqlalchemy import Engine

from app.domain.models import now_iso
from app.infra.ids import new_id

logger = logging.getLogger("xunzhi.migration")

TARGET_SCHEMA_VERSION = "11"

V4_COLUMNS = {
    "self_assessment": {
        "diagnostic_session_id": "VARCHAR",
        "kind": "VARCHAR NOT NULL DEFAULT 'note'",
    },
    "learning_evidence": {
        "diagnostic_answer_id": "VARCHAR",
    },
    "learning_plan_version": {
        "diagnostic_session_id": "VARCHAR",
    },
    "offline_state": {
        "diagnosis_json": "TEXT",
    },
    "generation_job": {
        "launch_state": "VARCHAR",
    },
}

V4_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS diagnostic_session (
        id VARCHAR PRIMARY KEY,
        source_id VARCHAR NOT NULL REFERENCES source_material(id) ON DELETE RESTRICT,
        target_goal TEXT NOT NULL DEFAULT '',
        status VARCHAR NOT NULL DEFAULT 'active',
        current_round INTEGER NOT NULL DEFAULT 0,
        session_key VARCHAR UNIQUE,
        pending_items_json TEXT NOT NULL DEFAULT '[]',
        plan_version_id VARCHAR,
        created_at VARCHAR NOT NULL,
        updated_at VARCHAR NOT NULL,
        confirmed_at VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnostic_round (
        id VARCHAR PRIMARY KEY,
        session_id VARCHAR NOT NULL REFERENCES diagnostic_session(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        intent VARCHAR NOT NULL DEFAULT '',
        status VARCHAR NOT NULL DEFAULT 'active',
        created_at VARCHAR NOT NULL,
        UNIQUE(session_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnostic_question (
        id VARCHAR PRIMARY KEY,
        session_id VARCHAR NOT NULL REFERENCES diagnostic_session(id) ON DELETE CASCADE,
        round_id VARCHAR NOT NULL REFERENCES diagnostic_round(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL DEFAULT 1,
        kind VARCHAR NOT NULL DEFAULT 'scope',
        stem TEXT NOT NULL,
        options_json TEXT NOT NULL DEFAULT '[]',
        answer_index INTEGER,
        explanation TEXT,
        knowledge_point_id VARCHAR REFERENCES knowledge_point(id) ON DELETE SET NULL,
        knowledge_point_name VARCHAR NOT NULL DEFAULT '',
        citation_json TEXT NOT NULL DEFAULT '[]',
        status VARCHAR NOT NULL DEFAULT 'active',
        created_at VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnostic_answer (
        id VARCHAR PRIMARY KEY,
        session_id VARCHAR NOT NULL REFERENCES diagnostic_session(id) ON DELETE CASCADE,
        question_id VARCHAR NOT NULL REFERENCES diagnostic_question(id) ON DELETE CASCADE,
        selected_index INTEGER,
        status VARCHAR NOT NULL DEFAULT 'answered',
        is_correct BOOLEAN,
        answer_key VARCHAR,
        created_at VARCHAR NOT NULL,
        updated_at VARCHAR NOT NULL,
        UNIQUE(session_id, question_id)
    )
    """,
)

V4_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_diagnosis_source_status "
    "ON diagnostic_session(source_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_diagnostic_question_session "
    "ON diagnostic_question(session_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_diagnostic_answer_key "
    "ON diagnostic_answer(answer_key)",
)

V5_COLUMNS = {
    "learning_plan_version": {
        "assessment_id": "VARCHAR",
    },
}

V5_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS learner_profile_snapshot (
        id VARCHAR PRIMARY KEY,
        learner_key VARCHAR NOT NULL DEFAULT 'local',
        source_id VARCHAR NOT NULL REFERENCES source_material(id) ON DELETE RESTRICT,
        page_start INTEGER,
        page_end INTEGER,
        attempt_id VARCHAR REFERENCES attempt(id) ON DELETE SET NULL,
        evidence_ids_json TEXT NOT NULL DEFAULT '[]',
        evidence_fingerprint VARCHAR NOT NULL,
        status VARCHAR NOT NULL DEFAULT 'pending',
        rule_version VARCHAR NOT NULL,
        result_json TEXT,
        failure_code VARCHAR,
        failure_message TEXT,
        retryable BOOLEAN NOT NULL DEFAULT 0,
        created_at VARCHAR NOT NULL,
        completed_at VARCHAR
    )
    """,
)

V5_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_profile_source_status "
    "ON learner_profile_snapshot(source_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_profile_fingerprint "
    "ON learner_profile_snapshot(evidence_fingerprint)",
    "CREATE INDEX IF NOT EXISTS ix_profile_attempt "
    "ON learner_profile_snapshot(attempt_id)",
)

V6_COLUMNS = {
    "learning_plan_version": {
        "modules_json": "TEXT",
    },
    "handout_section": {
        "module_id": "VARCHAR",
    },
    "question": {
        "module_id": "VARCHAR",
    },
}

V6_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_handout_module "
    "ON handout_section(pack_id, module_id)",
    "CREATE INDEX IF NOT EXISTS ix_question_module "
    "ON question(pack_id, module_id)",
)

V7_COLUMNS = {
    "diagnostic_session": {
        "knowledge_base_id": "VARCHAR",
        "source_scope_json": "TEXT",
    },
}

V7_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_base (
        id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at VARCHAR NOT NULL,
        updated_at VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_base_item (
        id VARCHAR PRIMARY KEY,
        knowledge_base_id VARCHAR NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
        source_id VARCHAR REFERENCES source_material(id) ON DELETE CASCADE,
        display_name VARCHAR NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        status VARCHAR NOT NULL DEFAULT 'pending',
        error_code VARCHAR,
        error_message TEXT,
        version_no INTEGER NOT NULL DEFAULT 1,
        position INTEGER NOT NULL DEFAULT 0,
        created_at VARCHAR NOT NULL,
        updated_at VARCHAR NOT NULL
    )
    """,
)

V7_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_kb_item_base "
    "ON knowledge_base_item(knowledge_base_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_kb_item_source "
    "ON knowledge_base_item(source_id)",
)

V8_COLUMNS = {
    "evidence": {
        "body_text": "TEXT",
    },
    "source_material": {
        "text_index_version": "INTEGER NOT NULL DEFAULT 0",
        "text_index_error": "TEXT",
    },
}

V8_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS generation_batch_cache (
        id VARCHAR PRIMARY KEY,
        cache_key VARCHAR NOT NULL UNIQUE,
        plan_version_id VARCHAR,
        job_id VARCHAR NOT NULL,
        batch_index INTEGER NOT NULL,
        fingerprint VARCHAR NOT NULL,
        batch_json TEXT NOT NULL,
        ref_map_json TEXT NOT NULL,
        usage_json TEXT,
        created_at VARCHAR NOT NULL,
        completed_at VARCHAR NOT NULL
    )
    """,
)

V8_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_batch_cache_plan "
    "ON generation_batch_cache(plan_version_id, batch_index)",
    "CREATE INDEX IF NOT EXISTS ix_batch_cache_fingerprint "
    "ON generation_batch_cache(fingerprint)",
)


V9_COLUMNS = {
    "knowledge_point": {
        "page_start": "INTEGER",
        "page_end": "INTEGER",
    },
}

V10_COLUMNS = {
    "learning_plan_version": {
        "schedule_json": "TEXT",
    },
}

V10_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS schedule_task (
        id VARCHAR PRIMARY KEY,
        plan_id VARCHAR NOT NULL REFERENCES learning_plan(id) ON DELETE CASCADE,
        plan_version_id VARCHAR NOT NULL REFERENCES learning_plan_version(id) ON DELETE CASCADE,
        module_id VARCHAR NOT NULL,
        module_title VARCHAR NOT NULL DEFAULT '',
        task_title VARCHAR NOT NULL DEFAULT '',
        task_type VARCHAR NOT NULL DEFAULT 'reading',
        ordinal INTEGER NOT NULL,
        scheduled_date VARCHAR NOT NULL,
        estimated_minutes INTEGER NOT NULL DEFAULT 0,
        knowledge_point_ids_json TEXT NOT NULL DEFAULT '[]',
        status VARCHAR NOT NULL DEFAULT 'pending',
        completed_at VARCHAR,
        completion_source VARCHAR,
        attempt_id VARCHAR REFERENCES attempt(id) ON DELETE SET NULL,
        created_at VARCHAR NOT NULL,
        updated_at VARCHAR NOT NULL,
        UNIQUE(plan_version_id, ordinal)
    )
    """,
)

V10_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_schedule_version "
    "ON schedule_task(plan_version_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS ix_schedule_date ON schedule_task(scheduled_date)",
    "CREATE INDEX IF NOT EXISTS ix_schedule_status ON schedule_task(status)",
)

V11_COLUMNS = {
    "schedule_task": {
        "planned_question_count": "INTEGER NOT NULL DEFAULT 0",
        "question_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "question_pack_id": "VARCHAR",
    },
    "attempt": {
        "schedule_task_id": "VARCHAR",
    },
}

V11_INDEXES = (
    "DROP INDEX IF EXISTS uq_attempt_draft_per_pack",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_draft_whole "
    "ON attempt(pack_id) WHERE status = 'draft' AND schedule_task_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_draft_task "
    "ON attempt(pack_id, schedule_task_id) WHERE status = 'draft' AND schedule_task_id IS NOT NULL",
)


V3_COLUMNS = {
    "question": {
        "origin_question_id": "VARCHAR",
        "variant_kind": "VARCHAR",
    },
    "learning_plan": {
        "primary_source_id": "VARCHAR",
    },
    "learning_plan_version": {
        "origin": "VARCHAR NOT NULL DEFAULT 'manual'",
        "base_version_id": "VARCHAR",
        "suggestion_key": "VARCHAR",
        "evidence_fingerprint": "VARCHAR",
        "input_hash": "VARCHAR",
        "adjustment_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "pack_plan_version": {
        "delivery_json": "TEXT",
    },
}

V3_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_point_mapping (
        id VARCHAR PRIMARY KEY,
        from_point_id VARCHAR NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
        to_point_id VARCHAR NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
        basis TEXT,
        status VARCHAR NOT NULL DEFAULT 'active',
        created_at VARCHAR NOT NULL,
        revoked_at VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS self_assessment (
        id VARCHAR PRIMARY KEY,
        knowledge_point_id VARCHAR REFERENCES knowledge_point(id) ON DELETE SET NULL,
        text TEXT NOT NULL DEFAULT '',
        source VARCHAR NOT NULL DEFAULT 'manual',
        created_at VARCHAR NOT NULL
    )
    """,
)

V3_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_evidence_point_time "
    "ON learning_evidence(knowledge_point_id, submitted_at)",
    "CREATE INDEX IF NOT EXISTS ix_evidence_source_time "
    "ON learning_evidence(source_id, submitted_at)",
    "CREATE INDEX IF NOT EXISTS ix_question_origin ON question(origin_question_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_version_suggestion "
    "ON learning_plan_version(suggestion_key)",
    "CREATE INDEX IF NOT EXISTS ix_plan_source_status "
    "ON learning_plan(primary_source_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_plan_version_base "
    "ON learning_plan_version(base_version_id)",
    "CREATE INDEX IF NOT EXISTS ix_kp_mapping_from "
    "ON knowledge_point_mapping(from_point_id)",
)


class MigrationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _ensure_review_focus_column(conn) -> None:
    if "review_focus" not in _tables(conn):
        return
    if "knowledge_point_id" not in _columns(conn, "review_focus"):
        conn.exec_driver_sql("ALTER TABLE review_focus ADD COLUMN knowledge_point_id VARCHAR")


def _question_point(conn, question_id: str):
    if "question_knowledge_point" not in _tables(conn):
        return None
    return conn.exec_driver_sql(
        "SELECT knowledge_point_id FROM question_knowledge_point "
        "WHERE question_id = ? AND role = 'primary' LIMIT 1",
        (question_id,),
    ).first()


def _question_citation(conn, question_id: str):
    return conn.exec_driver_sql(
        "SELECT id, source_id FROM citation WHERE owner_type = 'question' AND owner_id = ? "
        "ORDER BY id LIMIT 1",
        (question_id,),
    ).first()


def _fallback_source(conn, pack_id: str):
    return conn.exec_driver_sql(
        "SELECT source_id FROM pack_source WHERE pack_id = ? ORDER BY source_id LIMIT 1",
        (pack_id,),
    ).first()


def _backfill_attempt_answers(conn) -> tuple[int, int]:
    rows = conn.exec_driver_sql(
        """
        SELECT aa.attempt_id, a.pack_id, aa.question_id, aa.selected_index, aa.is_correct,
               a.submitted_at, lp.parent_pack_id
        FROM attempt_answer aa
        JOIN attempt a ON a.id = aa.attempt_id
        JOIN learning_pack lp ON lp.id = a.pack_id
        LEFT JOIN learning_evidence le
          ON le.attempt_id = aa.attempt_id AND le.question_id = aa.question_id
        WHERE a.status = 'submitted' AND le.id IS NULL
        ORDER BY a.submitted_at, aa.attempt_id, aa.question_id
        """
    ).fetchall()
    linked = 0
    pending = 0
    for row in rows:
        attempt_id, pack_id, question_id, selected_index, is_correct, submitted_at, parent_pack_id = row
        if selected_index is None:
            result = "unanswered"
        elif is_correct:
            result = "correct"
        else:
            result = "wrong"
        point = _question_point(conn, question_id)
        citation = _question_citation(conn, question_id)
        source_id = citation[1] if citation is not None else None
        if source_id is None:
            fallback = _fallback_source(conn, pack_id)
            source_id = fallback[0] if fallback is not None else None
        point_id = point[0] if point is not None else None
        reliability = "confirmed" if point_id else "pending"
        if point_id:
            linked += 1
        else:
            pending += 1
        conn.exec_driver_sql(
            "INSERT INTO learning_evidence("
            "id, pack_id, attempt_id, question_id, knowledge_point_id, source_id, citation_id, "
            "result, source_type, reliability, submitted_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("learning_evidence"),
                pack_id,
                attempt_id,
                question_id,
                point_id,
                source_id,
                citation[0] if citation is not None else None,
                result,
                "retry" if parent_pack_id else "practice",
                reliability,
                submitted_at,
                now_iso(),
            ),
        )
    return linked, pending


def _ensure_v3_columns(conn) -> None:
    for table, columns in V3_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v3_tables(conn) -> None:
    if "knowledge_point" not in _tables(conn):
        return
    for statement in V3_TABLES:
        conn.exec_driver_sql(statement)


def _ensure_v3_indexes(conn) -> None:
    for statement in V3_INDEXES:
        conn.exec_driver_sql(statement)


def _backfill_primary_source(conn) -> int:
    rows = conn.exec_driver_sql(
        "SELECT id, current_version_id FROM learning_plan WHERE primary_source_id IS NULL"
    ).fetchall()
    updated = 0
    for plan_id, version_id in rows:
        if not version_id:
            continue
        version = conn.exec_driver_sql(
            "SELECT sources_json FROM learning_plan_version WHERE id = ?", (version_id,)
        ).first()
        if version is None:
            continue
        try:
            sources = json.loads(version[0] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(sources, list) or not sources:
            continue
        source_id = sources[0].get("source_id") if isinstance(sources[0], dict) else None
        if not source_id:
            continue
        if conn.exec_driver_sql(
            "SELECT 1 FROM source_material WHERE id = ?", (source_id,)
        ).first() is None:
            continue
        conn.exec_driver_sql(
            "UPDATE learning_plan SET primary_source_id = ? WHERE id = ?", (source_id, plan_id)
        )
        updated += 1
    return updated


def _ensure_v4_columns(conn) -> None:
    for table, columns in V4_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                try:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                except Exception as exc:
                    if table == "generation_job" and name == "launch_state":
                        logger.error(
                            "migration failed code=%s table=%s column=%s stage=migrate",
                            "launch_state_migration_failed",
                            table,
                            name,
                        )
                        raise MigrationError(
                            "launch_state_migration_failed",
                            f"补充 generation_job.launch_state 失败：{exc}",
                        ) from exc
                    raise


def _assert_generation_launch_state(conn) -> None:
    if "generation_job" not in _tables(conn):
        return
    if "launch_state" not in _columns(conn, "generation_job"):
        logger.error(
            "migration assert failed code=%s table=%s column=%s stage=assert",
            "launch_state_column_missing",
            "generation_job",
            "launch_state",
        )
        raise MigrationError(
            "launch_state_column_missing",
            "数据库缺少 generation_job.launch_state，已停止启动",
        )


def _ensure_v4_tables(conn) -> None:
    if "source_material" not in _tables(conn):
        return
    for statement in V4_TABLES:
        conn.exec_driver_sql(statement)


def _ensure_v4_indexes(conn) -> None:
    for statement in V4_INDEXES:
        conn.exec_driver_sql(statement)


def _ensure_v5_columns(conn) -> None:
    for table, columns in V5_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v5_tables(conn) -> None:
    if "source_material" not in _tables(conn):
        return
    for statement in V5_TABLES:
        conn.exec_driver_sql(statement)


def _ensure_v5_indexes(conn) -> None:
    for statement in V5_INDEXES:
        conn.exec_driver_sql(statement)


def _ensure_v6_columns(conn) -> None:
    for table, columns in V6_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v6_indexes(conn) -> None:
    for statement in V6_INDEXES:
        conn.exec_driver_sql(statement)


def _ensure_v9_columns(conn) -> None:
    for table, columns in V9_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v8_columns(conn) -> None:
    for table, columns in V8_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v8_tables(conn) -> None:
    if "source_material" not in _tables(conn):
        return
    for statement in V8_TABLES:
        conn.exec_driver_sql(statement)


def _ensure_v8_indexes(conn) -> None:
    for statement in V8_INDEXES:
        conn.exec_driver_sql(statement)


def _ensure_v10_columns(conn) -> None:
    for table, columns in V10_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v10_tables(conn) -> None:
    if "learning_plan_version" not in _tables(conn):
        return
    for statement in V10_TABLES:
        conn.exec_driver_sql(statement)


def _ensure_v10_indexes(conn) -> None:
    for statement in V10_INDEXES:
        conn.exec_driver_sql(statement)


def _ensure_v11_columns(conn) -> None:
    for table, columns in V11_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v11_indexes(conn) -> None:
    for statement in V11_INDEXES:
        conn.exec_driver_sql(statement)


def _ensure_v7_columns(conn) -> None:
    for table, columns in V7_COLUMNS.items():
        if table not in _tables(conn):
            continue
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_v7_tables(conn) -> None:
    if "source_material" not in _tables(conn):
        return
    for statement in V7_TABLES:
        conn.exec_driver_sql(statement)


def _ensure_v7_indexes(conn) -> None:
    for statement in V7_INDEXES:
        conn.exec_driver_sql(statement)


def migrate(engine: Engine) -> dict:
    applied = False
    linked = 0
    pending = 0
    try:
        with engine.begin() as conn:
            _ensure_review_focus_column(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '2'"
            ).first() is None:
                linked, pending = _backfill_attempt_answers(conn)
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('2', ?, ?, ?, ?)",
                    (
                        now_iso(),
                        linked,
                        pending,
                        f"v1->v2: backfilled {linked + pending} attempt_answer rows; "
                        f"linked={linked} pending={pending}",
                    ),
                )
            _ensure_v3_columns(conn)
            _ensure_v3_tables(conn)
            _ensure_v3_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '3'"
            ).first() is None:
                backfilled = _backfill_primary_source(conn)
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('3', ?, 0, 0, ?)",
                    (now_iso(), f"v2->v3: columns/tables/indexes; primary_source_backfilled={backfilled}"),
                )
                applied = True
            try:
                _ensure_v4_columns(conn)
            except MigrationError:
                raise
            except Exception as exc:
                logger.error(
                    "migration failed code=%s table=%s column=%s stage=migrate",
                    "launch_state_migration_failed",
                    "generation_job",
                    "launch_state",
                )
                raise MigrationError(
                    "launch_state_migration_failed",
                    f"补充 generation_job.launch_state 失败：{exc}",
                ) from exc
            _assert_generation_launch_state(conn)
            _ensure_v4_tables(conn)
            _ensure_v4_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '4'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('4', ?, 0, 0, ?)",
                    (now_iso(), "v3->v4: diagnostic tables/columns/indexes; legacy rows unchanged"),
                )
                applied = True
            _ensure_v5_columns(conn)
            _ensure_v5_tables(conn)
            _ensure_v5_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '5'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('5', ?, 0, 0, ?)",
                    (
                        now_iso(),
                        "v4->v5: learner profile snapshots and plan assessment_id; legacy rows unchanged",
                    ),
                )
                applied = True
            _ensure_v6_columns(conn)
            _ensure_v6_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '6'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('6', ?, 0, 0, ?)",
                    (
                        now_iso(),
                        "v5->v6: plan module blueprint and module ids on handout/question; legacy rows unchanged",
                    ),
                )
                applied = True
            _ensure_v7_columns(conn)
            _ensure_v7_tables(conn)
            _ensure_v7_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '7'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('7', ?, 0, 0, ?)",
                    (
                        now_iso(),
                        "v6->v7: knowledge bases/items and diagnostic multi-source scope; legacy rows unchanged",
                    ),
                )
                applied = True
            _ensure_v8_columns(conn)
            _ensure_v8_tables(conn)
            _ensure_v8_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '8'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('8', ?, 0, 0, ?)",
                    (
                        now_iso(),
                        "v7->v8: evidence body text/index state and generation batch cache; legacy rows unchanged",
                    ),
                )
                applied = True
            _ensure_v9_columns(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '9'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('9', ?, 0, 0, ?)",
                    (
                        now_iso(),
                        "v8->v9: knowledge point page ranges for chapter-grounded plans; legacy rows unchanged",
                    ),
                )
                applied = True
            _ensure_v10_columns(conn)
            _ensure_v10_tables(conn)
            _ensure_v10_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '10'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('10', ?, 0, 0, ?)",
                    (
                        now_iso(),
                        "v9->v10: plan schedule config and schedule tasks; legacy rows unchanged",
                    ),
                )
                applied = True
            _ensure_v11_columns(conn)
            _ensure_v11_indexes(conn)
            if conn.exec_driver_sql(
                "SELECT version FROM schema_migration WHERE version = '11'"
            ).first() is None:
                conn.exec_driver_sql(
                    "INSERT INTO schema_migration(version, applied_at, linked_count, pending_count, detail) "
                    "VALUES ('11', ?, 0, 0, ?)",
                    (
                        now_iso(),
                        "v10->v11: task question bindings and scoped practice attempts; legacy attempts stay whole-pack",
                    ),
                )
                applied = True
            conn.exec_driver_sql(
                "INSERT INTO app_setting(key, value, updated_at) VALUES ('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (TARGET_SCHEMA_VERSION, now_iso()),
            )
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError("migration_failed", f"数据库迁移失败：{exc}") from exc
    return {"version": TARGET_SCHEMA_VERSION, "applied": applied, "linked_count": linked, "pending_count": pending}


def read_schema_state(engine: Engine) -> dict:
    try:
        with engine.connect() as conn:
            version_row = conn.exec_driver_sql(
                "SELECT value FROM app_setting WHERE key = 'schema_version'"
            ).first()
            migration_row = conn.exec_driver_sql(
                "SELECT version, applied_at, linked_count, pending_count, detail "
                "FROM schema_migration ORDER BY CAST(version AS INTEGER) DESC LIMIT 1"
            ).first()
            backfill_row = conn.exec_driver_sql(
                "SELECT linked_count, pending_count FROM schema_migration WHERE version = '2'"
            ).first()
        return {
            "schema_version": version_row[0] if version_row is not None else "unknown",
            "migration_version": migration_row[0] if migration_row is not None else None,
            "migration_applied_at": migration_row[1] if migration_row is not None else None,
            "linked_count": backfill_row[0] if backfill_row is not None else None,
            "pending_count": backfill_row[1] if backfill_row is not None else None,
            "migration_detail": migration_row[4] if migration_row is not None else None,
        }
    except Exception:
        return {"schema_version": "unknown", "migration_version": None}
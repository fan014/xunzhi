from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.domain.models import Base, now_iso
from app.infra.migrations import TARGET_SCHEMA_VERSION, migrate

SCHEMA_VERSION = TARGET_SCHEMA_VERSION

engine = create_engine(
    f"sqlite:///{settings.db_path.as_posix()}",
    future=True,
    connect_args={"check_same_thread": False, "timeout": 5},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS trg_progress_no_update
                BEFORE UPDATE ON progress_snapshot
                BEGIN
                    SELECT RAISE(ABORT, 'progress snapshot is immutable');
                END
                """
            )
        )
    migrate(engine)


def recover_interrupted_jobs() -> None:
    with engine.begin() as conn:
        # 诊断任务：已落库但未派发（NULL/pending_dispatch）保留 queued，重启后重调度原 job；
        # 已取权但执行器未接手（dispatching）释放回 pending_dispatch；dispatched 正常不可达，防御性释放。
        conn.execute(
            text(
                "UPDATE generation_job SET launch_state='pending_dispatch', updated_at=:now "
                "WHERE status='queued' AND idempotency_key LIKE 'diagnostic:%' "
                "AND launch_state IN ('dispatching','dispatched')"
            ),
            {"now": now_iso()},
        )
        # 已进入 running 的诊断任务无法原地恢复，按既有语义标记中断，下一次确认可推进代次。
        conn.execute(
            text(
                "UPDATE generation_job SET status='failed', error_code='interrupted', "
                "error_detail='应用重启时任务仍在运行，已标记为中断', updated_at=:now "
                "WHERE status='running' AND idempotency_key LIKE 'diagnostic:%'"
            ),
            {"now": now_iso()},
        )
        # 非诊断任务保持既有行为：queued/running 一律标记中断。
        conn.execute(
            text(
                "UPDATE generation_job SET status='failed', error_code='interrupted', "
                "error_detail='应用重启时任务仍在运行，已标记为中断', updated_at=:now "
                "WHERE status IN ('queued','running') AND idempotency_key NOT LIKE 'diagnostic:%'"
            ),
            {"now": now_iso()},
        )
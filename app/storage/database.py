import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

Base = declarative_base()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB = str(_PROJECT_ROOT / "data" / "atlas.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DEFAULT_DB}")

_engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    # NullPool plus a busy timeout lets concurrent writers wait instead of
    # failing immediately with "database is locked".
    _engine_kwargs["poolclass"] = NullPool
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _configure_sqlite(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


if DATABASE_URL.startswith("sqlite"):
    event.listen(engine, "connect", _configure_sqlite)


def _migrate() -> None:
    """Add columns and indexes that create_all will not add to an existing file."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "cases" in tables:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(cases)"))}
            if "input_hash" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN input_hash VARCHAR(64)"))
            if "replay_of_case_id" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN replay_of_case_id INTEGER"))
            if "error" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN error TEXT"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_cases_idempotency_key ON cases (idempotency_key)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_cases_replay_of ON cases (replay_of_case_id)"
            ))
        if "case_notes" in tables:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(case_notes)"))}
            if "dedupe_key" not in cols:
                conn.execute(text("ALTER TABLE case_notes ADD COLUMN dedupe_key VARCHAR(255)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_case_notes_dedupe_key ON case_notes (dedupe_key)"
            ))
        if "steps" in tables:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_steps_case_id ON steps (case_id)"))


def init_db() -> None:
    from app.storage import models  # noqa: F401

    Path(_DEFAULT_DB).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _migrate()


def mark_interrupted_cases_failed() -> int:
    """Single-worker recovery. A case left `running` across a restart will never finish.

    Do not use this as-is with more than one API process: a second process
    starting up would fail a case the first process is still executing.
    """
    from app.storage.models import Case
    import json

    db = SessionLocal()
    try:
        rows = db.query(Case).filter_by(status="running").all()
        for case in rows:
            case.status = "failed"
            case.error = json.dumps({"step": None, "error_type": "Interrupted"})
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

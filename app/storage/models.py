from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.storage.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_cases_idempotency_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=utcnow)
    status = Column(String(50), default="pending")
    # NULL keys are not idempotent. SQLite treats NULLs as distinct under this
    # unique constraint, so omitted keys still create separate cases.
    idempotency_key = Column(String(255), nullable=True)
    input_hash = Column(String(64), nullable=True)
    replay_of_case_id = Column(Integer, nullable=True, index=True)
    inputs_json = Column(Text)
    artifacts_json = Column(Text)
    outputs_json = Column(Text)
    error = Column(Text, nullable=True)

    steps = relationship("Step", back_populates="case")
    case_notes = relationship("CaseNote", back_populates="case")


class Step(Base):
    __tablename__ = "steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("cases.id"), index=True)
    name = Column(String(255))
    status = Column(String(50), default="pending")
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    attempts = Column(Integer, default=0)
    output_json = Column(Text)
    error = Column(Text)

    case = relationship("Case", back_populates="steps")


class CaseNote(Base):
    __tablename__ = "case_notes"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_case_notes_dedupe_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("cases.id"), index=True)
    employee_id = Column(String(255))
    note = Column(Text)
    # One draft note per case. Retries reuse the row instead of appending another.
    dedupe_key = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    case = relationship("Case", back_populates="case_notes")


class EmployeeRecord(Base):
    __tablename__ = "employee_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String(255), unique=True)
    metadata_json = Column(Text)
    last_activities_json = Column(Text)

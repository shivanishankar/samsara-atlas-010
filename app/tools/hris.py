import json

from sqlalchemy.exc import IntegrityError

from app.privacy import audit_employee_view
from app.storage.database import SessionLocal
from app.storage.models import CaseNote, EmployeeRecord


def retrieve_employee_context(employee_id: str) -> dict:
    """Return an allowlisted HRIS snapshot. Unknown fields never enter the workflow."""
    db = SessionLocal()
    try:
        emp = db.query(EmployeeRecord).filter_by(employee_id=employee_id).first()
        if not emp:
            return audit_employee_view(employee_id, {}, [], found=False)
        metadata = json.loads(emp.metadata_json or "{}")
        activities = json.loads(emp.last_activities_json or "[]")
        return audit_employee_view(employee_id, metadata, activities, found=True)
    finally:
        db.close()


def post_case_note_to_hris(
    case_id: int,
    employee_id: str,
    note: str,
    dedupe_key: str | None = None,
) -> dict:
    """Write one draft note per dedupe key. A retry returns the existing row."""
    db = SessionLocal()
    try:
        if dedupe_key:
            existing = db.query(CaseNote).filter_by(dedupe_key=dedupe_key).first()
            if existing:
                return _note_result(existing, existed=True)
        case_note = CaseNote(
            case_id=case_id,
            employee_id=employee_id,
            note=note,
            dedupe_key=dedupe_key,
        )
        db.add(case_note)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if not dedupe_key:
                raise
            existing = db.query(CaseNote).filter_by(dedupe_key=dedupe_key).one()
            return _note_result(existing, existed=True)
        db.refresh(case_note)
        return _note_result(case_note, existed=False)
    finally:
        db.close()


def _note_result(case_note: CaseNote, existed: bool) -> dict:
    return {
        "id": case_note.id,
        "employee_id": case_note.employee_id,
        "note": (case_note.note or "")[:100],
        "existed": existed,
    }

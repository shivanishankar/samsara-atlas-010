import json
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.engine.errors import IdempotencyConflict, InvalidWorkflowInput, WorkflowFailed, WorkflowNotFinished
from app.engine.runner import run_workflow
from app.providers.base import ProviderConfigError
from app.schemas import CaseInputs
from app.storage.database import SessionLocal
from app.storage.models import Case, CaseNote, EmployeeRecord, Step

logger = logging.getLogger(__name__)

router = APIRouter()


def _load_json(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _execute_workflow(inputs: dict, idempotency_key: str | None = None, replay_of_case_id: int | None = None):
    try:
        return run_workflow(
            inputs=inputs,
            idempotency_key=idempotency_key,
            replay_of_case_id=replay_of_case_id,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was already used with different inputs",
        ) from None
    except InvalidWorkflowInput:
        raise HTTPException(status_code=400, detail="Invalid case inputs") from None
    except WorkflowNotFinished as exc:
        raise HTTPException(
            status_code=503,
            detail={"message": "Case is still running", "case_id": exc.case_id},
            headers={"Retry-After": "2"},
        ) from None
    except WorkflowFailed as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Workflow failed",
                "case_id": exc.case_id,
                "step": exc.step,
                "error_type": exc.error_type,
            },
        ) from None
    except ProviderConfigError:
        raise HTTPException(status_code=500, detail="Model provider is misconfigured") from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("unhandled_case_error error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Internal error") from None


@router.get("/employees")
def list_employees():
    db = SessionLocal()
    try:
        employees = db.query(EmployeeRecord).order_by(EmployeeRecord.employee_id.asc()).all()
        return {"employees": [employee.employee_id for employee in employees]}
    finally:
        db.close()


@router.get("/info")
def get_info():
    """Returns current provider type - use to verify Ollama is active."""
    import os

    return {"provider": os.getenv("PROVIDER_TYPE", "stub")}


@router.get("/cases")
def list_cases(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    db = SessionLocal()
    try:
        total = db.query(Case).count()
        cases = db.query(Case).order_by(Case.id.desc()).offset(offset).limit(limit).all()
        return {
            "case_ids": [case.id for case in cases],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        db.close()


class CreateCaseRequest(BaseModel):
    inputs: CaseInputs
    idempotency_key: str | None = Field(default=None, max_length=255)


@router.post("/cases")
def create_case(req: CreateCaseRequest):
    return _execute_workflow(req.inputs.model_dump(), idempotency_key=req.idempotency_key)


@router.get("/cases/{case_id}")
def get_case(case_id: int):
    db = SessionLocal()
    try:
        case = db.query(Case).filter_by(id=case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")
        steps = db.query(Step).filter_by(case_id=case_id).order_by(Step.id.asc()).all()
        case_notes = db.query(CaseNote).filter_by(case_id=case_id).order_by(CaseNote.id.asc()).all()
        replays = db.query(Case.id).filter(Case.replay_of_case_id == case_id).order_by(Case.id.asc()).all()
        return {
            "case": {
                "id": case.id,
                "status": case.status,
                "inputs": _load_json(case.inputs_json, {}),
                "artifacts": _load_json(case.artifacts_json, {}),
                "outputs": _load_json(case.outputs_json, {}),
                "error": _load_json(case.error, None),
                "replay_of_case_id": case.replay_of_case_id,
                "review_state": "draft" if case.status == "completed" else None,
            },
            "steps": [
                {
                    "id": step.id,
                    "name": step.name,
                    "status": step.status,
                    "started_at": step.started_at.isoformat() if step.started_at else None,
                    "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                    "attempts": step.attempts,
                    "output": _load_json(step.output_json, None),
                    "error": step.error,
                }
                for step in steps
            ],
            "case_notes": [
                {
                    "id": note.id,
                    "employee_id": note.employee_id,
                    "note": note.note,
                    "created_at": note.created_at.isoformat() if note.created_at else None,
                }
                for note in case_notes
            ],
            "replay_case_ids": [row[0] for row in replays],
        }
    finally:
        db.close()


@router.post("/cases/{case_id}/replay")
def replay_case(case_id: int):
    db = SessionLocal()
    try:
        case = db.query(Case).filter_by(id=case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")
        try:
            inputs = json.loads(case.inputs_json or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="Case inputs could not be read") from None
        parent_id = case.id
    finally:
        db.close()
    # A replay is a new execution linked to the case it was copied from.
    # It does not reuse the original idempotency key.
    return _execute_workflow(inputs, idempotency_key=None, replay_of_case_id=parent_id)

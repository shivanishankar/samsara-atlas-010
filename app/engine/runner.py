import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.engine.context import ExecutionContext
from app.engine.errors import (
    IdempotencyConflict,
    InvalidWorkflowInput,
    WorkflowFailed,
    WorkflowNotFinished,
)
from app.engine.steps import STEPS, prompt_fingerprints
from app.privacy import collect_secrets, redact
from app.providers.base import ProviderConfigError
from app.providers.factory import get_provider
from app.schemas import CaseInputs
from app.storage.database import SessionLocal
from app.storage.models import Case, Step

logger = logging.getLogger(__name__)

WAIT_SECONDS = float(os.getenv("WORKFLOW_WAIT_SECONDS", "90"))
_TERMINAL = ("completed", "failed")


def run_workflow(
    inputs: dict,
    idempotency_key: str | None = None,
    replay_of_case_id: int | None = None,
) -> dict:
    validated = _validate_inputs(inputs)
    key = _normalize_key(idempotency_key)
    # Fail before inserting a case when the process is pointed at a provider
    # that does not exist. A silent fallback would draft replies with the stub
    # and record them as if a real model had written them.
    get_provider()
    case_id, action = _claim_case(
        key=key,
        inputs=validated,
        input_hash=_hash_inputs(validated),
        replay_of_case_id=replay_of_case_id,
    )
    if action == "wait":
        logger.info("idempotency_wait case_id=%s", case_id)
        _wait_for_case(case_id)
        return _completed_result(case_id)
    if action == "done":
        logger.info("idempotency_hit case_id=%s", case_id)
        return _completed_result(case_id)
    return _execute(case_id, validated)


def mark_interrupted_cases_failed() -> int:
    from app.storage.database import mark_interrupted_cases_failed as _mark

    return _mark()


def _validate_inputs(inputs: dict) -> dict:
    try:
        return CaseInputs(**inputs).model_dump()
    except ValidationError:
        raise InvalidWorkflowInput() from None


def _normalize_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    stripped = idempotency_key.strip()
    return stripped or None


def _hash_inputs(inputs: dict) -> str:
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _initial_artifacts(replay_of_case_id: int | None) -> dict:
    provider = get_provider()
    return {
        "prompt_version": "v1",
        "prompt_hashes": prompt_fingerprints(),
        "provider": os.getenv("PROVIDER_TYPE", "stub").strip().lower() or "stub",
        "model_name": getattr(provider, "model_name", type(provider).__name__),
        "replay_of_case_id": replay_of_case_id,
        "trace": [],
    }


def _claim_case(key: str | None, inputs: dict, input_hash: str, replay_of_case_id: int | None) -> tuple[int, str]:
    """Insert the case or attach to the one that already owns this key.

    Returns (case_id, action) where action is "run", "wait", or "done".
    A missing key is not a key: it must not match other rows whose key is NULL.
    """
    last_lock_error: Exception | None = None
    for _ in range(5):
        db = SessionLocal()
        try:
            if key is not None:
                existing = db.query(Case).filter(Case.idempotency_key == key).first()
                if existing is not None:
                    return _decide_existing(db, existing, input_hash)
            case = Case(
                status="running",
                idempotency_key=key,
                input_hash=input_hash,
                replay_of_case_id=replay_of_case_id,
                inputs_json=json.dumps(inputs, sort_keys=True),
                artifacts_json=json.dumps(_initial_artifacts(replay_of_case_id)),
            )
            db.add(case)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if key is None:
                    raise
                existing = db.query(Case).filter(Case.idempotency_key == key).first()
                if existing is None:
                    raise
                return _decide_existing(db, existing, input_hash)
            db.refresh(case)
            return case.id, "run"
        except OperationalError as exc:
            db.rollback()
            last_lock_error = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(0.05)
        finally:
            db.close()
    raise last_lock_error or OperationalError("database is locked", None, None)


def _decide_existing(db, existing: Case, input_hash: str) -> tuple[int, str]:
    if existing.input_hash and existing.input_hash != input_hash:
        raise IdempotencyConflict()
    if not existing.input_hash:
        existing.input_hash = input_hash
        db.commit()
    if existing.status == "completed":
        return existing.id, "done"
    if existing.status == "failed":
        claimed = (
            db.query(Case)
            .filter(Case.id == existing.id, Case.status == "failed")
            .update({Case.status: "running"}, synchronize_session=False)
        )
        db.commit()
        if claimed == 1:
            return existing.id, "run"
        return existing.id, "wait"
    return existing.id, "wait"


def _wait_for_case(case_id: int) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            status = db.query(Case.status).filter(Case.id == case_id).scalar()
        finally:
            db.close()
        if status in _TERMINAL:
            return
        time.sleep(0.05)
    raise WorkflowNotFinished(case_id)


def _completed_result(case_id: int) -> dict:
    db = SessionLocal()
    try:
        case = db.query(Case).filter(Case.id == case_id).one()
        status = case.status
        if status == "failed":
            err = _load_json(case.error, {})
            raise WorkflowFailed(
                case.id,
                err.get("step") or "unknown",
                err.get("error_type") or "Error",
            )
        if status != "completed":
            raise WorkflowNotFinished(case.id)
        return {
            "case_id": case.id,
            "status": status,
            "outputs": _load_json(case.outputs_json, {}),
            "replay_of_case_id": case.replay_of_case_id,
            "review_state": "draft",
        }
    finally:
        db.close()


def _execute(case_id: int, inputs: dict) -> dict:
    db = SessionLocal()
    case = None
    ctx = None
    try:
        case = db.query(Case).filter(Case.id == case_id).one()
        artifacts = _load_json(case.artifacts_json, {})
        artifacts.setdefault("trace", [])
        ctx = ExecutionContext(
            case_id=case.id,
            inputs=inputs,
            artifacts=artifacts,
        )
        ctx.trace = artifacts["trace"]
        prior = _latest_steps(db, case.id)

        for step_name, step_fn in STEPS:
            existing = prior.get(step_name)
            if existing is not None and existing.status == "completed" and existing.output_json:
                try:
                    ctx.step_outputs[step_name] = _decode_output(existing.output_json)
                    continue
                except json.JSONDecodeError:
                    pass

            step_record = existing if existing is not None else Step(case_id=case.id, name=step_name)
            if existing is None:
                db.add(step_record)
            prior_attempts = step_record.attempts or 0
            step_record.status = "running"
            step_record.started_at = datetime.now(timezone.utc)
            step_record.error = None
            db.commit()

            started = time.perf_counter()
            try:
                output = step_fn(ctx)
            except Exception as exc:
                _fail_step(db, case, ctx, step_record, step_name, prior_attempts, started, exc)
                raise WorkflowFailed(case.id, step_name, type(exc).__name__) from None

            ctx.step_outputs[step_name] = output
            elapsed_ms = (time.perf_counter() - started) * 1000
            attempts = prior_attempts + ctx.step_attempts.get(step_name, 1)
            step_record.status = "completed"
            step_record.output_json = _encode_output(output)
            step_record.finished_at = datetime.now(timezone.utc)
            step_record.attempts = attempts
            step_record.error = None
            ctx.trace.append({
                "step": step_name,
                "status": "completed",
                "duration_ms": round(elapsed_ms, 1),
                "attempts": attempts,
            })
            case.artifacts_json = json.dumps(ctx.artifacts)
            db.commit()
            logger.info(
                "step_completed case_id=%s step=%s duration_ms=%.0f attempts=%s",
                case.id,
                step_name,
                elapsed_ms,
                attempts,
            )

        final_output = ctx.step_outputs.get("generate_structured_response", {})
        case.status = "completed"
        case.outputs_json = json.dumps(final_output)
        case.error = None
        case.artifacts_json = json.dumps(ctx.artifacts)
        db.commit()
        return {
            "case_id": case.id,
            "status": "completed",
            "outputs": final_output,
            "replay_of_case_id": case.replay_of_case_id,
            "review_state": "draft",
        }
    finally:
        db.close()


def _fail_step(db, case, ctx, step_record, step_name: str, prior_attempts: int, started: float, exc: Exception) -> None:
    elapsed_ms = (time.perf_counter() - started) * 1000
    attempts = prior_attempts + ctx.step_attempts.get(step_name, 1)
    secrets = collect_secrets(ctx.inputs, ctx.step_outputs.get("retrieve_employee_context"))
    safe_message = redact(f"{type(exc).__name__}: {exc}", secrets)
    step_record.status = "failed"
    step_record.error = safe_message
    step_record.finished_at = datetime.now(timezone.utc)
    step_record.attempts = attempts
    ctx.trace.append({
        "step": step_name,
        "status": "failed",
        "duration_ms": round(elapsed_ms, 1),
        "attempts": attempts,
        "error_type": type(exc).__name__,
    })
    case.status = "failed"
    case.error = json.dumps({"step": step_name, "error_type": type(exc).__name__})
    case.artifacts_json = json.dumps(ctx.artifacts)
    db.commit()
    logger.error(
        "step_failed case_id=%s step=%s error_type=%s duration_ms=%.0f attempts=%s",
        case.id,
        step_name,
        type(exc).__name__,
        elapsed_ms,
        attempts,
    )


def _latest_steps(db, case_id: int) -> dict[str, Step]:
    rows = db.query(Step).filter(Step.case_id == case_id).order_by(Step.id.asc()).all()
    latest: dict[str, Step] = {}
    for row in rows:
        latest[row.name] = row
    return latest


def _encode_output(output) -> str:
    if isinstance(output, (str, int, float, bool)) or output is None:
        return json.dumps({"value": output})
    return json.dumps(output)


def _decode_output(raw: str):
    data = json.loads(raw)
    if isinstance(data, dict) and list(data.keys()) == ["value"]:
        return data["value"]
    return data


def _load_json(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


# Re-exported so a misconfigured provider surfaces as a domain error at the API edge.
__all__ = ["ProviderConfigError", "run_workflow"]

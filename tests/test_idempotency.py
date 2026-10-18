import json
import threading

import pytest

from app.engine.errors import IdempotencyConflict, WorkflowFailed
from app.engine.runner import run_workflow
from app.storage.models import Case, CaseNote


INPUTS = {
    "employee_id": "E10234",
    "case_transcript": "Asked how PTO carryover works. Has about 30 unused hours.",
    "tone": "empathetic",
}


def _seed(db_session):
    from app.storage.models import EmployeeRecord

    existing = db_session.query(EmployeeRecord).filter_by(employee_id="E10234").first()
    if not existing:
        db_session.add(
            EmployeeRecord(
                employee_id="E10234",
                metadata_json=json.dumps({"department": "Field Operations", "manager": "R. Alvarez"}),
                last_activities_json=json.dumps([{"type": "case", "summary": "PTO question"}]),
            )
        )
        db_session.commit()


def test_missing_idempotency_key_does_not_collapse_cases(db_session):
    _seed(db_session)
    first = run_workflow(
        {"employee_id": "E10234", "case_transcript": "PTO question for the first employee", "tone": "empathetic"},
        idempotency_key=None,
    )
    second = run_workflow(
        {"employee_id": "E10897", "case_transcript": "A different employee asked about parental leave", "tone": "formal"},
        idempotency_key=None,
    )
    assert first["case_id"] != second["case_id"]
    db_session.rollback()
    assert db_session.query(Case).filter(Case.id.in_([first["case_id"], second["case_id"]])).count() == 2


def test_same_key_returns_the_same_case(db_session):
    _seed(db_session)
    key = "same-key-returns-one"
    first = run_workflow(INPUTS, idempotency_key=key)
    second = run_workflow(INPUTS, idempotency_key=key)
    assert first["case_id"] == second["case_id"]
    assert second["status"] == "completed"
    db_session.rollback()
    notes = db_session.query(CaseNote).filter_by(case_id=first["case_id"]).count()
    assert notes == 1


def test_same_key_different_inputs_conflict(db_session):
    _seed(db_session)
    key = "same-key-different-body"
    run_workflow(INPUTS, idempotency_key=key)
    other = dict(INPUTS)
    other["case_transcript"] = "This is a different request and must not reuse the first case."
    with pytest.raises(IdempotencyConflict):
        run_workflow(other, idempotency_key=key)


def test_null_input_hash_same_body_is_a_hit_and_backfills(db_session):
    key = "legacy-null-hash-same"
    case = Case(
        status="completed",
        idempotency_key=key,
        input_hash=None,
        inputs_json=json.dumps(INPUTS),
        outputs_json=json.dumps({"subject": "Stored", "body": "Original draft"}),
        artifacts_json="{}",
    )
    db_session.add(case)
    db_session.commit()
    case_id = case.id

    result = run_workflow(INPUTS, idempotency_key=key)

    assert result["case_id"] == case_id
    assert result["outputs"]["body"] == "Original draft"
    db_session.rollback()
    stored = db_session.query(Case).filter_by(id=case_id).one()
    assert stored.input_hash


def test_null_input_hash_different_body_conflicts_without_overwriting(db_session):
    key = "legacy-null-hash-different"
    case = Case(
        status="completed",
        idempotency_key=key,
        input_hash=None,
        inputs_json=json.dumps(INPUTS, sort_keys=True),
        outputs_json=json.dumps({"subject": "Stored", "body": "Original draft"}),
        artifacts_json="{}",
    )
    db_session.add(case)
    db_session.commit()
    case_id = case.id

    other = dict(INPUTS)
    other["employee_id"] = "E99999"
    other["case_transcript"] = "A different employee asked about a different policy."
    with pytest.raises(IdempotencyConflict):
        run_workflow(other, idempotency_key=key)

    db_session.rollback()
    stored = db_session.query(Case).filter_by(id=case_id).one()
    assert stored.input_hash is None
    assert stored.status == "completed"
    assert json.loads(stored.outputs_json)["body"] == "Original draft"


def test_concurrent_requests_create_one_case_and_one_note(db_session):
    _seed(db_session)
    key = "concurrent-idempotency-key"
    results = []
    errors = []

    def worker():
        try:
            results.append(run_workflow(INPUTS, idempotency_key=key))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert {result["case_id"] for result in results} == {results[0]["case_id"]}
    assert all(result["status"] == "completed" for result in results)
    case_id = results[0]["case_id"]
    db_session.rollback()
    assert db_session.query(CaseNote).filter_by(case_id=case_id).count() == 1


def test_failed_case_is_not_returned_as_success_and_can_resume(db_session, monkeypatch):
    _seed(db_session)
    import app.engine.runner as runner

    calls = {"n": 0}
    original = dict(runner.STEPS)

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic failure")
        return original["summarize_and_extract_actions"](ctx)

    monkeypatch.setattr(
        runner,
        "STEPS",
        [(name, flaky if name == "summarize_and_extract_actions" else fn) for name, fn in runner.STEPS],
    )
    key = "resume-after-failure"
    with pytest.raises(WorkflowFailed) as caught:
        run_workflow(INPUTS, idempotency_key=key)
    assert caught.value.step == "summarize_and_extract_actions"

    db_session.rollback()
    case = db_session.query(Case).filter_by(idempotency_key=key).one()
    assert case.status == "failed"

    monkeypatch.setattr(runner, "STEPS", list(original.items()))
    resumed = run_workflow(INPUTS, idempotency_key=key)
    assert resumed["status"] == "completed"
    assert resumed["case_id"] == case.id
    db_session.rollback()
    assert db_session.query(CaseNote).filter_by(case_id=case.id).count() == 1

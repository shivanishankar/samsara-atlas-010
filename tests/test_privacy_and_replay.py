import json

import pytest

from app.engine.runner import run_workflow
from app.providers.base import CompletionResponse, ModelProvider, ProviderConfigError
from app.providers.factory import get_provider
from app.providers.stub import build_stub_text
from app.storage.models import Case, CaseNote, EmployeeRecord, Step


def _seed(db_session, employee_id="E10234", metadata=None, activities=None):
    existing = db_session.query(EmployeeRecord).filter_by(employee_id=employee_id).first()
    if existing:
        return
    db_session.add(
        EmployeeRecord(
            employee_id=employee_id,
            metadata_json=json.dumps(metadata or {"department": "Field Operations", "tenure_years": 2.4}),
            last_activities_json=json.dumps(activities or []),
        )
    )
    db_session.commit()


def test_validation_error_does_not_echo_transcript(client):
    secret = "SECRET_TRANSCRIPT_XYZ_12345"
    resp = client.post(
        "/cases",
        json={"inputs": {"employee_id": "", "case_transcript": secret, "tone": "empathetic"}},
    )
    assert resp.status_code == 422
    assert secret not in resp.text


def test_step_failure_does_not_leak_transcript(client, db_session, monkeypatch):
    _seed(db_session)
    import app.engine.runner as runner

    def boom(ctx):
        raise RuntimeError(ctx.inputs["case_transcript"])

    monkeypatch.setattr(
        runner,
        "STEPS",
        [(name, boom if name == "summarize_and_extract_actions" else fn) for name, fn in runner.STEPS],
    )
    secret = "SECRET_TRANSCRIPT_XYZ_12345"
    resp = client.post(
        "/cases",
        json={
            "inputs": {"employee_id": "E10234", "case_transcript": secret, "tone": "direct"},
            "idempotency_key": "redact-failure",
        },
    )
    assert resp.status_code == 500
    assert secret not in resp.text
    body = resp.json()["detail"]
    assert body["step"] == "summarize_and_extract_actions"
    assert body["error_type"] == "RuntimeError"

    fetched = client.get(f"/cases/{body['case_id']}")
    payload = fetched.json()
    assert payload["case"]["status"] == "failed"
    assert secret not in json.dumps(payload["steps"])
    assert payload["case"]["inputs"]["case_transcript"] == secret


def test_prompt_and_storage_drop_unlisted_employee_fields(db_session, monkeypatch):
    marker = "PRIOR_CASE_MEDICAL_LEAVE_MARKER"
    ssn = "999-00-1234"
    _seed(
        db_session,
        employee_id="E55555",
        metadata={
            "department": "People",
            "tenure_years": 4,
            "manager": "Hidden Manager",
            "ssn": ssn,
        },
        activities=[{"type": "case", "date": "2026-01-01", "summary": marker, "diagnosis": "private"}],
    )
    seen = []

    class Capture(ModelProvider):
        model_name = "capture"

        def complete(self, prompt, **kwargs):
            seen.append(prompt)
            return CompletionResponse(text=build_stub_text(prompt, **kwargs), raw={"choices": [{"message": {"content": "not json"}}]})

    monkeypatch.setattr("app.engine.steps.get_provider", lambda: Capture())
    result = run_workflow(
        {
            "employee_id": "E55555",
            "case_transcript": "How many PTO hours can I carry over?",
            "tone": "formal",
        },
        idempotency_key="privacy-allowlist",
    )
    assert result["status"] == "completed"
    combined = "\n".join(seen)
    assert "Hidden Manager" not in combined
    assert ssn not in combined
    assert marker not in combined
    assert "People" in combined

    db_session.rollback()
    step = (
        db_session.query(Step)
        .filter_by(case_id=result["case_id"], name="retrieve_employee_context")
        .one()
    )
    stored = json.loads(step.output_json)
    assert "ssn" not in json.dumps(stored)
    assert "diagnosis" not in json.dumps(stored)
    assert stored["metadata"]["manager"] == "Hidden Manager"
    assert stored["last_activities"][0]["summary"] == marker


def test_unknown_employee_does_not_write_a_note(db_session):
    result = run_workflow(
        {
            "employee_id": "E404404",
            "case_transcript": "I need to know my PTO balance.",
            "tone": "direct",
        },
        idempotency_key="missing-employee",
    )
    assert result["status"] == "completed"
    assert result["outputs"]["subject"] == "Employee record not found"
    db_session.rollback()
    assert db_session.query(CaseNote).filter_by(case_id=result["case_id"]).count() == 0


def test_replay_links_to_the_original_case(client, db_session):
    _seed(db_session)
    created = client.post(
        "/cases",
        json={
            "inputs": {
                "employee_id": "E10234",
                "case_transcript": "Question about expense receipts from a trip.",
                "tone": "formal",
            },
            "idempotency_key": "replay-parent",
        },
    )
    assert created.status_code == 200
    parent_id = created.json()["case_id"]
    replayed = client.post(f"/cases/{parent_id}/replay")
    assert replayed.status_code == 200
    body = replayed.json()
    assert body["case_id"] != parent_id
    assert body["replay_of_case_id"] == parent_id

    parent = client.get(f"/cases/{parent_id}")
    assert parent.json()["replay_case_ids"] == [body["case_id"]]
    db_session.rollback()
    child = db_session.query(Case).filter_by(id=body["case_id"]).one()
    assert child.replay_of_case_id == parent_id


def test_engine_uses_normalized_text_not_provider_raw(db_session, monkeypatch):
    _seed(db_session, employee_id="E77777", metadata={"department": "Engineering", "tenure_years": 1})

    class Misleading(ModelProvider):
        model_name = "misleading"

        def complete(self, prompt, **kwargs):
            return CompletionResponse(
                text=build_stub_text(prompt, **kwargs),
                raw={"choices": [{"message": {"content": "this is not json"}}]},
            )

    monkeypatch.setattr("app.engine.steps.get_provider", lambda: Misleading())
    result = run_workflow(
        {
            "employee_id": "E77777",
            "case_transcript": "How does PTO carryover work?",
            "tone": "empathetic",
        },
        idempotency_key="provider-text-not-raw",
    )
    assert result["status"] == "completed"
    assert result["outputs"]["subject"]


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("PROVIDER_TYPE", "definitely-not-a-provider")
    with pytest.raises(ProviderConfigError):
        get_provider()

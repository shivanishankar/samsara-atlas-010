import hashlib
import json
import logging
import os
import time
from pathlib import Path

from pydantic import ValidationError

from app.engine.errors import MalformedModelOutput
from app.privacy import model_employee_view
from app.providers.base import TransientProviderError
from app.providers.factory import get_provider
from app.schemas import ExtractActionsOutput, StructuredResponse
from app.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)

MAX_MODEL_ATTEMPTS = 3
_RETRYABLE = (TimeoutError, ConnectionError, BrokenPipeError, MalformedModelOutput, TransientProviderError)
_SNIPPET_SEPARATOR = "\n\n---\n\n"
_NOT_FOUND_SUMMARY = {
    "summary": "No HRIS record was found for this employee.",
    "action_items": [],
    "next_steps": ["Confirm the employee id before drafting a response."],
}


def _load_prompt(name: str) -> str:
    path = Path(__file__).parent.parent / "prompts" / f"{name}.txt"
    return path.read_text()


def prompt_fingerprints() -> dict[str, str]:
    """Short hashes of the prompt files used for this run, so drift is detectable."""
    fingerprints = {}
    for name in ("summarize", "generate_response"):
        fingerprints[name] = hashlib.sha256(_load_prompt(name).encode()).hexdigest()[:16]
    return fingerprints


def fill_prompt(template: str, **kwargs: object) -> str:
    """Insert values without letting braces in case text rewrite the template."""
    safe = {}
    for key, value in kwargs.items():
        text = "" if value is None else str(value)
        safe[key] = text.replace("{", "{{").replace("}", "}}")
    return template.format(**safe)


def _parse_json_from_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        body: list[str] = []
        in_block = False
        for line in lines:
            if line.strip().startswith("```"):
                if in_block:
                    break
                in_block = True
                continue
            if in_block:
                body.append(line)
        text = "\n".join(body).strip() or text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start:end])
    if not isinstance(parsed, dict):
        raise MalformedModelOutput("model output was not an object")
    return parsed


def _backoff(attempt: int) -> None:
    time.sleep(min(0.05 * (2 ** (attempt - 1)), 1.0))


def _complete_json(ctx, step_name: str, prompt: str, accept=None, **kwargs) -> dict:
    """Retry the model call only. A bad reply is retried; the workflow is not."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        ctx.step_attempts[step_name] = attempt
        try:
            provider = get_provider()
            ctx.artifacts["provider"] = os.getenv("PROVIDER_TYPE", "stub").strip().lower() or "stub"
            ctx.artifacts["model_name"] = getattr(provider, "model_name", type(provider).__name__)
            text = provider.complete(prompt, **kwargs).text
            if not isinstance(text, str) or not text.strip():
                raise MalformedModelOutput("empty completion")
            try:
                parsed = _parse_json_from_text(text)
            except (json.JSONDecodeError, ValueError):
                raise MalformedModelOutput("invalid json")
            if accept is not None:
                return accept(parsed)
            return parsed
        except _RETRYABLE as exc:
            last_error = exc
            logger.warning(
                "llm_call_failed case_id=%s step=%s attempt=%s error_type=%s",
                ctx.case_id,
                step_name,
                attempt,
                type(exc).__name__,
            )
            if attempt == MAX_MODEL_ATTEMPTS:
                break
            _backoff(attempt)
    assert last_error is not None
    raise last_error


def _str_list(value, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        raise MalformedModelOutput(f"{field_name} was not a list")
    cleaned = []
    for item in items[:20]:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip()[:500])
    return cleaned


def _employee_missing(ctx) -> bool:
    employee = ctx.step_outputs.get("retrieve_employee_context") or {}
    return employee.get("found") is False


def _format_snippets(snippets) -> str:
    if not snippets:
        return "(none retrieved)"
    return _SNIPPET_SEPARATOR.join(str(snippet)[:600] for snippet in list(snippets)[:5])


def retrieve_employee_context_step(ctx):
    registry = get_tool_registry()
    return registry.execute("retrieve_employee_context", employee_id=ctx.inputs["employee_id"])


def retrieve_policy_snippets_step(ctx):
    registry = get_tool_registry()
    snippets = registry.execute("retrieve_policy_snippets", query=ctx.inputs["case_transcript"])
    ctx.artifacts["policies_retrieved"] = [
        snippet.split("\n", 1)[0] for snippet in snippets if isinstance(snippet, str)
    ]
    return snippets


def summarize_and_extract_actions_step(ctx):
    if _employee_missing(ctx):
        return dict(_NOT_FOUND_SUMMARY)
    snippets = ctx.step_outputs.get("retrieve_policy_snippets") or []
    prompt = fill_prompt(
        _load_prompt("summarize"),
        employee_id=ctx.inputs["employee_id"],
        case_transcript=ctx.inputs["case_transcript"],
        policy_snippets=_format_snippets(snippets),
    )
    return _complete_json(
        ctx,
        "summarize_and_extract_actions",
        prompt,
        accept=_accept_summary,
    )


def _accept_summary(data: dict) -> dict:
    summary = data.get("summary", data.get("Summary", ""))
    if not isinstance(summary, str) or not summary.strip():
        raise MalformedModelOutput("summary missing")
    normalized = {
        "summary": summary.strip()[:2000],
        "action_items": _str_list(data.get("action_items", data.get("actionItems", [])), "action_items"),
        "next_steps": _str_list(data.get("next_steps", data.get("nextSteps", [])), "next_steps"),
    }
    try:
        return ExtractActionsOutput(**normalized).model_dump()
    except ValidationError:
        raise MalformedModelOutput("summary schema") from None


def _ground_citations(cited, snippets: list[str]) -> list[str]:
    if not isinstance(cited, list):
        return []
    normalized_snippets = [" ".join(snippet.split()) for snippet in snippets if isinstance(snippet, str)]
    grounded = []
    for item in cited:
        if not isinstance(item, str):
            continue
        candidate = " ".join(item.split())
        if not candidate:
            continue
        if any(candidate in snippet or snippet[:120] in candidate for snippet in normalized_snippets):
            grounded.append(item.strip()[:500])
    return grounded


def generate_structured_response_step(ctx):
    tone = ctx.inputs["tone"]
    if _employee_missing(ctx):
        return StructuredResponse(
            subject="Employee record not found",
            body=(
                "No HRIS record was found for this employee id. No response was drafted "
                "and no case note was written. Confirm the employee id before continuing."
            ),
            tone=tone,
            cited_snippets=[],
        ).model_dump()

    employee = ctx.step_outputs.get("retrieve_employee_context") or {}
    snippets = ctx.step_outputs.get("retrieve_policy_snippets") or []
    actions = ctx.step_outputs.get("summarize_and_extract_actions") or {}
    prompt = fill_prompt(
        _load_prompt("generate_response"),
        employee_id=ctx.inputs["employee_id"],
        employee_context=json.dumps(model_employee_view(employee), sort_keys=True),
        policy_snippets=_format_snippets(snippets),
        actions=json.dumps(actions, sort_keys=True),
        tone=tone,
    )
    snippet_list = [snippet for snippet in snippets if isinstance(snippet, str)]

    def accept(data: dict) -> dict:
        subject = data.get("subject", data.get("Subject", ""))
        body = data.get("body", data.get("Body", ""))
        if not isinstance(subject, str) or not subject.strip():
            raise MalformedModelOutput("subject missing")
        if not isinstance(body, str) or not body.strip():
            raise MalformedModelOutput("body missing")
        normalized = {
            "subject": subject.strip()[:200],
            "body": body.strip()[:4000],
            "tone": tone,
            "cited_snippets": _ground_citations(
                data.get("cited_snippets", data.get("citedSnippets", [])),
                snippet_list,
            ),
        }
        try:
            return StructuredResponse(**normalized).model_dump()
        except ValidationError:
            raise MalformedModelOutput("response schema") from None

    return _complete_json(ctx, "generate_structured_response", prompt, accept=accept, tone=tone)


def post_case_note_to_hris_step(ctx):
    if _employee_missing(ctx):
        return {"skipped": True, "reason": "employee_not_found"}
    response = ctx.step_outputs.get("generate_structured_response") or {}
    subject = str(response.get("subject", ""))[:200]
    body = str(response.get("body", ""))[:200]
    note = f"Draft response (not sent): {subject} - {body}"[:1000]
    registry = get_tool_registry()
    return registry.execute(
        "post_case_note_to_hris",
        case_id=ctx.case_id,
        employee_id=ctx.inputs["employee_id"],
        note=note,
        dedupe_key=f"case:{ctx.case_id}:draft_response",
    )


STEPS = [
    ("retrieve_employee_context", retrieve_employee_context_step),
    ("retrieve_policy_snippets", retrieve_policy_snippets_step),
    ("summarize_and_extract_actions", summarize_and_extract_actions_step),
    ("generate_structured_response", generate_structured_response_step),
    ("post_case_note_to_hris", post_case_note_to_hris_step),
]

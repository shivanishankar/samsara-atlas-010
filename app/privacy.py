"""Boundaries for employee data that leaves the HRIS.

The model provider is not the system of record. Only fields needed to apply
policy are placed in a prompt. A slightly wider, still allowlisted snapshot is
what gets stored on the case for the HR partner reviewing the draft.
"""

MODEL_METADATA_FIELDS = (
    "department",
    "level",
    "location",
    "tenure_years",
    "employment_status",
)

# Manager is another person's data. It stays available to the HR partner and
# stays out of the model prompt.
AUDIT_METADATA_FIELDS = MODEL_METADATA_FIELDS + ("manager",)

ACTIVITY_FIELDS = ("type", "date", "summary")


def audit_employee_view(employee_id: str, metadata: object, activities: object, found: bool) -> dict:
    """Drop any HRIS field that this workflow has not explicitly opted into."""
    raw_metadata = metadata if isinstance(metadata, dict) else {}
    safe_metadata = {key: raw_metadata[key] for key in AUDIT_METADATA_FIELDS if key in raw_metadata}
    safe_activities = []
    if isinstance(activities, list):
        for item in activities:
            if not isinstance(item, dict):
                continue
            safe_activities.append({key: item[key] for key in ACTIVITY_FIELDS if key in item})
    return {
        "employee_id": employee_id,
        "metadata": safe_metadata,
        "last_activities": safe_activities,
        "found": found,
    }


def model_employee_view(employee: dict) -> dict:
    """The subset of an employee snapshot that may be sent to a model."""
    metadata = employee.get("metadata") if isinstance(employee.get("metadata"), dict) else {}
    view = {"found": bool(employee.get("found"))}
    for key in MODEL_METADATA_FIELDS:
        if key in metadata and metadata[key] not in (None, ""):
            view[key] = metadata[key]
    return view


def redact(text: str, secrets: list[str]) -> str:
    """Remove known case content from an error string before it is stored or logged."""
    redacted = (text or "").splitlines()[0]
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 8 and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted[:400]


def collect_secrets(inputs: dict, employee: dict | None = None) -> list[str]:
    secrets: list[str] = []
    for key in ("case_transcript", "employee_id"):
        value = inputs.get(key)
        if isinstance(value, str):
            secrets.append(value)
    if not employee:
        return secrets
    metadata = employee.get("metadata") if isinstance(employee.get("metadata"), dict) else {}
    for value in metadata.values():
        if isinstance(value, str):
            secrets.append(value)
    activities = employee.get("last_activities") if isinstance(employee.get("last_activities"), list) else []
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        for value in activity.values():
            if isinstance(value, str):
                secrets.append(value)
    return secrets

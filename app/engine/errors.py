class InvalidWorkflowInput(Exception):
    """Inputs failed schema validation. The message intentionally has no payload."""


class IdempotencyConflict(Exception):
    """The same idempotency key was reused with a different request body."""


class WorkflowFailed(Exception):
    """A case finished in the failed state. Safe to show: id, step, error type."""

    def __init__(self, case_id: int, step: str, error_type: str):
        self.case_id = case_id
        self.step = step
        self.error_type = error_type
        super().__init__(f"Workflow failed at {step} ({error_type})")


class WorkflowNotFinished(Exception):
    """Another worker still holds this idempotency key and did not finish in time."""

    def __init__(self, case_id: int):
        self.case_id = case_id
        super().__init__(f"Case {case_id} is still running")


class MalformedModelOutput(Exception):
    """The model reply was empty or not the JSON object this step requires."""

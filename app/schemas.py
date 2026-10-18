from typing import Literal

from pydantic import BaseModel, Field


class CaseInputs(BaseModel):
    employee_id: str = Field(min_length=1, max_length=64)
    case_transcript: str = Field(min_length=1, max_length=8000)
    tone: Literal["empathetic", "direct", "formal"]


class StructuredResponse(BaseModel):
    subject: str
    body: str
    tone: Literal["empathetic", "direct", "formal"]
    cited_snippets: list[str]


class ExtractActionsOutput(BaseModel):
    summary: str
    action_items: list[str]
    next_steps: list[str]

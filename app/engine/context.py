import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionContext:
    case_id: int
    inputs: dict
    artifacts: dict
    step_outputs: dict[str, Any] = field(default_factory=dict)
    step_attempts: dict[str, int] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

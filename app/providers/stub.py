import hashlib
import json
import random

from app.providers.base import CompletionResponse, ModelProvider


def _policy_excerpt(prompt: str) -> str:
    """Cite text that actually appeared in the prompt, not a fabricated policy line."""
    marker = "Relevant policy excerpts:"
    if marker not in prompt:
        return ""
    rest = prompt.split(marker, 1)[1]
    rest = rest.split("Summary and actions", 1)[0]
    block = rest.split("\n\n---\n\n", 1)[0].strip()
    lines = block.splitlines()
    if lines and lines[0].lstrip().startswith("[") and "]" in lines[0]:
        lines[0] = lines[0].split("]", 1)[1].lstrip()
        if not lines[0]:
            lines = lines[1:]
    text = "\n".join(lines).strip()
    if not text or text.startswith("(none"):
        return ""
    return text[:180].strip()


def build_stub_text(prompt: str, **kwargs) -> str:
    """Deterministic JSON (or plain text) for a prompt. Shared by every local provider."""
    key = hashlib.sha256((prompt + str(sorted(kwargs.items()))).encode()).hexdigest()
    lowered = prompt.lower()
    if "subject" in lowered and "cited_snippets" in lowered:
        excerpt = _policy_excerpt(prompt)
        return json.dumps({
            "subject": f"Following up on your case: {key[:12]}",
            "body": "Thanks for reaching out. Here is what I found and the next steps.",
            "tone": kwargs.get("tone", "empathetic"),
            "cited_snippets": [excerpt] if excerpt else [],
        })
    if "summary" in lowered and "action_items" in lowered:
        return json.dumps({
            "summary": f"Case summary for {key[:8]}",
            "action_items": ["Confirm eligibility with HRBP", "Share PTO balance"],
            "next_steps": ["Send policy link", "Follow up in 3 business days"],
        })
    return f"[stub response for {key[:16]}]"


class StubProvider(ModelProvider):
    """Deterministic stub. Same prompt and kwargs produce the same text.

    A small timeout chance exercises retry behavior. The raw payload is an
    OpenAI-shaped envelope on purpose; callers must ignore it and use .text.
    """

    model_name = "stub"

    def __init__(self, timeout_chance: float = 0.02):
        self.timeout_chance = timeout_chance

    def complete(self, prompt: str, **kwargs) -> CompletionResponse:
        if random.random() < self.timeout_chance:
            raise TimeoutError("Stub provider simulated timeout")
        text = build_stub_text(prompt, **kwargs)
        raw = {
            "choices": [{"message": {"content": text, "role": "assistant"}}],
            "model": self.model_name,
        }
        return CompletionResponse(text=text, raw=raw)

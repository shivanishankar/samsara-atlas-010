"""Alternate local provider. Different raw shape, same .text contract."""

from app.providers.base import CompletionResponse, ModelProvider
from app.providers.stub import build_stub_text


class LocalStubProvider(ModelProvider):
    model_name = "local-stub"

    def complete(self, prompt: str, **kwargs) -> CompletionResponse:
        text = build_stub_text(prompt, **kwargs)
        raw = {"output": {"text": text}, "status": "ok"}
        return CompletionResponse(text=text, raw=raw)

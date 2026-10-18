from abc import ABC, abstractmethod
from typing import Any


class TransientProviderError(Exception):
    """Provider failed in a way that is safe to retry. Message must not include prompt text."""


class ProviderConfigError(Exception):
    """PROVIDER_TYPE is missing, unknown, or the selected provider cannot start."""


class CompletionResponse:
    """Normalized response. Callers use .text and do not read provider-specific raw payloads."""

    def __init__(self, text: str, raw: Any = None):
        self.text = text
        self.raw = raw


class ModelProvider(ABC):
    model_name: str = "unknown"

    @abstractmethod
    def complete(self, prompt: str, **kwargs) -> CompletionResponse:
        pass

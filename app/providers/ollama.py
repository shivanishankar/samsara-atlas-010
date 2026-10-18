"""Ollama provider. Calls a local Ollama server. No API key required."""

import os

import requests

from app.providers.base import CompletionResponse, ModelProvider, ProviderConfigError, TransientProviderError


class OllamaProvider(ModelProvider):
    """Local Ollama completion. Errors never include the prompt."""

    def __init__(self, base_url: str | None = None, model: str = "llama3.2"):
        self.base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.model = os.getenv("OLLAMA_MODEL", model)
        self.model_name = self.model

    def complete(self, prompt: str, **kwargs) -> CompletionResponse:
        try:
            resp = requests.post(
                f"{self.base_url.rstrip('/')}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0, "num_predict": 600},
                },
                timeout=120,
            )
        except requests.Timeout:
            raise TimeoutError("model provider timed out") from None
        except requests.ConnectionError:
            raise ConnectionError("model provider is unreachable") from None

        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientProviderError(f"provider HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise ProviderConfigError(f"provider HTTP {resp.status_code}")

        data = resp.json()
        text = data.get("response", "")
        if not isinstance(text, str):
            text = ""
        return CompletionResponse(text=text, raw={"model": self.model})

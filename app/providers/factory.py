import os

from app.providers.base import ModelProvider, ProviderConfigError
from app.providers.stub import StubProvider

_SUPPORTED = ("stub", "ollama", "local")


def get_provider() -> ModelProvider:
    """Return the configured provider. Never silently substitute a different one."""
    provider_type = os.getenv("PROVIDER_TYPE", "stub").strip().lower() or "stub"
    if provider_type == "stub":
        return StubProvider()
    if provider_type == "ollama":
        try:
            from app.providers.ollama import OllamaProvider
        except ImportError as exc:
            raise ProviderConfigError("Ollama provider dependencies are not installed") from exc
        return OllamaProvider()
    if provider_type == "local":
        try:
            from app.providers.local_stub import LocalStubProvider
        except ImportError as exc:
            raise ProviderConfigError("Local provider is not installed") from exc
        return LocalStubProvider()
    raise ProviderConfigError(
        f"Unknown PROVIDER_TYPE '{provider_type}'. Expected one of: {', '.join(_SUPPORTED)}."
    )

"""Model factory — the ONLY place in the codebase that knows which provider is in use.

LangChain/LangGraph are provider-agnostic. Because the agent only ever talks to
tools, swapping providers touches this function and nothing else. Imports are
lazy, so you only need the SDK for the provider you actually configured.
"""

from typing import Any

from src.config import get_settings

# Provider → (pip package, module path, class name). Held as data so the error
# below can name the exact package to install.
_PROVIDERS: dict[str, tuple[str, str, str]] = {
    "google": ("langchain-google-genai", "langchain_google_genai", "ChatGoogleGenerativeAI"),
    "openai": ("langchain-openai", "langchain_openai", "ChatOpenAI"),
    "anthropic": ("langchain-anthropic", "langchain_anthropic", "ChatAnthropic"),
    "ollama": ("langchain-ollama", "langchain_ollama", "ChatOllama"),
}

# Each provider spells the credential differently. Ollama runs locally, no key.
_API_KEY_KWARG: dict[str, str] = {
    "google": "google_api_key",
    "openai": "api_key",
    "anthropic": "api_key",
}


def build_llm(**overrides: Any):
    """Return a LangChain chat model for the configured provider.

    Deliberately uncached — tests inject a fake model rather than calling this,
    and a long-running process builds it once at startup anyway.
    """
    settings = get_settings()
    provider = settings.model_provider

    if provider not in _PROVIDERS:
        raise ValueError(
            f"Unknown MODEL_PROVIDER {provider!r}. Expected one of: {sorted(_PROVIDERS)}"
        )

    package, module_path, class_name = _PROVIDERS[provider]

    try:
        module = __import__(module_path, fromlist=[class_name])
    except ImportError as exc:  # pragma: no cover — depends on local install
        raise ImportError(
            f"MODEL_PROVIDER={provider!r} needs the {package!r} package. "
            f"Install it with:  pip install {package}"
        ) from exc

    kwargs: dict[str, Any] = {"model": settings.model_name}
    if key_kwarg := _API_KEY_KWARG.get(provider):
        kwargs[key_kwarg] = settings.model_api_key
    kwargs.update(overrides)

    return getattr(module, class_name)(**kwargs)

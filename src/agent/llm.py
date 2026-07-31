"""Model factory — the ONLY place in the codebase that knows which provider is in use.

LangChain/LangGraph are provider-agnostic. Because the agent only ever talks to
tools, swapping providers touches this function and nothing else. That is worth
saying out loud in a design discussion.
"""

from src.config import get_settings


def build_llm():
    """Return a LangChain chat model for the configured provider."""
    s = get_settings()

    # TODO(Phase 3): import lazily per provider so only the chosen SDK is required.
    #   google    -> langchain_google_genai.ChatGoogleGenerativeAI
    #   openai    -> langchain_openai.ChatOpenAI
    #   anthropic -> langchain_anthropic.ChatAnthropic
    #   ollama    -> langchain_ollama.ChatOllama
    raise NotImplementedError(f"Phase 3: wire up provider {s.model_provider!r}")

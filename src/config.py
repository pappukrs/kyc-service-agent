"""Application configuration.

Everything the app needs comes from the environment — 12-factor, no hard-coded
provider names or connection strings anywhere else in the codebase.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Model provider — the agent only ever talks to tools, so swapping the
    # provider touches exactly one factory function (src/agent/llm.py).
    model_provider: Literal["google", "openai", "anthropic", "ollama"] = "google"
    model_name: str = "gemini-2.0-flash"
    model_api_key: str = "changeme"

    # MongoDB
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "kyc_servicing"

    # Kafka
    kafka_bootstrap: str = "localhost:9092"
    kafka_tasks_topic: str = "servicing.tasks"
    kafka_audit_topic: str = "servicing.audit"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    jwt_secret: str = "dev-only-change-me"
    log_level: str = "INFO"

    # Agent
    agent_runtime: Literal["langgraph", "adk"] = "langgraph"
    max_tool_calls_per_turn: int = 8
    tool_timeout_seconds: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()

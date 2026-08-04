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
    # How long a publish may take before it is treated as a broker outage. Short
    # on purpose: this runs inside a customer's turn, and aiokafka's own
    # bootstrap timeout is tens of seconds — long enough to look like a hang.
    kafka_publish_timeout_seconds: float = 5.0

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    jwt_secret: str = "dev-only-change-me"
    log_level: str = "INFO"

    # Worker — how long the simulated document check takes. There is no real
    # scanner behind this; the delay is what makes the async path observable
    # (the customer is told "in progress", the result lands later). Tests set 0.
    verification_latency_seconds: float = 3.0

    # Agent
    agent_runtime: Literal["langgraph", "adk"] = "langgraph"

    # Bounds on a turn. A tool that never returns and a model that never stops
    # calling tools are the same failure seen from the customer's side: no
    # answer. Both are bounded here rather than left to whatever the process
    # underneath decides to do.
    max_tool_calls_per_turn: int = 8
    tool_timeout_seconds: float = 15.0
    # Total attempts for a tool that may be retried at all — 1 means "no retry".
    tool_retry_attempts: int = 3
    tool_retry_backoff_seconds: float = 0.25
    # The whole call, retries and backoff included. Without this, a retry policy
    # only multiplies the wait it was meant to bound: 3 × 15s is 45s of a
    # customer looking at a spinner.
    tool_deadline_seconds: float = 25.0


@lru_cache
def get_settings() -> Settings:
    return Settings()

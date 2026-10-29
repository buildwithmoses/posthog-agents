"""Runtime configuration, overridable by environment variable.

Models go through OpenRouter (langchain-openrouter). Lessons carried from earlier builds:
- Claude 5.5 through OpenRouter rejects forced tool_choice, so structured output uses JSON schema.
- ChatOpenRouter's `timeout` is in MILLISECONDS.
- The judge is a different model family from the model it grades.

PostHog: POSTHOG_HOST + POSTHOG_PERSONAL_API_KEY. Without a real key, the agents talk to the mock PostHog in
src/posthog_agents/mock (same endpoints and payload shapes), started with `uv run posthog-agents mock`.
"""

from __future__ import annotations

import os
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL = "openrouter:anthropic/claude-haiku-5.5"
DEFAULT_JUDGE_MODEL = "openrouter:openai/gpt-6.1-terra"
APP_URL = "https://buildwithmoses.vercel.app"
APP_TITLE = "PostHog Agents (buildwithmoses)"
OPENROUTER_PROVIDER = {"require_parameters": True, "allow_fallbacks": True}
TIMEOUT_MS = 180_000
MOCK_PORT = int(os.getenv("PHA_MOCK_PORT", "8010"))

# Each agent reads its own PostHog project. In the mock, the project id picks which agent's data answers.
PROJECTS = {"churn-risk": 101, "replay-to-bug": 102, "ticket-enricher": 103, "llm-watch": 104, "experiment-readout": 105}


def load_env() -> None:
    """Load this project's .env, then fill unset vars from .env files further up. Real env vars win."""
    from dotenv import dotenv_values

    for folder in (PROJECT_ROOT, *PROJECT_ROOT.parents):
        env_file = folder / ".env"
        if env_file.is_file():
            for key, value in dotenv_values(env_file).items():
                if value and not os.environ.get(key):
                    os.environ[key] = value


def model_id() -> str:
    return os.getenv("PHA_MODEL", DEFAULT_MODEL)


def judge_model_id() -> str:
    return os.getenv("PHA_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)


def get_model(mid: str | None = None, **kwargs) -> BaseChatModel:
    mid = mid or model_id()
    if mid.startswith("openrouter:"):
        kwargs.setdefault("timeout", TIMEOUT_MS)
        kwargs.setdefault("max_retries", 0)
        return init_chat_model(mid, app_url=APP_URL, app_title=APP_TITLE,
                               openrouter_provider=kwargs.pop("openrouter_provider", OPENROUTER_PROVIDER), **kwargs)
    return init_chat_model(mid, **kwargs)


def posthog_host() -> str:
    return os.getenv("POSTHOG_HOST", f"http://127.0.0.1:{MOCK_PORT}").rstrip("/")


def posthog_key() -> str:
    return os.getenv("POSTHOG_PERSONAL_API_KEY", "phx_mock_key")


def project_id(slug: str) -> int:
    return int(os.getenv(f"POSTHOG_PROJECT_{slug.upper().replace('-', '_')}", PROJECTS[slug]))


def data_dir(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath("fixtures", *parts)

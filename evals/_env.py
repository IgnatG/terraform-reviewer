"""Bridge the app's ``.env``-loaded settings into the process environment.

The LangSmith SDK and the openevals judge model (via langchain ``init_chat_model``)
read their credentials from ``os.environ`` — *not* the app's pydantic ``settings``.
So a key that lives only in ``.env`` is invisible to them. ``settings`` does load
``.env``, so copy the relevant values across without clobbering anything already
exported (``setdefault``).
"""

from __future__ import annotations

import os
from typing import Any


def _set(name: str, secret: Any) -> None:
    if secret is not None:
        os.environ.setdefault(name, secret.get_secret_value())


def bridge_env_from_settings(*, include_langsmith: bool = False) -> None:
    """Forward provider keys (always) and LangSmith creds (opt-in) from ``settings``."""

    from terraform_review_agent.config import settings

    _set("OPENAI_API_KEY", settings.openai_api_key)
    _set("ANTHROPIC_API_KEY", settings.anthropic_api_key)
    _set("GOOGLE_API_KEY", settings.google_api_key)
    if include_langsmith:
        _set("LANGSMITH_API_KEY", settings.langsmith_api_key)
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
        if settings.langsmith_endpoint:
            os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)

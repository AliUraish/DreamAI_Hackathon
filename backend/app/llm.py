"""One door to the model. OPENAI_KEY selects OpenAI; otherwise Anthropic is used when its credentials resolve.

Callers get either a validated pydantic object / a string, or `LLMUnavailable` with a
message fit to show a user. Nothing else about the provider leaks out of this module.
"""

from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

from .config import settings

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """The model could not be reached, is not configured, or declined."""


def openai_key() -> str | None:
    return os.environ.get("OPENAI_KEY") or os.environ.get("OPENAI_API_KEY") or None


def provider() -> str | None:
    if openai_key():
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    return None


def describe() -> dict[str, str | None]:
    name = provider()
    return {"provider": name, "model": settings.openai_model if name == "openai" else settings.llm_model if name else None}


def structured(system: str, prompt: str, schema: type[T], *, effort: str | None = None) -> T:
    name = provider()
    if name == "openai":
        return _openai_structured(system, prompt, schema, effort or settings.llm_effort)
    if name == "anthropic":
        return _anthropic_structured(system, prompt, schema, {"minimal": "low"}.get(effort or "", effort or settings.llm_effort))
    raise LLMUnavailable("no LLM key is configured (set OPENAI_KEY in backend/.env)")


class _Text(BaseModel):
    text: str


def text(system: str, prompt: str, *, effort: str = "minimal") -> str:
    """Short prose. "minimal" reasoning: a few sentences about given facts do not need deliberation, and it answers in seconds."""
    return structured(system, prompt, _Text, effort=effort).text.strip()


# --- OpenAI -----------------------------------------------------------------------


def _openai_structured(system: str, prompt: str, schema: type[T], effort: str) -> T:
    import openai

    client = openai.OpenAI(api_key=openai_key(), timeout=600)
    kwargs = {}
    if settings.openai_model.startswith(("gpt-5", "o")):  # reasoning models take an effort; others reject the field
        kwargs["reasoning"] = {"effort": {"xhigh": "high", "max": "high"}.get(effort, effort)}  # minimal | low | medium | high
    try:
        response = client.responses.parse(model=settings.openai_model, instructions=system, input=prompt, text_format=schema, **kwargs)
    except openai.AuthenticationError as exc:
        raise LLMUnavailable("the OpenAI key was rejected (check OPENAI_KEY in backend/.env)") from exc
    except openai.RateLimitError as exc:
        raise LLMUnavailable("OpenAI rate limit or quota reached") from exc
    except openai.NotFoundError as exc:
        raise LLMUnavailable(f"OpenAI does not know the model '{settings.openai_model}' (set CHOWKIDAAR_OPENAI_MODEL)") from exc
    except openai.APIStatusError as exc:
        raise LLMUnavailable(f"OpenAI API error {exc.status_code}: {exc.message}") from exc
    except openai.APIConnectionError as exc:
        raise LLMUnavailable("could not reach the OpenAI API") from exc
    if response.status == "incomplete":
        raise LLMUnavailable("the answer did not fit in the output limit")
    if response.output_parsed is None:
        raise LLMUnavailable("the model declined or returned no structured result")
    return response.output_parsed


# --- Anthropic ----------------------------------------------------------------------


def _anthropic_structured(system: str, prompt: str, schema: type[T], effort: str) -> T:
    import anthropic

    try:
        with anthropic.Anthropic().messages.stream(
            model=settings.llm_model, max_tokens=64000, system=system, thinking={"type": "adaptive"},
            output_config={"effort": effort}, output_format=schema, messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise LLMUnavailable("the Anthropic key was rejected") from exc
    except anthropic.RateLimitError as exc:
        raise LLMUnavailable("Anthropic rate limit reached") from exc
    except anthropic.APIStatusError as exc:
        raise LLMUnavailable(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMUnavailable("could not reach the Anthropic API") from exc
    if message.stop_reason == "refusal":
        raise LLMUnavailable("the model declined this request")
    if message.stop_reason == "max_tokens":
        raise LLMUnavailable("the answer did not fit in the output limit")
    if message.parsed_output is None:
        raise LLMUnavailable("the model returned no structured result")
    return message.parsed_output

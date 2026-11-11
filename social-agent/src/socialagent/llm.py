"""Thin wrapper around the Anthropic SDK.

Three capabilities, all returning plain Python values so the rest of the agent (and the tests) never
touch SDK types:

* ``json``     - structured output (``output_config.format`` JSON schema)
* ``text``     - plain completion
* ``research`` - the server-side web_search tool, resuming ``pause_turn`` turns

The model never receives a tool that can post, delete or spend anything: it only returns a plan.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import anthropic

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = "web_search_20260209"


class LLMError(RuntimeError):
    """Anything that stops us getting a usable answer from Claude (message is owner-friendly)."""


class Refused(LLMError):
    """Claude declined the request (safety classifier)."""


class LLM(Protocol):
    def json(self, *, system: str, content: str | list[dict[str, Any]], schema: dict[str, Any],
             max_tokens: int = 8000) -> dict[str, Any]: ...

    def text(self, *, system: str, content: str | list[dict[str, Any]], max_tokens: int = 4000) -> str: ...

    def research(self, *, system: str, prompt: str, max_searches: int = 8) -> tuple[str, list[str]]: ...


class ClaudeLLM:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", fallbacks: bool = True,
                 client: Any = None):
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:  # built lazily so CLI commands that never call Claude work without a key
            self._client = anthropic.Anthropic(max_retries=3)
        return self._client

    # -- plumbing ----------------------------------------------------------------
    def _create(self, **kw: Any) -> Any:
        oc = dict(kw.pop("output_config", None) or {})
        oc.setdefault("effort", self.effort)  # on the default model thinking is always on; effort is the dial
        kw["output_config"] = oc
        kw.setdefault("model", self.model)
        try:
            if self.fallbacks:
                try:
                    return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
                except anthropic.BadRequestError as exc:
                    log.warning("Server-side fallbacks rejected (%s); continuing without them", exc)
                    self.fallbacks = False
            return self.client.messages.create(**kw)
        except anthropic.AuthenticationError as exc:
            raise LLMError("ANTHROPIC_API_KEY is missing or invalid.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"The Claude API key is not allowed to do this ({exc.message}).") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Claude is rate-limiting me right now; try again in a minute.") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMError("Claude took too long to answer; try again.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Cannot reach the Claude API (network problem).") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc

    @staticmethod
    def _check(resp: Any) -> None:
        reason = getattr(resp, "stop_reason", None)
        if reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) or "policy"
            raise Refused(f"Claude declined to handle this content ({category}).")
        if reason == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens); try again or simplify the request.")

    @staticmethod
    def _text_of(resp: Any) -> str:
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    # -- public API ----------------------------------------------------------------
    def json(self, *, system: str, content: str | list[dict[str, Any]], schema: dict[str, Any],
             max_tokens: int = 8000) -> dict[str, Any]:
        resp = self._create(
            max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        self._check(resp)
        raw = self._text_of(resp)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError("Claude returned malformed JSON; try again.") from exc
        if not isinstance(data, dict):
            raise LLMError("Claude returned an unexpected JSON shape; try again.")
        return data

    def text(self, *, system: str, content: str | list[dict[str, Any]], max_tokens: int = 4000) -> str:
        resp = self._create(max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}])
        self._check(resp)
        return self._text_of(resp).strip()

    def research(self, *, system: str, prompt: str, max_searches: int = 8, max_turns: int = 5) -> tuple[str, list[str]]:
        """Run a web-search-backed answer. Returns (text, source_urls)."""
        tools = [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_searches}]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        urls: list[str] = []
        resp: Any = None
        for _ in range(max_turns):
            resp = self._create(max_tokens=8000, system=system, messages=messages, tools=tools)
            self._check(resp)
            urls += _urls_in(resp)
            if getattr(resp, "stop_reason", None) == "pause_turn":  # server-side tool loop paused: resume it
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        if resp is None:
            raise LLMError("No response from Claude.")
        return self._text_of(resp).strip(), list(dict.fromkeys(urls))


def _urls_in(resp: Any) -> list[str]:
    found: list[str] = []
    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            for cite in getattr(block, "citations", None) or []:
                url = getattr(cite, "url", None)
                if url:
                    found.append(url)
        elif btype == "web_search_tool_result":
            inner = getattr(block, "content", None)
            if isinstance(inner, list):  # an error result is an object, not a list
                found += [r.url for r in inner if getattr(r, "url", None)]
    return found

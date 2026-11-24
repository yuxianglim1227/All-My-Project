"""Gemini API (free tier) as a drop-in for the `LLM` protocol, over plain REST with httpx.

Free tier: rate-limited per project and per model (limits change; see Google AI Studio), and Google may use free-tier
inputs to improve its products. The request shape follows Google's REST docs as seen via search (the docs site was not
reachable from the build environment): POST {base}/models/{model}:generateContent with the key in `x-goog-api-key`,
`system_instruction`, `contents[].parts[]` with `text` / `inline_data {mime_type, data}`, and JSON output through
`generationConfig.responseMimeType` + `responseJsonSchema`. If the API rejects the schema field we retry without it and
check the JSON ourselves, so every answer is validated locally either way.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .jsoncheck import extract_json, validate
from .llm import LLMError, Refused

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta"


class QuotaExceeded(LLMError):
    """The free-tier quota for this model is used up (HTTP 429)."""


def to_parts(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Our (Anthropic-style) content blocks -> Gemini parts."""
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict[str, Any]] = []
    for b in content:
        if b.get("type") == "text":
            parts.append({"text": b.get("text", "")})
        elif b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
            src = b["source"]
            parts.append({"inline_data": {"mime_type": src.get("media_type", "image/jpeg"), "data": src["data"]}})
    return parts or [{"text": ""}]


class GeminiLLM:
    def __init__(self, api_key: str, model: str = "gemini-flash-latest", *, base: str = BASE,
                 http: httpx.Client | None = None, timeout: float = 120.0):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set. Create a free key in Google AI Studio, or use the AI Bridge.")
        self.api_key, self.model, self.base = api_key, model, base.rstrip("/")
        self.http = http or httpx.Client(timeout=timeout)

    # -- plumbing -------------------------------------------------------------------------
    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base}/models/{self.model}:generateContent"
        try:
            r = self.http.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.HTTPError as exc:
            raise LLMError(f"Couldn't reach Gemini ({type(exc).__name__}).") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        if r.status_code == 429:
            raise QuotaExceeded("Gemini free-tier limit reached for now (it resets daily). Use the AI Bridge "
                                "(copy-paste) or try again later.")
        if r.status_code in (401, 403):
            raise LLMError(f"Gemini refused the API key ({err.get('status') or r.status_code}). Check GEMINI_API_KEY.")
        if r.status_code == 404:
            raise LLMError(f"Gemini model '{self.model}' not found for this key. Set GEMINI_MODEL to one listed by "
                           "`socialagent selfcheck --live`.")
        if r.status_code >= 400:
            raise _BadRequest(r.status_code, str(err.get("message") or r.text[:300]))
        return data

    @staticmethod
    def _text(data: dict[str, Any]) -> str:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise Refused(f"Gemini blocked this request ({block}).")
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError("Gemini returned no answer.")
        c = cands[0]
        reason = c.get("finishReason", "")
        text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts") or [] if not p.get("thought"))
        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION") and not text:
            raise Refused(f"Gemini declined to answer ({reason}).")
        if not text:
            raise LLMError(f"Gemini returned an empty answer ({reason or 'no reason given'}).")
        return text

    def _body(self, system: str, content: str | list[dict[str, Any]], max_tokens: int, **gen: Any) -> dict[str, Any]:
        return {"system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": to_parts(content)}],
                "generationConfig": {"maxOutputTokens": max_tokens, **gen}}

    # -- LLM protocol ---------------------------------------------------------------------
    def json(self, *, system: str, content: str | list[dict[str, Any]], schema: dict[str, Any],
             max_tokens: int = 8000) -> dict[str, Any]:
        body = self._body(system, content, max_tokens, responseMimeType="application/json", responseJsonSchema=schema)
        try:
            data = self._post(body)
        except _BadRequest as exc:  # older API surface or unsupported schema keyword: fall back to prompt-only schema
            log.info("Gemini rejected responseJsonSchema (%s); retrying with the schema in the prompt", exc)
            body["generationConfig"].pop("responseJsonSchema", None)
            body["system_instruction"]["parts"].append({"text": "Reply with JSON only, matching this JSON Schema:\n"
                                                                + json.dumps(schema)})
            try:
                data = self._post(body)
            except _BadRequest as exc2:
                raise LLMError(f"Gemini rejected the request: {exc2}") from None
        try:
            value = extract_json(self._text(data))
        except ValueError:
            raise LLMError("Gemini's answer was not valid JSON.") from None
        problems = validate(value, schema)
        if problems:
            raise LLMError("Gemini's answer didn't match the expected format: " + "; ".join(problems[:5]))
        return value

    def text(self, *, system: str, content: str | list[dict[str, Any]], max_tokens: int = 4000) -> str:
        try:
            return self._text(self._post(self._body(system, content, max_tokens)))
        except _BadRequest as exc:
            raise LLMError(f"Gemini rejected the request: {exc}") from None

    def research(self, *, system: str, prompt: str, max_searches: int = 8) -> tuple[str, list[str]]:
        # No live web search on this route: trends come from free feeds and your own analytics instead.
        raise LLMError("live web search isn't available with the Gemini free-tier provider")

    # -- helpers ----------------------------------------------------------------------------
    def list_models(self) -> list[str]:
        try:
            r = self.http.get(f"{self.base}/models", headers={"x-goog-api-key": self.api_key}, params={"pageSize": 100})
        except httpx.HTTPError as exc:
            raise LLMError(f"Couldn't reach Gemini ({type(exc).__name__}).") from None
        if r.status_code >= 400:
            raise LLMError(f"Gemini refused to list models (HTTP {r.status_code}); check GEMINI_API_KEY.")
        out = []
        for m in r.json().get("models") or []:
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                out.append(str(m.get("name", "")).removeprefix("models/"))
        return out


class _BadRequest(LLMError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")

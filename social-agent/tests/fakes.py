"""Test doubles: a fake LLM (agent-level) and a fake Anthropic client (SDK-level)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from socialagent.models import INSPO_SCHEMA, PLAN_SCHEMA, TRENDS_SCHEMA


class FakeLLM:
    """Implements the LLM protocol with canned answers and records every call."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.plan_response: dict[str, Any] = {
            "pillar": "Daily life", "summary": "A sunny cafe table.", "edit_brief": "", "trend_used": "",
            "risk_flags": [], "message_to_owner": "Posting this to Instagram.",
            "posts": [{"platform": "instagram", "kind": "photo", "caption": "Coffee o'clock", "hashtags": ["#coffee"],
                       "on_screen_text": "", "when": "next_slot", "highlight": "", "media_indexes": [0],
                       "trim_start": 0, "trim_end": 0, "why": "Single photo"}],
        }
        self.trends_response: dict[str, Any] = {"summary": "s", "trends": [{
            "platform": "tiktok", "name": "Photo dump", "format_description": "6 casual shots",
            "why_it_works": "authentic", "how_to_adapt": "use your week", "hook_templates": ["POV: ..."],
            "audio_note": "", "sources": ["https://example.com/a"], "stage": "rising"}]}
        self.inspo_response: dict[str, Any] = {
            "format_name": "Split-screen reaction", "hook": "Wait for it", "structure": "setup/payoff",
            "visual_style": "bright", "text_overlay_style": "big white", "pacing": "fast",
            "translation": "", "how_to_recreate": "film two angles"}
        self.research_result: tuple[str, list[str]] = ("notes about trends", ["https://example.com/a"])
        self.research_error: Exception | None = None
        self.text_result = "evergreen notes"

    def json(self, *, system, content, schema, max_tokens=8000):
        self.calls.append({"kind": "json", "system": system, "content": content, "schema": schema})
        if schema is PLAN_SCHEMA:
            return self.plan_response
        if schema is TRENDS_SCHEMA:
            return self.trends_response
        if schema is INSPO_SCHEMA:
            return self.inspo_response
        raise AssertionError("unexpected schema")

    def text(self, *, system, content, max_tokens=4000):
        self.calls.append({"kind": "text", "system": system, "content": content})
        return self.text_result

    def research(self, *, system, prompt, max_searches=8):
        self.calls.append({"kind": "research", "system": system, "content": prompt})
        if self.research_error:
            raise self.research_error
        return self.research_result


# ---------------------------------------------------------------- SDK-level fake
def text_block(text: str, citations: list[str] | None = None):
    cites = [SimpleNamespace(url=u) for u in citations or []]
    return SimpleNamespace(type="text", text=text, citations=cites)


def response(blocks, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, stop_details=stop_details)


class FakeAnthropic:
    """Mimics client.messages.create and client.beta.messages.create, replaying queued responses/exceptions."""

    def __init__(self, queue: list[Any] | None = None, beta_queue: list[Any] | None = None):
        self.queue = list(queue or [])
        self.beta_queue = list(beta_queue or [])
        self.calls: list[dict[str, Any]] = []          # standard endpoint
        self.beta_calls: list[dict[str, Any]] = []     # beta endpoint
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta_create))

    def _pop(self, q):
        item = q.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _create(self, **kw):
        self.calls.append(kw)
        return self._pop(self.queue)

    def _beta_create(self, **kw):
        self.beta_calls.append(kw)
        return self._pop(self.beta_queue)

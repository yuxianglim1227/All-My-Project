import json

import anthropic
import httpx
import pytest

from fakes import FakeAnthropic, response, text_block
from socialagent import llm as llm_mod
from socialagent.llm import ClaudeLLM, LLMError, Refused
from socialagent.textutil import compose_caption, normalize_hashtag, normalize_hashtags, truncate


# ---------------------------------------------------------------- textutil
@pytest.mark.parametrize("raw,expected", [
    ("#My Tag!", "#MyTag"), ("coffee", "#coffee"), ("##x", "#x"), ("#", ""), ("  ", ""), ("美食", "#美食"),
    ("#a-b_c", "#ab_c"),
])
def test_normalize_hashtag(raw, expected):
    assert normalize_hashtag(raw) == expected


def test_normalize_hashtags_dedupes_bans_orders_and_caps():
    out = normalize_hashtags(["#Food", "#food", "#spam", "yum", "#x1"], banned=["Spam"], first=["#brand"], limit=3)
    assert out == ["#brand", "#Food", "#yum"]          # brand first, case-insensitive dedupe, banned removed, capped


def test_compose_caption_drops_hashtags_before_text():
    tags = ["#one", "#two", "#three"]
    assert compose_caption("instagram", "Hello", tags, max_len=100) == "Hello\n\n#one #two #three"
    assert compose_caption("tiktok", "Hello", tags, max_len=100) == "Hello #one #two #three"
    assert compose_caption("tiktok", "Hello", tags, max_len=15) == "Hello #one #two"
    short = compose_caption("tiktok", "Hello", tags, max_len=11)
    assert short == "Hello #one" and len(short) <= 11
    long_cap = compose_caption("instagram", "x" * 50, ["#a"], max_len=10)
    assert len(long_cap) == 10 and long_cap.endswith("…")
    assert truncate("short", 10) == "short"


# ---------------------------------------------------------------- ClaudeLLM
def _bad_request():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError("nope", response=httpx.Response(400, request=req), body=None)


def _status_exc(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


def test_json_request_shape_defaults():
    fake = FakeAnthropic(beta_queue=[response([text_block(json.dumps({"a": 1}))])])
    llm = ClaudeLLM(client=fake)
    assert llm.json(system="S", content="hi", schema={"type": "object"}) == {"a": 1}
    kw = fake.beta_calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["betas"] == [llm_mod.FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert kw["system"] == "S" and kw["messages"] == [{"role": "user", "content": "hi"}]
    for banned in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert banned not in kw                         # rejected / unsupported on the default model


def test_without_fallbacks_uses_standard_endpoint():
    fake = FakeAnthropic(queue=[response([text_block("hello")])])
    llm = ClaudeLLM(model="claude-sonnet-5-5", effort="low", fallbacks=False, client=fake)
    assert llm.text(system="S", content="hi") == "hello"
    assert fake.beta_calls == [] and fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert fake.calls[0]["output_config"] == {"effort": "low"}


def test_fallback_rejection_retries_once_without_and_disables():
    fake = FakeAnthropic(beta_queue=[_bad_request()], queue=[response([text_block("ok")]), response([text_block("ok2")])])
    llm = ClaudeLLM(client=fake)
    assert llm.text(system="S", content="a") == "ok"
    assert llm.fallbacks is False
    assert llm.text(system="S", content="b") == "ok2"
    assert len(fake.beta_calls) == 1 and len(fake.calls) == 2   # beta not retried after being disabled


def test_refusal_truncation_and_bad_json_become_friendly_errors():
    details = type("D", (), {"category": "cyber"})()
    llm = ClaudeLLM(fallbacks=False, client=FakeAnthropic(queue=[response([], "refusal", details)]))
    with pytest.raises(Refused, match="cyber"):
        llm.text(system="S", content="x")
    llm = ClaudeLLM(fallbacks=False, client=FakeAnthropic(queue=[response([text_block("{")], "max_tokens")]))
    with pytest.raises(LLMError, match="cut off"):
        llm.json(system="S", content="x", schema={})
    llm = ClaudeLLM(fallbacks=False, client=FakeAnthropic(queue=[response([text_block("not json")]), response([text_block("[1]")])]))
    with pytest.raises(LLMError, match="malformed"):
        llm.json(system="S", content="x", schema={})
    with pytest.raises(LLMError, match="unexpected JSON shape"):
        llm.json(system="S", content="x", schema={})


@pytest.mark.parametrize("exc,needle", [
    (_status_exc(anthropic.AuthenticationError, 401), "ANTHROPIC_API_KEY"),
    (_status_exc(anthropic.RateLimitError, 429), "rate-limiting"),
    (_status_exc(anthropic.InternalServerError, 500), "API error 500"),
])
def test_sdk_errors_are_translated(exc, needle):
    llm = ClaudeLLM(fallbacks=False, client=FakeAnthropic(queue=[exc]))
    with pytest.raises(LLMError, match=needle):
        llm.text(system="S", content="x")


def test_research_resumes_pause_turn_and_collects_sources():
    result_block = type("R", (), {"type": "web_search_tool_result",
                                  "content": [type("X", (), {"url": "https://b.example/2"})()]})()
    first = response([result_block, text_block("part one")], "pause_turn")
    second = response([text_block("final notes", ["https://a.example/1", "https://b.example/2"])], "end_turn")
    fake = FakeAnthropic(queue=[first, second])
    llm = ClaudeLLM(fallbacks=False, client=fake)
    text, urls = llm.research(system="S", prompt="trends?", max_searches=3)
    assert text == "final notes"
    assert urls == ["https://b.example/2", "https://a.example/1"]   # de-duplicated, order kept
    tools = fake.calls[0]["tools"]
    assert tools == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    # the paused assistant turn is replayed so the server can continue it
    assert fake.calls[1]["messages"][-1] == {"role": "assistant", "content": first.content}


def test_research_error_result_object_does_not_crash():
    err = type("E", (), {"type": "web_search_tool_result", "content": type("Err", (), {"error_code": "unavailable"})()})()
    llm = ClaudeLLM(fallbacks=False, client=FakeAnthropic(queue=[response([err, text_block("no web")])]))
    assert llm.research(system="S", prompt="p") == ("no web", [])

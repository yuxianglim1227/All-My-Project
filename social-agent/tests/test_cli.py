import json
import os
import shutil
import subprocess
import sys

import httpx
import pytest

from conftest import needs_ffmpeg
from fakes import FakeLLM
from helpers import ROOT, one_post, set_plan
from socialagent.cli import main
from socialagent.config import load_dotenv
from socialagent.store import Store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    brand = tmp_path / "brand.yaml"
    shutil.copyfile(ROOT / "config" / "brand.yaml", brand)
    for k in list(os.environ):
        if k.startswith(("IG_", "TIKTOK_", "TELEGRAM_", "OWNER_", "DRY_RUN", "API_TOKEN", "PUBLIC_BASE", "REQUIRE_", "ANTHROPIC")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BRAND_FILE", str(brand))
    monkeypatch.setenv("TIMEZONE", "UTC")
    return tmp_path


def run(argv, llm=None, http=None):
    return main(["--env-file", "/nonexistent.env", *argv], llm=llm or FakeLLM(), http=http)


# ---------------------------------------------------------------- .env loader
def test_load_dotenv_parsing_and_precedence(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# comment\nA=1   # trailing\nB="x # y"\nexport C=\'z\'\nE=tok#en\nF=\nBADLINE\nA2 = spaced \n'
                 'BLANK=      # only a comment (regression: used to become the value)\nBLANK2=# glued comment\n')
    environ = {"A": "already-set"}
    assert load_dotenv(f, environ) == 7
    assert environ == {"A": "already-set", "B": "x # y", "C": "z", "E": "tok#en", "F": "", "A2": "spaced",
                       "BLANK": "", "BLANK2": ""}
    assert load_dotenv(tmp_path / "missing.env", {}) == 0


# ---------------------------------------------------------------- commands
@needs_ffmpeg
def test_submit_queue_cancel_flow_via_cli(env, media_dir, capsys):
    llm = FakeLLM()
    set_plan(llm, one_post(when="next_slot"))
    original = env / "mine.jpg"
    shutil.copyfile(media_dir / "photo.jpg", original)
    before = original.read_bytes()

    assert run(["submit", str(original), "-i", "nice coffee"], llm=llm) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Instagram photo" in out and "Cancel #1" in out
    assert original.read_bytes() == before                                  # originals are never modified

    assert run(["queue"]) == 0
    queue_out = capsys.readouterr().out
    assert "instagram photo" in queue_out
    pid = queue_out.split("[")[-1].split("]")[0]
    assert run(["cancel", pid]) == 0
    assert "Cancelled" in capsys.readouterr().out
    assert run(["cancel", pid]) == 1                                         # second time: nothing to cancel
    assert run(["queue"]) == 0 and "Nothing queued" in capsys.readouterr().out


def test_submit_missing_file_is_a_clean_error(env, capsys):
    assert run(["submit", "/no/such/file.jpg"]) == 1
    assert "not found" in capsys.readouterr().err


def test_tick_trends_inspo_highlights_review(env, tmp_path, capsys):
    assert run(["tick"]) == 0 and "published" in capsys.readouterr().out
    assert run(["trends"]) == 0 and "No trends yet" in capsys.readouterr().out
    assert run(["trends", "--refresh"]) == 0 and "Photo dump" in capsys.readouterr().out
    assert run(["inspo", "link", "https://v.douyin.com/abc/", "-n", "nice"]) == 0
    assert "screenshot" in capsys.readouterr().out
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"Likes": [{"Link": "https://www.tiktok.com/@a/video/1"}]}))
    assert run(["inspo", "export", str(export)]) == 0 and "'saved': 1" in capsys.readouterr().out
    assert run(["highlights"]) == 0 and "No Stories waiting" in capsys.readouterr().out
    assert run(["highlights", "--done"]) == 0 and "Marked 0" in capsys.readouterr().out
    assert run(["review"]) == 0 and "Weekly feed review" in capsys.readouterr().out


def test_llm_errors_become_exit_code_1_not_a_traceback(env, capsys):
    from socialagent.llm import LLMError
    llm = FakeLLM()
    llm.research_error = None
    llm.json = lambda **kw: (_ for _ in ()).throw(LLMError("ANTHROPIC_API_KEY is missing or invalid."))
    assert run(["trends", "--refresh"], llm=llm) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_bad_configuration_exits_2(env, monkeypatch, capsys):
    monkeypatch.setenv("TIMEZONE", "Mars/Base")
    assert run(["queue"]) == 2 and "Configuration error" in capsys.readouterr().err


# ---------------------------------------------------------------- selfcheck
def test_selfcheck_in_dry_run_passes_with_warnings(env, capsys):
    assert run(["selfcheck"]) == 0
    out = capsys.readouterr().out
    assert "DRY_RUN is ON" in out and "ffmpeg" in out and "ANTHROPIC_API_KEY not set" in out and "brand profile loaded" in out


def test_selfcheck_live_reports_platform_failures_with_exit_1(env, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("IG_USER_ID", "U1")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "ttok")
    monkeypatch.setenv("TIKTOK_MODE", "direct")

    def handler(request: httpx.Request):
        if "instagram" in request.url.host or "graph" in request.url.host:
            return httpx.Response(401, json={"error": {"message": "bad token", "code": 190}})
        return httpx.Response(200, json={"data": {"creator_username": "me", "privacy_level_options": ["SELF_ONLY"]},
                                         "error": {"code": "ok"}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert run(["selfcheck"], http=http) == 1
    out = capsys.readouterr().out
    assert "DRY_RUN is off" in out and "instagram: FAILED" in out and "access token" in out
    assert "connected as @me" in out and "not passed TikTok's audit" in out


def test_selfcheck_live_claude_ping(env, capsys):
    llm = FakeLLM()
    llm.text_result = "OK"
    assert run(["selfcheck", "--claude"], llm=llm) == 0
    assert "Claude replied: 'OK'" in capsys.readouterr().out


# ---------------------------------------------------------------- auth helpers
def test_auth_tiktok_url_and_exchange_store_tokens(env, monkeypatch, capsys):
    assert run(["auth", "tiktok-url", "--redirect", "https://x.example/cb"]) == 1       # no client key yet
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "KEY")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "SECRET")
    capsys.readouterr()
    assert run(["auth", "tiktok-url", "--redirect", "https://x.example/cb"]) == 0
    assert "client_key=KEY" in capsys.readouterr().out

    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "access_token": "ACC1", "refresh_token": "REF1", "scope": "video.publish", "expires_in": 86400})))
    assert run(["auth", "tiktok-exchange", "--code", "CODE", "--redirect", "https://x.example/cb"], http=http) == 0
    assert "TikTok tokens saved" in capsys.readouterr().out
    store = Store(env / "data" / "agent.db")
    assert (store.kv_get("tt_access_token"), store.kv_get("tt_refresh_token")) == ("ACC1", "REF1")


def test_auth_ig_refresh_updates_stored_token(env, monkeypatch, capsys):
    monkeypatch.setenv("IG_ACCESS_TOKEN", "OLD")
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000})))
    assert run(["auth", "ig-refresh"], http=http) == 0
    assert "refreshed (valid ~60 more days)" in capsys.readouterr().out
    assert Store(env / "data" / "agent.db").kv_get("ig_access_token") == "NEW"


def test_python_dash_m_entrypoint_works():
    r = subprocess.run([sys.executable, "-m", "socialagent", "--help"], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert r.returncode == 0 and "Creative Instagram + TikTok manager" in r.stdout and "selfcheck" in r.stdout

"""The setup wizard: live key checks, owner-id discovery, file writers, and "no secret is ever shown"."""

import os
import shutil
import stat

import anthropic
import httpx
import pytest
import yaml

from helpers import ROOT
from socialagent import setup as su
from socialagent.cli import main
from socialagent.config import BrandProfile, Settings, load_dotenv

TOKEN = "123456789:TEST_not_a_real_token"
CLAUDE_KEY = "test-claude-key-not-real-0123456789"
OWNER = 4242


# ---------------------------------------------------------------- fakes
class FakeTelegram:
    """Just enough of the Bot API: getMe, getUpdates (with offset acknowledgement), sendMessage."""

    def __init__(self, token=TOKEN, updates=None, webhook_conflict=False, down=False):
        self.token, self.pending, self.sent, self.calls = token, list(updates or []), [], []
        self.acked = 0
        self.webhook_conflict, self.down = webhook_conflict, down

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("boom")
        _, bot, method = request.url.path.split("/")[-3:] if request.url.path.count("/") >= 2 else ("", "", "")
        assert bot == f"bot{self.token}" or bot.startswith("bot"), request.url
        self.calls.append((method, dict(request.url.params)))
        if bot != f"bot{self.token}":
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True, "username": "my_agent_bot"}})
        if method == "getUpdates":
            if self.webhook_conflict:
                return httpx.Response(409, json={"ok": False, "description": "Conflict"})
            off = request.url.params.get("offset")
            if off is not None:
                self.pending = [u for u in self.pending if u["update_id"] >= int(off)]
                self.acked = max(self.acked, int(off))
            return httpx.Response(200, json={"ok": True, "result": list(self.pending)})
        if method == "sendMessage":
            self.sent.append(dict(request.url.params))
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def msg(update_id, user_id=OWNER, chat="private", is_bot=False, first="Alex", username="alex"):
    return {"update_id": update_id, "message": {"chat": {"type": chat}, "text": "/start",
                                                "from": {"id": user_id, "is_bot": is_bot, "first_name": first, "username": username}}}


class Script:
    """Scripted terminal: answers are matched by a substring of the prompt (first rule wins), secrets come from a queue."""

    def __init__(self, rules=None, secrets=None, interrupt_on=None):
        self.rules, self.queue, self.interrupt_on = list(rules or []), list(secrets or []), interrupt_on
        self.out: list[str] = []      # everything printed or asked
        self.asked: list[str] = []

    def ask(self, prompt, default=""):
        self.out.append(prompt)
        self.asked.append(prompt)
        if len(self.asked) > 300:                    # a wizard stuck re-asking the same question must fail, not spin
            raise AssertionError(f"wizard is looping on: {prompt}")
        if self.interrupt_on and self.interrupt_on in prompt:
            raise EOFError
        for key, ans in self.rules:
            if key in prompt:
                if isinstance(ans, list):          # successive answers to the same question
                    ans = ans.pop(0) if len(ans) > 1 else ans[0]
                return ans if ans != "" else default
        return default

    def secret(self, prompt):
        self.out.append(prompt)
        if not self.queue:
            raise AssertionError(f"unexpected secret prompt: {prompt}")
        return self.queue.pop(0)

    def say(self, text):
        self.out.append(text)

    @property
    def io(self):
        return su.IO(ask=self.ask, secret=self.secret, say=self.say)

    @property
    def transcript(self):
        return "\n".join(self.out)


def claude_ok(key):
    return su.OK, "Claude key works."


@pytest.fixture()
def paths(tmp_path):
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    shutil.copyfile(ROOT / ".env.example", example)
    brand = tmp_path / "brand.yaml"
    shutil.copyfile(ROOT / "config" / "brand.yaml", brand)
    return env, example, brand


def run(script, paths, tg, check_claude=claude_ok, **kw):
    env, example, brand = paths
    kw.setdefault("port_busy", lambda port: False)          # real socket probing is tested separately
    return su.run_setup(script.io, env_path=env, brand_path=brand, example_path=example, http=tg.client(),
                        check_claude=check_claude, sleep=lambda s: None, now=ticker(), **kw)


def ticker():
    t = {"v": 0.0}

    def now():
        t["v"] += 20.0     # every look at the clock "takes" 20 s, so a silent wait ends after ~9 polls
        return t["v"]
    return now


def happy_script(**over):
    rules = [("time zone", "Asia/Kuala_Lumpur"), ("brand name", "Lily's Kitchen"), ("about (one line)", "Home cooking in KL"),
             ("voice", "warm, witty, never salesy"), ("caption tone", "funny"), ("language", "zh"), ("look", "vivid")]
    return Script(rules=rules + over.pop("extra", []), secrets=over.pop("secrets", [CLAUDE_KEY, TOKEN]), **over)


# ---------------------------------------------------------------- upsert_env
def test_upsert_env_sets_values_and_keeps_comments_and_unrelated_lines():
    text = "# keep me\nDRY_RUN=true   # safety\nTOKEN=   # from botfather\nOTHER=1\n"
    out = su.upsert_env(text, {"TOKEN": "abc", "NEW": "x y"})
    assert out == '# keep me\nDRY_RUN=true   # safety\nTOKEN=abc   # from botfather\nOTHER=1\nNEW="x y"\n'


def test_upsert_env_replaces_only_the_named_key_not_prefix_lookalikes():
    out = su.upsert_env("API_TOKEN_OLD=1\nAPI_TOKEN=\n", {"API_TOKEN": "zzz"})
    assert out == "API_TOKEN_OLD=1\nAPI_TOKEN=zzz\n"


def test_upsert_env_normalises_crlf_and_adds_trailing_newline():
    assert su.upsert_env("A=1\r\nB=2", {"B": "3"}) == "A=1\nB=3\n"
    assert su.upsert_env("", {"A": "1"}) == "A=1\n"


def test_upsert_env_round_trips_through_the_real_loader_including_hash_and_spaces(tmp_path):
    out = su.upsert_env("X=\nY=\n", {"X": "has # hash", "Y": "tok#en"})
    env: dict[str, str] = {}
    p = tmp_path / ".env"
    p.write_text(out, encoding="utf-8")
    load_dotenv(p, env)
    assert env == {"X": "has # hash", "Y": "tok#en"}


# ---------------------------------------------------------------- apply_brand
def test_apply_brand_fills_answers_keeps_comments_and_still_parses():
    text = (ROOT / "config" / "brand.yaml").read_text(encoding="utf-8")
    out = su.apply_brand(text, {"account_name": 'Lily "Kitchen" 厨房', "niche": "Home cooking", "voice": "warm,  witty",
                                "tone": "funny", "language": "bi", "look": "bright", "region": "Malaysia"})
    assert "# Your brand brain." in out and "#   tone:     descriptive |" in out and "# photo/video polish" in out
    b = BrandProfile.from_dict(yaml.safe_load(out))
    assert b.account_name == 'Lily "Kitchen" 厨房' and b.niche == "Home cooking" and b.region == "Malaysia"
    assert b.voice == "warm, witty"
    assert (b.default_tone, b.default_language, b.default_look) == ("funny", "bi", "bright")
    assert b.audience == BrandProfile.from_dict(yaml.safe_load(text)).audience      # untouched when not answered
    assert len(b.pillars) == 4 and len(b.highlights) == 4                           # the rest of the file survives


def test_apply_brand_returns_the_original_if_the_result_would_not_parse():
    broken = "account_name: \"x\"\n  bad: [unclosed\n"
    assert su.apply_brand(broken, {"account_name": "New"}) == broken


def test_apply_brand_blank_answers_change_nothing():
    text = (ROOT / "config" / "brand.yaml").read_text(encoding="utf-8")
    assert su.apply_brand(text, {"account_name": "", "voice": ""}) == text


# ---------------------------------------------------------------- Telegram calls
def test_get_me_ok_and_wrong_token_and_network_errors_never_leak_the_token():
    tg = FakeTelegram()
    assert su.telegram_get_me(tg.client(), TOKEN)["username"] == "my_agent_bot"
    with pytest.raises(su.TokenRejected) as bad:
        su.telegram_get_me(tg.client(), "999:wrong")
    assert "999:wrong" not in str(bad.value) and "BotFather" in str(bad.value)
    with pytest.raises(su.SetupError) as down:
        su.telegram_get_me(FakeTelegram(down=True).client(), TOKEN)
    assert TOKEN not in str(down.value) and "reach Telegram" in str(down.value)
    assert down.value.__cause__ is None and down.value.__suppress_context__


def test_webhook_conflict_is_explained():
    with pytest.raises(su.SetupError, match="Another program"):
        su.telegram_wait_for_owner(FakeTelegram(webhook_conflict=True).client(), TOKEN, Script().io, now=ticker(), sleep=lambda s: None)


def test_wait_for_owner_ignores_groups_and_bots_and_skips_a_stranger_the_owner_rejects():
    tg = FakeTelegram(updates=[msg(1, 7, chat="group"), msg(2, 8, is_bot=True), msg(3, 9, first="Stranger"), msg(4, OWNER)])
    s = Script(rules=[("Stranger", "n")])
    found = su.telegram_wait_for_owner(tg.client(), TOKEN, s.io, now=ticker(), sleep=lambda s_: None)
    assert found == {"id": OWNER, "first_name": "Alex", "username": "alex"}
    assert not any("group" in p or "id 7" in p or "id 8" in p for p in s.asked)     # never even offered
    assert any("Stranger" in p for p in s.asked)
    assert tg.acked == 5 and tg.pending == []                                        # acknowledged: the agent will not replay them


def test_wait_for_owner_times_out_when_nobody_writes():
    tg = FakeTelegram()
    assert su.telegram_wait_for_owner(tg.client(), TOKEN, Script().io, wait_seconds=60, now=ticker(), sleep=lambda s: None) is None
    assert 1 <= len(tg.calls) <= 4                                                   # polled a bounded number of times


# ---------------------------------------------------------------- Claude key check
def _fake_anthropic(monkeypatch, exc):
    class Models:
        def list(self):
            if exc:
                raise exc

    class Client:
        def __init__(self, **kw):
            self.models = Models()
    monkeypatch.setattr(anthropic, "Anthropic", Client)


def _status_exc(cls, status):
    req = httpx.Request("GET", "https://api.anthropic.com/v1/models")
    return cls("x", response=httpx.Response(status, request=req), body=None)


def test_validate_claude_key_verdicts(monkeypatch):
    _fake_anthropic(monkeypatch, None)
    assert su.validate_claude_key("k")[0] == su.OK
    _fake_anthropic(monkeypatch, _status_exc(anthropic.AuthenticationError, 401))
    assert su.validate_claude_key("k")[0] == su.REJECTED
    _fake_anthropic(monkeypatch, _status_exc(anthropic.PermissionDeniedError, 403))
    assert su.validate_claude_key("k")[0] == su.OK                                   # restricted keys are fine
    _fake_anthropic(monkeypatch, anthropic.APIConnectionError(request=httpx.Request("GET", "https://x")))
    assert su.validate_claude_key("k")[0] == su.UNVERIFIED
    _fake_anthropic(monkeypatch, RuntimeError("weird"))
    verdict, message = su.validate_claude_key("k")
    assert verdict == su.UNVERIFIED and "RuntimeError" in message


# ---------------------------------------------------------------- the wizard end to end
def test_happy_path_writes_env_and_brand_and_never_shows_a_secret(paths):
    tg = FakeTelegram(updates=[msg(10)])
    s = happy_script()
    assert run(s, paths, tg) == 0
    env_path, _, brand_path = paths

    values: dict[str, str] = {}
    load_dotenv(env_path, values)
    assert values["ANTHROPIC_API_KEY"] == CLAUDE_KEY and values["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert values["OWNER_TELEGRAM_ID"] == str(OWNER) and values["TIMEZONE"] == "Asia/Kuala_Lumpur"
    assert values["DRY_RUN"] == "true" and len(values["API_TOKEN"]) >= 32
    assert values["PUBLIC_BASE_URL"] == "https://agent.example.com"                  # blank answer: template value kept
    assert values["EDIT_LEVEL"] == "enhance" and values["COLLECT_WINDOW_SECONDS"] == "3"   # the rest of the template survives
    text = env_path.read_text(encoding="utf-8")
    assert "# --- Safety" in text and "# from @BotFather" in text                      # comments survive
    if os.name == "posix":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert not env_path.with_name(".env.tmp").exists()

    b = BrandProfile.load(brand_path)
    assert (b.account_name, b.niche, b.default_tone, b.default_language, b.default_look) == \
        ("Lily's Kitchen", "Home cooking in KL", "funny", "zh", "vivid")

    # a confirmation message reached the owner's Telegram
    assert tg.sent and tg.sent[0]["chat_id"] == str(OWNER)
    # the transcript (everything printed or asked) never contains a secret in full
    assert CLAUDE_KEY not in s.transcript and TOKEN not in s.transcript and values["API_TOKEN"] not in s.transcript
    assert "DRY_RUN is ON" in s.transcript and "selfcheck --live" in s.transcript


def test_the_file_the_wizard_writes_is_accepted_by_settings_and_the_brand_loader(paths):
    run(happy_script(), paths, FakeTelegram(updates=[msg(10)]))
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    settings = Settings.from_env(values)
    assert settings.dry_run is True and settings.owner_telegram_id == OWNER and settings.telegram_token == TOKEN
    assert settings.timezone == "Asia/Kuala_Lumpur"


def test_wrong_bot_token_is_rejected_then_the_right_one_accepted(paths):
    tg = FakeTelegram(updates=[msg(10)])
    s = happy_script(secrets=[CLAUDE_KEY, "999:wrongwrongwrong", "not a token", TOKEN])
    assert run(s, paths, tg) == 0
    assert "Telegram says that token is wrong" in s.transcript and "doesn't look like a bot token" in s.transcript
    assert "999:wrongwrongwrong" not in s.transcript
    assert ("getMe", {}) in tg.calls


def test_three_bad_bot_tokens_stop_the_wizard_and_write_nothing(paths):
    s = happy_script(secrets=[CLAUDE_KEY, "1:bad", "2:bad", "3:bad"])
    assert run(s, paths, FakeTelegram()) == 1
    assert not paths[0].exists() and "Couldn't connect to the bot" in s.transcript


def test_rejected_claude_key_is_asked_again_and_three_failures_abort(paths):
    verdicts = {"bad1": su.REJECTED, "bad2": su.REJECTED, CLAUDE_KEY: su.OK}
    check = lambda k: (verdicts[k], "Claude rejected that key." if verdicts[k] == su.REJECTED else "Claude key works.")
    s = happy_script(secrets=["bad1", "bad2", CLAUDE_KEY, TOKEN])
    assert run(s, paths, FakeTelegram(updates=[msg(10)]), check_claude=check) == 0
    assert s.transcript.count("Claude rejected that key.") == 2

    paths[0].unlink()
    s = happy_script(secrets=["bad1", "bad2", "bad1"])
    assert run(s, paths, FakeTelegram(), check_claude=check) == 1
    assert not paths[0].exists() and "Couldn't get a working Claude key" in s.transcript


def test_unverifiable_claude_key_can_be_saved_anyway_when_the_owner_says_so(paths):
    check = lambda k: (su.UNVERIFIED, "Couldn't reach api.anthropic.com to check it.")
    s = happy_script(extra=[("Save it anyway", "y")])
    assert run(s, paths, FakeTelegram(updates=[msg(10)]), check_claude=check) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["ANTHROPIC_API_KEY"] == CLAUDE_KEY
    # ...and declining means asking again instead of saving
    paths[0].unlink()
    s = happy_script(secrets=[CLAUDE_KEY, CLAUDE_KEY, CLAUDE_KEY], extra=[("Save it anyway", "n")])
    assert run(s, paths, FakeTelegram(), check_claude=check) == 1 and not paths[0].exists()


def test_empty_secret_is_not_sent_to_any_service(paths):
    calls = []
    check = lambda k: calls.append(k) or (su.OK, "ok")
    s = happy_script(secrets=["", CLAUDE_KEY, TOKEN])
    assert run(s, paths, FakeTelegram(updates=[msg(10)]), check_claude=check) == 0
    assert calls == [CLAUDE_KEY] and "Nothing was entered" in s.transcript


def test_owner_is_typed_manually_when_no_message_arrives(paths):
    s = happy_script(extra=[("Telegram user id", ["abc", "5555"])])
    assert run(s, paths, FakeTelegram()) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["OWNER_TELEGRAM_ID"] == "5555" and "Digits only" in s.transcript and "didn't see a message" in s.transcript


def test_invalid_timezone_is_asked_again(paths):
    s = Script(rules=[("time zone", ["Mars/Olympus", "Europe/London"]), ("caption tone", "funny"), ("language", "zh"), ("look", "vivid")],
               secrets=[CLAUDE_KEY, TOKEN])
    assert run(s, paths, FakeTelegram(updates=[msg(10)])) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["TIMEZONE"] == "Europe/London" and "Unknown TIMEZONE" in s.transcript


def test_cancelling_midway_saves_nothing(paths):
    s = happy_script(interrupt_on="caption tone")
    assert run(s, paths, FakeTelegram(updates=[msg(10)])) == 130
    assert not paths[0].exists() and "Nothing was saved" in s.transcript
    assert "Lily's Kitchen" not in paths[2].read_text(encoding="utf-8")


def test_rerun_keeps_saved_secrets_and_owner_and_preserves_the_dry_run_choice(paths):
    run(happy_script(), paths, FakeTelegram(updates=[msg(10)]))
    text = paths[0].read_text(encoding="utf-8").replace("DRY_RUN=true", "DRY_RUN=false")
    paths[0].write_text(text, encoding="utf-8")
    before: dict[str, str] = {}
    load_dotenv(paths[0], before)

    s = Script(rules=[("time zone", "")])        # accept every default; no secret is typed (an unexpected prompt raises)
    tg = FakeTelegram()
    assert run(s, paths, tg) == 0
    after: dict[str, str] = {}
    load_dotenv(paths[0], after)
    assert after["ANTHROPIC_API_KEY"] == CLAUDE_KEY and after["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert after["OWNER_TELEGRAM_ID"] == str(OWNER) and after["API_TOKEN"] == before["API_TOKEN"]
    assert after["DRY_RUN"] == "false" and "DRY_RUN is OFF" in s.transcript         # not silently flipped, and the owner is warned
    assert not any(m == "getUpdates" for m, _ in tg.calls)                          # no need to wait for a message again
    assert CLAUDE_KEY not in s.transcript and TOKEN not in s.transcript


def test_changing_the_bot_token_re_detects_the_owner(paths):
    run(happy_script(), paths, FakeTelegram(updates=[msg(10)]))
    new_token = "555555555:NEW_not_a_real_token"
    s = Script(rules=[("Keep your saved Claude key", "y"), ("Keep your saved bot token", "n")], secrets=[new_token])
    tg = FakeTelegram(token=new_token, updates=[msg(20, user_id=777, first="Sam", username="sam")])
    assert run(s, paths, tg) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["TELEGRAM_BOT_TOKEN"] == new_token and values["OWNER_TELEGRAM_ID"] == "777"


def test_a_unreadable_env_file_is_reported_not_crashed(paths):
    paths[0].write_bytes(b"A=\xff\xfe\xfd broken\n")
    s = happy_script()
    assert run(s, paths, FakeTelegram()) == 2 and "UTF-8" in s.transcript


def test_works_without_a_template_or_brand_file(tmp_path):
    env = tmp_path / "sub" / ".env"
    s = happy_script()
    rc = su.run_setup(s.io, env_path=env, brand_path=tmp_path / "missing.yaml", example_path=None,
                      http=FakeTelegram(updates=[msg(10)]).client(), check_claude=claude_ok, sleep=lambda x: None, now=ticker())
    assert rc == 0 and not (tmp_path / "missing.yaml").exists()
    values: dict[str, str] = {}
    load_dotenv(env, values)
    assert values["OWNER_TELEGRAM_ID"] == str(OWNER) and values["DRY_RUN"] == "true"


def test_url_is_saved_without_a_trailing_slash(paths):
    s = happy_script(extra=[("Public HTTPS", "https://agent.mydomain.com/")])
    run(s, paths, FakeTelegram(updates=[msg(10)]))
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["PUBLIC_BASE_URL"] == "https://agent.mydomain.com"


# ---------------------------------------------------------------- CLI wiring
def test_setup_subcommand_is_dispatched_before_the_env_is_loaded(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(su, "main_setup", lambda env_file: seen.setdefault("env", env_file) and 0)
    monkeypatch.delenv("OWNER_TELEGRAM_ID", raising=False)
    env = tmp_path / "x.env"
    env.write_text("OWNER_TELEGRAM_ID=999\n")
    assert main(["--env-file", str(env), "setup"]) == 0
    assert seen["env"] == str(env) and "OWNER_TELEGRAM_ID" not in os.environ          # not pushed into the process


def test_selfcheck_live_also_checks_the_telegram_bot(tmp_path, monkeypatch, capsys):
    from fakes import FakeLLM
    shutil.copyfile(ROOT / "config" / "brand.yaml", tmp_path / "brand.yaml")
    for k in list(os.environ):
        if k.startswith(("IG_", "TIKTOK_", "TELEGRAM_", "OWNER_", "DRY_RUN", "API_TOKEN", "PUBLIC_BASE", "ANTHROPIC")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BRAND_FILE", str(tmp_path / "brand.yaml"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", str(OWNER))
    llm = FakeLLM()
    llm.text_result = "OK"
    assert main(["selfcheck", "--live"], llm=llm, http=FakeTelegram().client()) == 0
    assert "Telegram bot reachable: @my_agent_bot" in capsys.readouterr().out

    bad = FakeTelegram(token="999:other")
    assert main(["selfcheck", "--live"], llm=llm, http=bad.client()) == 1
    out = capsys.readouterr().out
    assert "Telegram: Telegram says that token is wrong" in out and TOKEN not in out


# ---------------------------------------------------------------- file permissions on the secrets file
def test_write_private_in_place_keeps_the_same_file_so_a_windows_acl_survives(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OLD=1\n")
    inode = f.stat().st_ino
    su._write_private(f, "NEW=2\n", in_place=True)
    assert f.read_text() == "NEW=2\n" and f.stat().st_ino == inode        # same file object => same security descriptor
    assert not f.with_name(".env.tmp").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_write_private_creates_owner_only_files_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0)
    try:
        f = tmp_path / "deep" / ".env"
        su._write_private(f, "A=1\n")
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        su._write_private(f, "A=2\n")                                       # rewriting an existing file keeps it private
        assert stat.S_IMODE(f.stat().st_mode) == 0o600 and f.read_text() == "A=2\n"
    finally:
        os.umask(old)


# ---------------------------------------------------------------- local port (other apps share the server)
def _listener():
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_port_in_use_sees_a_real_listener_and_a_free_port():
    srv, port = _listener()
    try:
        assert su.port_in_use(port) is True
    finally:
        srv.close()
    assert su.port_in_use(_free_port()) is False


def test_pick_free_port_skips_busy_and_avoided_ports_and_gives_up_eventually():
    busy = {8787, 8788, 8790}
    assert su.pick_free_port(8787, avoid=[8789], in_use=lambda p: p in busy) == 8791
    assert su.pick_free_port(9000, in_use=lambda p: False) == 9000
    with pytest.raises(su.SetupError, match="free port"):
        su.pick_free_port(9000, attempts=5, in_use=lambda p: True)


def test_wizard_picks_an_uncommon_free_port_and_binds_to_localhost_only(paths):
    s = happy_script()
    assert run(s, paths, FakeTelegram(updates=[msg(10)])) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["HTTP_PORT"] == "8787" and values["HTTP_PORT"] != "8080"          # not the template/common default
    assert values["HTTP_HOST"] == "127.0.0.1"                                       # the template's 0.0.0.0 is replaced
    assert "listens on 127.0.0.1:8787" in s.transcript and "http://localhost:8787" in s.transcript


def test_wizard_suggests_the_next_free_port_when_the_default_is_taken(paths):
    s = happy_script()
    assert run(s, paths, FakeTelegram(updates=[msg(10)]), port_busy=lambda p: p in (8787, 8788)) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["HTTP_PORT"] == "8789"


def test_wizard_rejects_a_busy_or_invalid_port_the_owner_types(paths):
    s = happy_script(extra=[("Local port", ["80", "abc", "9000", "9100"])])
    assert run(s, paths, FakeTelegram(updates=[msg(10)]), port_busy=lambda p: p == 9000) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["HTTP_PORT"] == "9100"
    assert s.transcript.count("Use a number between 1024 and 65535.") == 2          # "80" (privileged) and "abc"
    assert "Port 9000 is already in use" in s.transcript


def test_rerun_keeps_the_chosen_port_and_a_custom_host_but_replaces_an_old_open_host(paths):
    run(happy_script(), paths, FakeTelegram(updates=[msg(10)]))
    text = paths[0].read_text(encoding="utf-8").replace("HTTP_PORT=8787", "HTTP_PORT=9300")
    paths[0].write_text(text, encoding="utf-8")
    s = Script(rules=[("time zone", "")])
    assert run(s, paths, FakeTelegram()) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["HTTP_PORT"] == "9300" and values["HTTP_HOST"] == "127.0.0.1"

    paths[0].write_text(text.replace("HTTP_HOST=127.0.0.1", "HTTP_HOST=10.0.0.5"), encoding="utf-8")     # deliberate choice survives
    assert run(Script(rules=[("time zone", "")]), paths, FakeTelegram()) == 0
    load_dotenv(paths[0], (again := {}))
    assert again["HTTP_HOST"] == "10.0.0.5"

    paths[0].write_text(text.replace("HTTP_HOST=127.0.0.1", "HTTP_HOST=0.0.0.0"), encoding="utf-8")      # the old wide-open default does not
    assert run(Script(rules=[("time zone", "")]), paths, FakeTelegram()) == 0
    load_dotenv(paths[0], (third := {}))
    assert third["HTTP_HOST"] == "127.0.0.1"


def test_a_saved_port_that_is_now_taken_is_replaced_by_a_free_one(paths):
    run(happy_script(), paths, FakeTelegram(updates=[msg(10)]))
    paths[0].write_text(paths[0].read_text(encoding="utf-8").replace("HTTP_PORT=8787", "HTTP_PORT=9300"), encoding="utf-8")
    assert run(Script(rules=[("time zone", "")]), paths, FakeTelegram(), port_busy=lambda p: p == 9300) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["HTTP_PORT"] == "8787"


def test_the_template_default_8080_counts_as_never_chosen(paths):
    paths[0].write_text(paths[1].read_text(encoding="utf-8"), encoding="utf-8")
    s = happy_script(secrets=[CLAUDE_KEY, TOKEN])
    assert run(s, paths, FakeTelegram(updates=[msg(10)])) == 0
    values: dict[str, str] = {}
    load_dotenv(paths[0], values)
    assert values["HTTP_PORT"] == "8787"

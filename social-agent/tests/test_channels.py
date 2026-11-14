import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from conftest import needs_ffmpeg
from helpers import make_app
from socialagent.cards import Button, Card
from socialagent.channels.telegram_bot import (Callback, Collector, TelegramFrontend, TelegramNotifier, build_telegram,
                                               callback_data, chunk_text, format_inspo, parse_callback, render_card)

# ---------------------------------------------------------------- pure helpers
def test_chunk_text_respects_limit_and_keeps_all_content():
    text = "\n".join(f"line {i} " + "x" * 30 for i in range(200))
    pieces = chunk_text(text, limit=500)
    assert all(len(p) <= 500 for p in pieces) and len(pieces) > 1
    assert "\n".join(pieces) == text
    assert chunk_text("") == [""] and chunk_text("short") == ["short"]
    long_line = "y" * 1250
    assert [len(p) for p in chunk_text(long_line, limit=500)] == [500, 500, 250]


@pytest.mark.parametrize("data,expected", [
    ("d:abc123def0:tone:funny", Callback("draft", "abc123def0", "tone:funny")),
    ("d:abc123def0:approve!", Callback("draft", "abc123def0", "approve!")),
    ("d:abc123def0:show", Callback("draft", "abc123def0", "show")),
    ("s:-:method:instagram:manual", Callback("settings", "-", "method:instagram:manual")),
    ("p:abc123def0:cancel:0123456789", Callback("post", "0123456789", "cancel")),
    ("cancel:abc123def0", Callback("post", "abc123def0", "cancel")),          # notification buttons
    ("posted:abc123def0", Callback("post", "abc123def0", "posted")),
    ("retry:abcdef", Callback("post", "abcdef", "retry")),
    ("approve:abc123def0", None),                                              # posts are no longer approved: drafts are
    ("delete:abcdef", None), ("cancel:xyz", None), ("cancel:", None), ("", None), (None, None),
    ("cancel:abc123def0;rm -rf", None), ("d:abc123def0:TONE", None), ("d:ZZZ:tone:funny", None), ("s:x:tone:funny", None),
])
def test_parse_callback(data, expected):
    assert parse_callback(data) == expected


def test_callback_data_roundtrip_and_64_byte_limit():
    card = Card("draft", "abc123def0", "t")
    for action in ("tone:informative", "lang:zh-hant", "tgt:t3", "approve!", "rewrite"):
        data = callback_data(card, action)
        assert parse_callback(data) == Callback("draft", "abc123def0", action) and len(data.encode()) <= 64
    assert parse_callback(callback_data(Card("settings", "-", ""), "method:instagram:manual")).kind == "settings"
    assert parse_callback(callback_data(Card("post", "abc123def0", ""), "cancel:0123456789")) == Callback("post", "0123456789", "cancel")
    with pytest.raises(ValueError, match="too long"):
        callback_data(card, "x" * 80)


def test_render_card_marks_selected_options_and_builds_the_keyboard():
    assert render_card(Card("draft", "abc123def0", "plain")) == ("plain", None)
    card = Card("draft", "abc123def0", "body", [[Button("😂 Funny", "tone:funny", True), Button("📝 Describe", "tone:descriptive")],
                                                 [Button("☑ IG post", "tgt:t1", True), Button("☐ TikTok", "tgt:t2")]])
    text, markup = render_card(card)
    rows = markup.inline_keyboard
    assert text == "body" and [b.text for b in rows[0]] == ["✓ 😂 Funny", "📝 Describe"]
    assert [b.text for b in rows[1]] == ["☑ IG post", "☐ TikTok"]                   # toggles already carry ☑/☐: no double mark
    assert rows[0][0].callback_data == "d:abc123def0:tone:funny"


def test_format_inspo():
    assert format_inspo({"status": "duplicate"}) == "Already saved that one."
    out = format_inspo({"status": "saved", "title": "T", "analysis": {"format_name": "F", "hook": "H", "how_to_recreate": "R",
                                                                     "translation": "hello"}, "hint": "send a screenshot"})
    assert all(s in out for s in ("“T”", "Format: F", "Hook: H", "Translation: hello", "send a screenshot"))


# ---------------------------------------------------------------- collector
async def test_collector_merges_a_burst_into_one_batch_and_resets_the_window():
    got = []

    async def ready(key, paths, text, meta):
        got.append((key, paths, text, meta))

    c = Collector(0.08, ready)
    c.add("chat1", path=Path("a.jpg"), text="story only", meta="ack-1")
    await asyncio.sleep(0.04)
    c.add("chat1", path=Path("b.jpg"), meta="ack-2")            # only the first meta (the acknowledgement message) is kept
    await asyncio.sleep(0.04)
    c.add("chat1", text="travel highlight")        # each add restarts the window
    assert c.has_pending("chat1")
    await asyncio.sleep(0.05)
    assert got == []                                # still within the (restarted) window
    await asyncio.sleep(0.12)
    assert got == [("chat1", [Path("a.jpg"), Path("b.jpg")], "story only travel highlight", "ack-1")]
    assert not c.has_pending("chat1")


async def test_collector_keeps_chats_separate_and_ignores_text_only_batches():
    got = []

    async def ready(key, paths, text, meta):
        got.append((key, len(paths)))

    c = Collector(0.03, ready)
    c.add("a", path=Path("1.jpg"))
    c.add("b", path=Path("2.jpg"))
    c.add("b", path=Path("3.jpg"))
    c.add("c", text="just chatting")
    await asyncio.sleep(0.15)
    assert sorted(got) == [("a", 1), ("b", 2)]


async def test_collector_survives_a_failing_handler():
    calls = []

    async def ready(key, paths, text, meta):
        calls.append(key)
        raise RuntimeError("boom")

    c = Collector(0.01, ready)
    c.add("a", path=Path("1.jpg"))
    await asyncio.sleep(0.05)
    c.add("a", path=Path("2.jpg"))
    await asyncio.sleep(0.05)
    assert calls == ["a", "a"]


# ---------------------------------------------------------------- notifier
class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)


async def test_notifier_backlog_thread_safety_buttons_and_chunking():
    n = TelegramNotifier(42)
    n.notify("early message")                                   # before the bot exists -> queued
    bot = FakeBot()
    n.attach(bot, asyncio.get_running_loop())
    await asyncio.sleep(0.05)
    assert [m["text"] for m in bot.sent] == ["early message"] and bot.sent[0]["chat_id"] == 42

    await asyncio.to_thread(n.notify, "from a worker thread", actions=[("A", "cancel:aaaaaa"), ("B", "retry:bbbbbb"), ("C", "approve:cccccc")])
    await asyncio.sleep(0.05)
    msg = bot.sent[-1]
    rows = msg["reply_markup"].inline_keyboard
    assert [[b.text for b in r] for r in rows] == [["A", "B"], ["C"]] and rows[0][0].callback_data == "cancel:aaaaaa"

    bot.sent.clear()
    await asyncio.to_thread(n.notify, "\n".join("z" * 100 for _ in range(100)), actions=[("X", "cancel:aaaaaa")])
    await asyncio.sleep(0.1)
    assert len(bot.sent) >= 3 and all(len(m["text"]) <= 4000 for m in bot.sent)
    assert [m["reply_markup"] is not None for m in bot.sent] == [False] * (len(bot.sent) - 1) + [True]   # buttons on the last piece only


def test_notifier_without_owner_never_sends():
    n = TelegramNotifier(None)
    n.notify("hello")
    assert n._backlog == []


# ---------------------------------------------------------------- frontend (fake updates)
class Att:
    def __init__(self, uid, data=b"", file_name=None, mime_type=None, too_big=False):
        self.file_unique_id, self.data, self.file_name, self.mime_type, self.too_big = uid, data, file_name, mime_type, too_big

    async def get_file(self):
        if self.too_big:
            raise BadRequest("File is too big")
        att = self

        class F:
            async def download_to_drive(self, custom_path):
                Path(custom_path).write_bytes(att.data)

        return F()


def upd(user_id=42, *, text=None, caption=None, photo=None, video=None, document=None, chat=7, data=None, reply_to=None):
    replies, toasts = [], []

    async def reply_text(t, **kw):
        replies.append(t)
        return SimpleNamespace(message_id=500 + len(replies))

    async def answer(text="", *a, **k):
        replies.append("<answered>")
        toasts.append(text)

    async def edit_markup(**k):
        replies.append("<buttons removed>")

    msg = SimpleNamespace(text=text, caption=caption, photo=photo, video=video, animation=None, document=document, reply_text=reply_text,
                          message_id=900, reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to else None)
    query = SimpleNamespace(data=data, answer=answer, edit_message_reply_markup=edit_markup) if data is not None else None
    u = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=msg, effective_chat=SimpleNamespace(id=chat), callback_query=query)
    u.replies, u.toasts = replies, toasts
    return u


def ctx(*args):
    return SimpleNamespace(args=list(args))


@pytest.fixture()
def fe(tmp_path):
    app, notifier, llm = make_app(tmp_path)
    frontend = TelegramFrontend(app, notifier)
    frontend.collector.window = 0.05
    submitted = []
    app.drafts.create = lambda paths, instruction="", source="cli", handle=None: submitted.append((list(paths), instruction, source, handle))
    frontend.submitted, frontend.llm, frontend.notifier_ = submitted, llm, notifier
    return frontend


async def test_non_owner_is_ignored_everywhere(fe, media_dir):
    for handler, kwargs in [(fe.cmd_start, {}), (fe.cmd_help, {}), (fe.cmd_queue, {}), (fe.cmd_trends, {}), (fe.cmd_review, {}),
                            (fe.cmd_drafts, {}), (fe.cmd_settings, {}), (fe.cmd_draft, {}),
                            (fe.cmd_status, {}), (fe.on_text, {"text": "hi"}), (fe.on_media, {"photo": [Att("p", b"x")]})]:
        u = upd(999, **kwargs)
        await handler(u, ctx())
        assert u.replies == [], handler.__name__
    cancelled = []
    fe.app.pipeline.cancel = lambda pid: cancelled.append(pid)
    u = upd(999)
    await fe.cmd_cancel(u, ctx("abcdef1234"))
    u = upd(999, data="cancel:abcdef1234")
    await fe.on_callback(u, ctx())
    await asyncio.sleep(0.1)
    assert cancelled == [] and fe.submitted == []


async def test_start_setup_mode_reveals_user_id_only_when_no_owner_configured(tmp_path):
    app, notifier, _ = make_app(tmp_path, env={"OWNER_TELEGRAM_ID": ""})
    f = TelegramFrontend(app, notifier)
    u = upd(555)
    await f.cmd_start(u, ctx())
    assert "555" in u.replies[0] and "OWNER_TELEGRAM_ID" in u.replies[0]
    u = upd(555, text="post this")
    await f.on_text(u, ctx())
    assert u.replies == []                                      # no owner configured -> nothing is obeyed


async def test_start_greets_the_owner(fe):
    u = upd(42)
    await fe.cmd_start(u, ctx())
    assert "social media manager" in u.replies[0] and "/queue" in u.replies[0]


@needs_ffmpeg
async def test_photo_with_caption_becomes_a_submission_after_the_window(fe, media_dir):
    png = (media_dir / "square.png").read_bytes()
    await fe.on_media(upd(photo=[Att("small", b"s"), Att("big", png)], caption="story only"), ctx())
    assert fe.submitted == []                                   # still collecting
    await asyncio.sleep(0.2)
    (paths, instruction, source, handle), = fe.submitted
    assert instruction == "story only" and source == "telegram" and handle == "501"            # the ack message becomes the draft card
    assert len(paths) == 1 and paths[0].read_bytes() == png and paths[0].suffix == ".jpg"      # largest size was downloaded
    assert paths[0].parent == fe.app.settings.inbox_dir


async def test_album_plus_followup_text_is_one_submission(fe):
    await fe.on_media(upd(photo=[Att("a", b"1")], caption="carousel please"), ctx())
    await fe.on_media(upd(photo=[Att("b", b"2")]), ctx())
    await fe.on_text(upd(text="and tag it Travel"), ctx())
    await asyncio.sleep(0.25)
    (paths, instruction, _, _), = fe.submitted
    assert len(paths) == 2 and instruction == "carousel please and tag it Travel"


async def test_video_and_document_media_are_downloaded_with_sensible_extensions(fe):
    await fe.on_media(upd(video=Att("v", b"vid")), ctx())
    await fe.on_media(upd(document=Att("d", b"doc", file_name="IMG_1.HEIC", mime_type="image/heic")), ctx())
    await asyncio.sleep(0.25)
    (paths, _, _, _), = fe.submitted
    assert sorted(p.suffix for p in paths) == [".heic", ".mp4"]


async def test_free_text_is_answered_by_the_account_manager(fe):
    fe.llm.text_result = "Try a photo dump this weekend."
    u = upd(text="what should I post?")
    await fe.on_text(u, ctx())
    assert u.replies == ["Try a photo dump this weekend."] and fe.submitted == []


async def test_files_over_the_bot_limit_get_actionable_advice(fe):
    u = upd(video=Att("big", too_big=True))
    await fe.on_media(u, ctx())
    assert "20 MB" in u.replies[0] and "TELEGRAM_API_BASE" in u.replies[0]
    assert fe.submitted == [] and not fe.collector.has_pending("7")


async def test_data_file_without_inspo_caption_gets_a_hint(fe):
    u = upd(document=Att("j", b"{}", file_name="user_data.json", mime_type="application/json"))
    await fe.on_media(u, ctx())
    assert "inspo export" in u.replies[0] and not fe.collector.has_pending("7")


async def test_inspo_export_import_via_document(fe):
    export = b'{"Likes": [{"Link": "https://www.tiktok.com/@a/video/1"}, {"Link": "https://v.douyin.com/x/"}]}'
    u = upd(document=Att("j", export, file_name="user_data.json", mime_type="application/json"), caption="inspo export")
    await fe.on_media(u, ctx())
    assert "Found 2 link(s); saved 2 new" in u.replies[0]
    assert len(fe.app.store.list_inspiration(10)) == 2 and fe.submitted == []


@needs_ffmpeg
async def test_inspo_screenshot_is_analysed_not_posted(fe, media_dir):
    u = upd(photo=[Att("s", (media_dir / "square.png").read_bytes())], caption="inspo 抖音 style")
    await fe.on_media(u, ctx())
    assert "Saved to your inspiration board" in u.replies[0] and "Split-screen reaction" in u.replies[0]
    await asyncio.sleep(0.15)
    assert fe.submitted == []


async def test_inspo_command(fe):
    u = upd()
    await fe.cmd_inspo(u, ctx())
    assert "Usage: /inspo" in u.replies[0]
    u = upd()
    await fe.cmd_inspo(u, ctx("https://v.douyin.com/abc/", "love", "this"))
    assert "screenshot" in u.replies[0]
    assert fe.app.store.list_inspiration(1)[0]["note"] == "love this"


async def test_queue_status_trends_highlights_review_commands(fe):
    u = upd(); await fe.cmd_queue(u, ctx()); assert "Nothing queued" in u.replies[0]
    u = upd(); await fe.cmd_status(u, ctx()); assert "DRY RUN" in u.replies[0] and "dry-run mode" in u.replies[0]
    u = upd(); await fe.cmd_trends(u, ctx()); assert "No trends yet" in u.replies[0]
    u = upd(); await fe.cmd_trends(u, ctx("refresh")); assert "Researching" in u.replies[0] and "Photo dump" in u.replies[1]
    u = upd(); await fe.cmd_highlights(u, ctx()); assert "No Stories waiting" in u.replies[0]
    u = upd(); await fe.cmd_highlights_done(u, ctx()); assert "Nothing to mark" in u.replies[0]
    u = upd(); await fe.cmd_review(u, ctx()); assert "Weekly feed review" in u.replies[0]


async def test_trends_refresh_failure_is_reported(fe):
    def boom(**kw):
        raise RuntimeError("search down")
    fe.llm.json = boom
    u = upd()
    await fe.cmd_trends(u, ctx("refresh"))
    assert "Trend research failed" in u.replies[-1]


async def test_id_commands_and_callbacks_call_the_pipeline(fe):
    calls = []
    fe.app.pipeline.cancel = lambda pid: calls.append(("cancel", pid)) or True
    fe.app.pipeline.retry = lambda pid: calls.append(("retry", pid)) or True
    fe.app.pipeline.confirm_posted = lambda pid: calls.append(("posted", pid)) or True
    u = upd(); await fe.cmd_cancel(u, ctx("[abcdef1234]"))
    await fe.cmd_retry(upd(), ctx("abcdef1236"))
    u = upd(); await fe.cmd_cancel(u, ctx())
    assert "Usage: /cancel" in u.replies[0]
    cb = upd(data="posted:abcdef1237")                                     # the manual hand-off "✅ I posted it" button
    await fe.on_callback(cb, ctx())
    assert cb.replies == ["<answered>", "<buttons removed>"]
    cb = upd(data="p:abcdef1230:cancel:abcdef1238")                        # a Cancel button on an approved-draft card
    await fe.on_callback(cb, ctx())
    bad = upd(data="rm -rf /")
    await fe.on_callback(bad, ctx())
    assert bad.toasts == [""] and calls == [("cancel", "abcdef1234"), ("retry", "abcdef1236"), ("posted", "abcdef1237"), ("cancel", "abcdef1238")]


# ---------------------------------------------------------------- application wiring
def _real_update(application, user_id, text, command=False, photo=False):
    from telegram import Update
    msg = {"message_id": 1, "date": 1700000000, "chat": {"id": user_id, "type": "private"},
           "from": {"id": user_id, "is_bot": False, "first_name": "x"}}
    if photo:
        msg["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 10, "height": 10}]
    else:
        msg["text"] = text
        if command:
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.de_json({"update_id": 1, "message": msg}, application.bot)


def _handled_by(application, update):
    """Names of the registered callbacks that would accept this update."""
    hits = []
    for group in application.handlers.values():
        for h in group:
            if h.check_update(update):
                hits.append(h.callback.__name__)
    return hits


def test_build_telegram_registers_commands_and_owner_filter_blocks_strangers(tmp_path):
    app, notifier, _ = make_app(tmp_path)
    application, frontend = build_telegram(app, notifier)
    from telegram import User
    application.bot._bot_user = User(id=1, first_name="bot", is_bot=True, username="testbot")   # normally fetched via getMe at startup
    handlers = [h for group in application.handlers.values() for h in group]
    commands = set().union(*(h.commands for h in handlers if hasattr(h, "commands")))
    assert {"start", "queue", "drafts", "draft", "settings", "cancel", "retry", "inspo", "highlights_done", "status", "trends"} <= commands
    assert "approve" not in commands                                       # drafts are approved with a button, posts are not approved

    # the owner (42) reaches every kind of handler ...
    assert _handled_by(application, _real_update(application, 42, "/queue", command=True)) == ["cmd_queue"]
    assert _handled_by(application, _real_update(application, 42, "hello there")) == ["on_text"]
    assert _handled_by(application, _real_update(application, 42, "", photo=True)) == ["on_media"]
    # ... a stranger (99) reaches nothing except /start (which only prints their id in setup mode)
    assert _handled_by(application, _real_update(application, 99, "/queue", command=True)) == []
    assert _handled_by(application, _real_update(application, 99, "post this please")) == []
    assert _handled_by(application, _real_update(application, 99, "", photo=True)) == []
    assert _handled_by(application, _real_update(application, 99, "/start", command=True)) == ["cmd_start"]


def test_build_telegram_local_bot_api_server_mode(tmp_path):
    app, notifier, _ = make_app(tmp_path, env={"TELEGRAM_API_BASE": "http://localhost:8081/"})
    application, _ = build_telegram(app, notifier)
    assert application.bot.local_mode is True
    assert application.bot.base_url.startswith("http://localhost:8081/bot") and "/file/bot" in application.bot.base_file_url


# ================================================================ the draft experience (real DraftService, fake Telegram)
@pytest.fixture()
def fe_real(tmp_path):
    app, notifier, llm = make_app(tmp_path)
    frontend = TelegramFrontend(app, notifier)
    frontend.collector.window = 0.05
    frontend.llm, frontend.notifier_ = llm, notifier
    return frontend


async def wait_for(predicate, timeout=15.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


async def send_photo(fe, media_dir, caption=""):
    await fe.on_media(upd(photo=[Att("p", (media_dir / "photo.jpg").read_bytes())], caption=caption), ctx())
    await wait_for(lambda: any(d["state"] == "ready" for d in fe.app.store.list_drafts()))
    return fe.app.store.list_drafts()[0]["id"]


@needs_ffmpeg
async def test_share_a_photo_get_an_ack_that_becomes_the_draft_card(fe_real, media_dir):
    u = upd(photo=[Att("p", (media_dir / "photo.jpg").read_bytes())], caption="funny 中文")
    await fe_real.on_media(u, ctx())
    assert u.replies == ["📥 Got it - one moment…"]                          # instant acknowledgement, before any AI work
    await wait_for(lambda: any(d["state"] == "ready" for d in fe_real.app.store.list_drafts()))
    cards = fe_real.notifier_.cards
    assert {h for _, h in cards} == {"501"}                                    # every update edits that same Telegram message
    d = fe_real.app.store.list_drafts()[0]
    assert (d["tone"], d["lang"], d["card_ref"]) == ("funny", "zh", "501")
    assert "tone:funny" in [b.action for row in cards[-1][0].rows for b in row]
    assert fe_real.app.store.list_posts() == []                                # a draft, not a post


@needs_ffmpeg
async def test_tapping_a_tone_rewrites_without_looking_at_the_photo_again(fe_real, media_dir):
    did = await send_photo(fe_real, media_dir)
    cb = upd(data=f"d:{did}:tone:hype")
    await fe_real.on_callback(cb, ctx())
    assert cb.toasts == ["Rewriting…"]                                        # answered immediately so Telegram stops the spinner
    assert fe_real.app.store.get_draft(did)["tone"] == "hype"
    vision = [c for c in fe_real.llm.calls if c["kind"] == "json" and isinstance(c["content"], list)]
    assert len(vision) == 1 and len(fe_real.llm.write_calls) == 2


@needs_ffmpeg
async def test_my_caption_and_change_prompts_capture_the_next_text(fe_real, media_dir):
    did = await send_photo(fe_real, media_dir)
    cb = upd(data=f"d:{did}:mycap")
    await fe_real.on_callback(cb, ctx())
    assert "exactly as written" in cb.replies[-1] and fe_real.awaiting == {"7": (did, "mycap")}
    await fe_real.on_text(upd(text="Sunday coffee, my way"), ctx())
    assert fe_real.awaiting == {} and fe_real.app.store.get_draft(did)["targets"][0]["caption"] == "Sunday coffee, my way"
    assert fe_real.llm.write_calls.__len__() == 1                               # used verbatim: no AI call

    cb = upd(data=f"d:{did}:change")
    await fe_real.on_callback(cb, ctx())
    assert "What should I change" in cb.replies[-1] and fe_real.awaiting == {"7": (did, "change")}
    await fe_real.on_text(upd(text="shorter, add a food emoji"), ctx())
    assert "CHANGE REQUEST (for this rewrite):\nshorter, add a food emoji" in fe_real.llm.write_calls[-1] and fe_real.awaiting == {}


@needs_ffmpeg
async def test_replying_to_the_draft_card_edits_it(fe_real, media_dir):
    did = await send_photo(fe_real, media_dir)
    card_message = int(fe_real.app.store.get_draft(did)["card_ref"])
    u = upd(text="less emoji please", reply_to=card_message)
    await fe_real.on_text(u, ctx())
    assert u.replies == [] and "CHANGE REQUEST (for this rewrite):\nless emoji please" in fe_real.llm.write_calls[-1]
    await fe_real.on_text(upd(text="= Exactly this caption", reply_to=card_message), ctx())
    assert fe_real.app.store.get_draft(did)["targets"][0]["caption"] == "Exactly this caption"
    fe_real.llm.text_result = "chat answer"
    u = upd(text="reply to something else", reply_to=12345)                      # a reply to an unrelated message is just chat
    await fe_real.on_text(u, ctx())
    assert u.replies == ["chat answer"]


@needs_ffmpeg
async def test_approve_button_schedules_and_swaps_the_card_for_cancel_buttons(fe_real, media_dir):
    did = await send_photo(fe_real, media_dir)
    cb = upd(data=f"d:{did}:approve")
    await fe_real.on_callback(cb, ctx())
    assert cb.toasts == ["Scheduling…"] and len(fe_real.app.store.list_posts()) == 1
    card = fe_real.notifier_.cards[-1][0]
    assert card.kind == "post" and [b.action for b in card.rows[0]][0].startswith("cancel:")
    text, markup = render_card(card)
    assert markup.inline_keyboard[0][0].callback_data.startswith(f"p:{did}:cancel:")


@needs_ffmpeg
async def test_strangers_cannot_tap_draft_buttons(fe_real, media_dir):
    did = await send_photo(fe_real, media_dir)
    before = fe_real.app.store.get_draft(did)["tone"]
    cb = upd(user_id=999, data=f"d:{did}:tone:hype")
    await fe_real.on_callback(cb, ctx())
    assert fe_real.app.store.get_draft(did)["tone"] == before and cb.toasts == [""]
    await fe_real.on_callback(upd(user_id=999, data=f"d:{did}:approve"), ctx())
    assert fe_real.app.store.list_posts() == []


async def test_settings_card_and_taps_persist_and_edit_in_place(fe_real):
    await fe_real.cmd_settings(upd(), ctx())
    card, handle = fe_real.notifier_.cards[-1]
    assert card.kind == "settings" and "Defaults for every new draft" in card.text
    cb = upd(data="s:-:tone:hype")
    await fe_real.on_callback(cb, ctx())
    assert cb.toasts == ["Saved"] and fe_real.app.prefs.tone == "hype"
    again, again_handle = fe_real.notifier_.cards[-1]
    assert again_handle == "900" and [b for row in again.rows for b in row if b.action == "tone:hype"][0].selected
    cb = upd(data="s:-:tone:unicorn")
    await fe_real.on_callback(cb, ctx())
    assert cb.toasts[0].startswith("Unknown tone") and fe_real.app.prefs.tone == "hype"


@needs_ffmpeg
async def test_drafts_and_draft_commands(fe_real, media_dir):
    await fe_real.cmd_drafts(upd(), ctx())
    assert "No open drafts" in fe_real.notifier_.texts[-1]
    did = await send_photo(fe_real, media_dir)
    await fe_real.cmd_drafts(upd(), ctx())
    text, acts = fe_real.notifier_.messages[-1]
    assert f"[{did}]" in text and acts == [("Open", f"d:{did}:show")]
    u = upd(); await fe_real.cmd_draft(u, ctx())
    assert "Usage: /draft" in u.replies[0]
    n = len(fe_real.notifier_.cards)
    await fe_real.cmd_draft(upd(), ctx(f"[{did}]"))
    assert len(fe_real.notifier_.cards) == n + 1 and fe_real.notifier_.cards[-1][0].ref == did
    cb = upd(data=f"d:{did}:show")                                              # the "Open" button
    await fe_real.on_callback(cb, ctx())
    assert len(fe_real.notifier_.cards) == n + 2


# ---------------------------------------------------------------- TelegramNotifier: cards edit in place
class CardBot:
    def __init__(self):
        self.sent, self.edited, self.edit_error, self.commands = [], [], None, None
        self._n = 100

    async def send_message(self, **kw):
        self._n += 1
        self.sent.append(kw)
        return SimpleNamespace(message_id=self._n)

    async def edit_message_text(self, **kw):
        if self.edit_error:
            raise self.edit_error
        self.edited.append(kw)

    async def set_my_commands(self, cmds):
        if isinstance(self.commands, Exception):
            raise self.commands
        self.commands = cmds


CARD = Card("draft", "abc123def0", "hello", [[Button("✅ Approve", "approve")]])


async def test_notifier_present_sends_then_edits_in_place():
    n, bot = TelegramNotifier(42), CardBot()
    n.attach(bot, asyncio.get_running_loop())
    handle = await asyncio.to_thread(n.present, CARD)
    assert handle == "101" and bot.sent[0]["chat_id"] == 42 and bot.sent[0]["reply_markup"].inline_keyboard[0][0].callback_data == "d:abc123def0:approve"
    again = await asyncio.to_thread(n.present, Card("draft", "abc123def0", "updated"), handle=handle)
    assert again == "101" and bot.edited[0]["message_id"] == 101 and bot.edited[0]["text"] == "updated" and len(bot.sent) == 1


async def test_notifier_present_handles_unchanged_and_vanished_messages():
    from telegram.error import BadRequest
    n, bot = TelegramNotifier(42), CardBot()
    n.attach(bot, asyncio.get_running_loop())
    bot.edit_error = BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")
    assert await asyncio.to_thread(n.present, CARD, handle="55") == "55" and bot.sent == []         # nothing changed: nothing re-sent
    bot.edit_error = BadRequest("Message to edit not found")
    assert await asyncio.to_thread(n.present, CARD, handle="55") == "101" and len(bot.sent) == 1   # deleted message: send a fresh card
    assert await asyncio.to_thread(n.present, CARD, handle="not-a-number") == "102"


async def test_notifier_present_truncates_and_guards_against_loop_thread_deadlock():
    n, bot = TelegramNotifier(42), CardBot()
    n.attach(bot, asyncio.get_running_loop())
    await asyncio.to_thread(n.present, Card("draft", "abc123def0", "x" * 6000))
    assert len(bot.sent[0]["text"]) <= 4000
    with pytest.raises(RuntimeError, match="deadlock"):
        n.present(CARD)                                                         # called on the event-loop thread


def test_notifier_present_without_telegram_is_a_safe_noop():
    assert TelegramNotifier(None).present(CARD, handle="9") == "9"
    assert TelegramNotifier(42).present(CARD) == "0"                            # bot not attached yet
    TelegramNotifier(42).send_files(["x"])                                       # no crash


async def test_command_menu_is_set_and_never_lists_a_command_without_a_handler(tmp_path):
    from telegram.error import TelegramError
    from socialagent.channels.telegram_bot import COMMAND_MENU, setup_commands
    bot = CardBot()
    await setup_commands(bot)
    assert [c.command for c in bot.commands] == [c for c, _ in COMMAND_MENU]
    bot.commands = TelegramError("nope")
    await setup_commands(bot)                                                    # best effort: must not raise
    app, notifier, _ = make_app(tmp_path)
    application, _ = build_telegram(app, notifier)
    registered = set().union(*(h.commands for g in application.handlers.values() for h in g if hasattr(h, "commands")))
    assert {c for c, _ in COMMAND_MENU} <= registered

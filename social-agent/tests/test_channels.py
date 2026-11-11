import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from conftest import needs_ffmpeg
from helpers import make_app
from socialagent.channels.telegram_bot import (Collector, TelegramFrontend, TelegramNotifier, build_telegram,
                                               chunk_text, format_inspo, parse_callback)

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
    ("cancel:abc123def0", ("cancel", "abc123def0")), ("approve:0123456789", ("approve", "0123456789")),
    ("retry:abcdef", ("retry", "abcdef")), ("delete:abcdef", None), ("cancel:xyz", None), ("cancel:", None), ("", None),
    (None, None), ("cancel:abc123def0;rm -rf", None),
])
def test_parse_callback(data, expected):
    assert parse_callback(data) == expected


def test_format_inspo():
    assert format_inspo({"status": "duplicate"}) == "Already saved that one."
    out = format_inspo({"status": "saved", "title": "T", "analysis": {"format_name": "F", "hook": "H", "how_to_recreate": "R",
                                                                     "translation": "hello"}, "hint": "send a screenshot"})
    assert all(s in out for s in ("“T”", "Format: F", "Hook: H", "Translation: hello", "send a screenshot"))


# ---------------------------------------------------------------- collector
async def test_collector_merges_a_burst_into_one_batch_and_resets_the_window():
    got = []

    async def ready(key, paths, text):
        got.append((key, paths, text))

    c = Collector(0.08, ready)
    c.add("chat1", path=Path("a.jpg"), text="story only")
    await asyncio.sleep(0.04)
    c.add("chat1", path=Path("b.jpg"))
    await asyncio.sleep(0.04)
    c.add("chat1", text="travel highlight")        # each add restarts the window
    assert c.has_pending("chat1")
    await asyncio.sleep(0.05)
    assert got == []                                # still within the (restarted) window
    await asyncio.sleep(0.12)
    assert got == [("chat1", [Path("a.jpg"), Path("b.jpg")], "story only travel highlight")]
    assert not c.has_pending("chat1")


async def test_collector_keeps_chats_separate_and_ignores_text_only_batches():
    got = []

    async def ready(key, paths, text):
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

    async def ready(key, paths, text):
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


def upd(user_id=42, *, text=None, caption=None, photo=None, video=None, document=None, chat=7, data=None):
    replies = []

    async def reply_text(t, **kw):
        replies.append(t)

    async def answer(*a, **k):
        replies.append("<answered>")

    async def edit_markup(**k):
        replies.append("<buttons removed>")

    msg = SimpleNamespace(text=text, caption=caption, photo=photo, video=video, animation=None, document=document, reply_text=reply_text)
    query = SimpleNamespace(data=data, answer=answer, edit_message_reply_markup=edit_markup) if data is not None else None
    u = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=msg, effective_chat=SimpleNamespace(id=chat), callback_query=query)
    u.replies = replies
    return u


def ctx(*args):
    return SimpleNamespace(args=list(args))


@pytest.fixture()
def fe(tmp_path):
    app, notifier, llm = make_app(tmp_path)
    frontend = TelegramFrontend(app, notifier)
    frontend.collector.window = 0.05
    submitted = []
    app.pipeline.submit = lambda paths, instruction="", source="cli": submitted.append((list(paths), instruction, source))
    frontend.submitted, frontend.llm, frontend.notifier_ = submitted, llm, notifier
    return frontend


async def test_non_owner_is_ignored_everywhere(fe, media_dir):
    for handler, kwargs in [(fe.cmd_start, {}), (fe.cmd_help, {}), (fe.cmd_queue, {}), (fe.cmd_trends, {}), (fe.cmd_review, {}),
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
    (paths, instruction, source), = fe.submitted
    assert instruction == "story only" and source == "telegram"
    assert len(paths) == 1 and paths[0].read_bytes() == png and paths[0].suffix == ".jpg"      # largest size was downloaded
    assert paths[0].parent == fe.app.settings.inbox_dir


async def test_album_plus_followup_text_is_one_submission(fe):
    await fe.on_media(upd(photo=[Att("a", b"1")], caption="carousel please"), ctx())
    await fe.on_media(upd(photo=[Att("b", b"2")]), ctx())
    await fe.on_text(upd(text="and tag it Travel"), ctx())
    await asyncio.sleep(0.25)
    (paths, instruction, _), = fe.submitted
    assert len(paths) == 2 and instruction == "carousel please and tag it Travel"


async def test_video_and_document_media_are_downloaded_with_sensible_extensions(fe):
    await fe.on_media(upd(video=Att("v", b"vid")), ctx())
    await fe.on_media(upd(document=Att("d", b"doc", file_name="IMG_1.HEIC", mime_type="image/heic")), ctx())
    await asyncio.sleep(0.25)
    (paths, _, _), = fe.submitted
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
    fe.app.pipeline.approve = lambda pid: calls.append(("approve", pid)) or True
    fe.app.pipeline.retry = lambda pid: calls.append(("retry", pid)) or True
    u = upd(); await fe.cmd_cancel(u, ctx("[abcdef1234]"))
    await fe.cmd_approve(upd(), ctx("abcdef1235"))
    await fe.cmd_retry(upd(), ctx("abcdef1236"))
    u = upd(); await fe.cmd_cancel(u, ctx())
    assert "Usage: /cancel" in u.replies[0]
    cb = upd(data="approve:abcdef1237")
    await fe.on_callback(cb, ctx())
    assert cb.replies == ["<answered>", "<buttons removed>"]
    bad = upd(data="rm -rf /")
    await fe.on_callback(bad, ctx())
    assert calls == [("cancel", "abcdef1234"), ("approve", "abcdef1235"), ("retry", "abcdef1236"), ("approve", "abcdef1237")]


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
    assert {"start", "queue", "cancel", "approve", "retry", "inspo", "highlights_done", "status", "trends"} <= commands

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

"""Telegram front end: share a photo/video to your private bot and get a DRAFT back to review.

Seamless flow
-------------
1. Share media to the bot (gallery -> Share -> Telegram -> your bot; or attach in the chat). Albums and a quick
   follow-up text within COLLECT_WINDOW_SECONDS are merged into one submission.
2. The bot answers at once ("Got it...") and EDITS that same message into the draft card when Claude is done.
3. Tap tone / language / where-to-post, rewrite, then Approve. Or reply to the card with what to change
   ("shorter, mention Sunday"); start with = to use your own caption. Nothing is published before Approve.

Only OWNER_TELEGRAM_ID is obeyed (checked by a filter AND again inside every handler).
The cloud Bot API limits downloads to 20 MB; for bigger videos set TELEGRAM_API_BASE (local Bot API server)
or use the HTTP endpoint (see http_api.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from ..app import App
from ..cards import Card

log = logging.getLogger(__name__)

TG_LIMIT = 4000
HELP = (
    "Send me a photo or video (or several) and I'll draft the post.\n\n"
    "I look at it, write a caption in your default tone and language, and show a draft card. Tap to change the tone "
    "(😂 Funny, 📝 Describe, ✨ Aesthetic…), the language (EN / 简体 / 繁體 / EN+中) or where it goes (IG post / story / "
    "TikTok), then ✅ Approve. Nothing is posted until you tap.\n\n"
    "Shortcuts\n• Type hints in the caption: “funny 中文”, “story only”, “post at 8pm”.\n"
    "• Reply to a draft with what to change (“shorter”, “less emoji”), or start with = to use your own caption.\n\n"
    "Commands\n/drafts - open drafts   /settings - default tone, language, how posts go out\n/queue - what's scheduled   "
    "/cancel <id>   /retry <id>\n/trends - current trends (/trends refresh)   /review - weekly review\n"
    "/highlights - Highlights checklist, /highlights_done [name]\n"
    "/inspo <TikTok or Douyin link> [note] - save inspiration; or send a screenshot with the caption “inspo”\n"
    "/status - modes and connections\n\nAnything else you type, I'll answer as your account manager."
)
COMMAND_MENU = [
    ("drafts", "Open drafts"), ("settings", "Default tone, language, publishing"), ("queue", "What's scheduled"),
    ("trends", "Current trends"), ("review", "Weekly feed review"), ("highlights", "Highlights checklist"),
    ("inspo", "Save a TikTok/Douyin reference"), ("status", "Modes and connections"), ("help", "How this works"),
]
TOASTS = {"tone": "Rewriting…", "lang": "Rewriting…", "rewrite": "Rewriting…", "approve": "Scheduling…",
          "approve!": "Scheduling…", "now": "Scheduling…", "retry": "Trying again…"}
CARD_PREFIX = {"draft": "d", "settings": "s", "post": "p"}


def chunk_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on line breaks so each piece fits Telegram's message limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


# ---------------------------------------------------------------------------
# Callback data (Telegram limits it to 64 bytes): "d:<draft>:<action>", "s:-:<action>", "p:<draft>:cancel:<post>",
# plus the plain "cancel|retry|posted:<post>" used by notification buttons.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Callback:
    kind: str  # "draft" | "settings" | "post"
    ref: str  # draft id, "-" for settings, post id
    action: str


_DRAFT_RE = re.compile(r"d:([0-9a-f]{6,16}):([a-z0-9:!_-]{1,40})")
_SETTINGS_RE = re.compile(r"s:-:([a-z0-9:!_-]{1,40})")
_POSTCARD_RE = re.compile(r"p:[0-9a-f]{6,16}:(cancel):([0-9a-f]{6,16})")
_POST_RE = re.compile(r"(cancel|retry|posted):([0-9a-f]{6,16})")


def parse_callback(data: str | None) -> Callback | None:
    data = data or ""
    if m := _DRAFT_RE.fullmatch(data):
        return Callback("draft", m.group(1), m.group(2))
    if m := _SETTINGS_RE.fullmatch(data):
        return Callback("settings", "-", m.group(1))
    if m := _POSTCARD_RE.fullmatch(data):
        return Callback("post", m.group(2), m.group(1))
    if m := _POST_RE.fullmatch(data):
        return Callback("post", m.group(2), m.group(1))
    return None


def callback_data(card: Card, action: str) -> str:
    data = f"{CARD_PREFIX.get(card.kind, 'd')}:{card.ref}:{action}"
    if len(data.encode()) > 64:
        raise ValueError(f"callback data too long ({len(data.encode())} bytes): {data}")
    return data


def render_card(card: Card) -> tuple[str, InlineKeyboardMarkup | None]:
    """Card -> (text, inline keyboard). Selected options get a ✓ (target toggles already carry ☑/☐)."""
    if not card.rows:
        return card.text, None
    rows = []
    for row in card.rows:
        rows.append([InlineKeyboardButton(("✓ " if b.selected and not b.label.startswith(("☑", "☐")) else "") + b.label,
                                          callback_data=callback_data(card, b.action)) for b in row])
    return card.text, InlineKeyboardMarkup(rows)


def format_inspo(res: dict[str, Any]) -> str:
    if res.get("status") == "duplicate":
        return "Already saved that one."
    a = res.get("analysis") or {}
    lines = ["💡 Saved to your inspiration board."]
    if res.get("title"):
        lines.append(f"“{res['title']}”")
    if a.get("format_name"):
        lines.append(f"Format: {a['format_name']}\nHook: {a.get('hook', '')}\nHow to recreate: {a.get('how_to_recreate', '')}")
        if a.get("translation"):
            lines.append(f"Translation: {a['translation']}")
    if res.get("hint"):
        lines.append(f"ℹ️ {res['hint']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
class Collector:
    """Debounce: merges rapid-fire media/text from one chat into a single batch."""

    class _Batch:
        def __init__(self) -> None:
            self.paths: list[Path] = []
            self.texts: list[str] = []
            self.meta: Any = None
            self.task: asyncio.Task | None = None

    def __init__(self, window: float, on_ready: Callable[[str, list[Path], str, Any], Awaitable[None]]):
        self.window, self.on_ready = window, on_ready
        self._batches: dict[str, Collector._Batch] = {}
        self._tasks: set[asyncio.Task] = set()

    def has_pending(self, key: str) -> bool:
        return key in self._batches

    def add(self, key: str, *, path: Path | None = None, text: str = "", meta: Any = None) -> None:
        batch = self._batches.setdefault(key, Collector._Batch())
        if path is not None:
            batch.paths.append(path)
        if text.strip():
            batch.texts.append(text.strip())
        if batch.meta is None:
            batch.meta = meta  # first writer wins (the acknowledgement message to turn into the draft card)
        if batch.task:
            batch.task.cancel()
        batch.task = asyncio.create_task(self._fire(key))
        self._tasks.add(batch.task)
        batch.task.add_done_callback(self._tasks.discard)

    async def _fire(self, key: str) -> None:
        await asyncio.sleep(self.window)
        batch = self._batches.pop(key, None)
        if batch and batch.paths:
            try:
                await self.on_ready(key, batch.paths, " ".join(batch.texts), batch.meta)
            except Exception:
                log.exception("Collector handler failed")


class TelegramNotifier:
    """Sends the core's messages and cards to the owner. Safe to call from worker threads (never from the event loop)."""

    def __init__(self, owner_id: int | None):
        self.owner_id = owner_id
        self._bot: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._backlog: list[tuple[str, list[tuple[str, str]] | None]] = []

    def attach(self, bot: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._bot, self._loop = bot, loop
        self._loop_thread = threading.get_ident()
        backlog, self._backlog = self._backlog, []
        for text, actions in backlog:
            self.notify(text, actions=actions)

    def notify(self, text: str, *, actions: list[tuple[str, str]] | None = None) -> None:
        if self.owner_id is None:
            log.info("(no owner configured) %s", text[:120])
            return
        if self._bot is None or self._loop is None:
            self._backlog.append((text, actions))
            return
        fut = asyncio.run_coroutine_threadsafe(self._send(text, actions), self._loop)
        fut.add_done_callback(lambda f: f.exception() and log.warning("Telegram send failed: %s", f.exception()))

    async def _send(self, text: str, actions: list[tuple[str, str]] | None) -> None:
        pieces = chunk_text(text)
        for i, piece in enumerate(pieces):
            markup = None
            if actions and i == len(pieces) - 1:
                rows = [[InlineKeyboardButton(label, callback_data=data) for label, data in actions[j:j + 2]]
                        for j in range(0, len(actions), 2)]
                markup = InlineKeyboardMarkup(rows)
            await self._bot.send_message(chat_id=self.owner_id, text=piece, reply_markup=markup)

    def present(self, card: Card, *, handle: str | None = None) -> str:
        """Show a card; with a handle, edit that message in place. Blocks until Telegram has answered."""
        if self.owner_id is None or self._bot is None or self._loop is None:
            log.info("(card not sent: Telegram not ready) %s", card.text[:80])
            return handle or "0"
        if threading.get_ident() == self._loop_thread:
            raise RuntimeError("TelegramNotifier.present() would deadlock on the event-loop thread; call it from a worker thread")
        return asyncio.run_coroutine_threadsafe(self._present(card, handle), self._loop).result(timeout=40)

    async def _present(self, card: Card, handle: str | None) -> str:
        text, markup = render_card(card)
        text = text[:TG_LIMIT] if len(text) > TG_LIMIT else text
        if handle:
            try:
                await self._bot.edit_message_text(chat_id=self.owner_id, message_id=int(handle), text=text, reply_markup=markup)
                return handle
            except BadRequest as exc:
                if "not modified" in str(exc).lower():  # the owner tapped something that changed nothing
                    return handle
                log.info("Could not edit message %s (%s); sending a new one", handle, exc)
            except (TelegramError, ValueError) as exc:
                log.info("Could not edit message %s (%s); sending a new one", handle, exc)
        sent = await self._bot.send_message(chat_id=self.owner_id, text=text, reply_markup=markup)
        return str(sent.message_id)

    def send_files(self, paths: list[str], caption: str = "") -> None:
        if self.owner_id is None or self._bot is None or self._loop is None:
            return

        async def go() -> None:
            for p in paths:
                if Path(p).stat().st_size > 49 * 1024 * 1024:  # the cloud Bot API cannot upload more than 50 MB
                    await self._bot.send_message(chat_id=self.owner_id, text=f"(File too large to send back: {Path(p).name})")
                    continue
                with open(p, "rb") as fh:
                    await self._bot.send_document(chat_id=self.owner_id, document=fh, caption=caption or None)

        fut = asyncio.run_coroutine_threadsafe(go(), self._loop)
        fut.add_done_callback(lambda f: f.exception() and log.warning("Telegram file send failed: %s", f.exception()))


class TelegramFrontend:
    def __init__(self, app: App, notifier: Any):
        self.app, self.notifier = app, notifier
        self.owner_id = app.settings.owner_telegram_id
        self.collector = Collector(app.settings.collect_window_seconds, self._on_ready)
        self.awaiting: dict[str, tuple[str, str]] = {}  # chat -> (draft id, "mycap" | "change"): next text is the answer

    # -- auth ----------------------------------------------------------------------
    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        ok = bool(user and self.owner_id is not None and user.id == self.owner_id)
        if not ok:
            log.warning("Ignoring message from unauthorized user %s", getattr(user, "id", None))
        return ok

    async def _reply(self, update: Update, text: str) -> None:
        for piece in chunk_text(text):
            await update.effective_message.reply_text(piece)

    # -- commands ------------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if self.owner_id is None:  # setup mode: reveal the id so the owner can lock the bot to themselves
            await self._reply(update, f"👋 Setup: your Telegram user id is {user.id}.\nPut it in OWNER_TELEGRAM_ID "
                                      "in your .env and restart me. Until then I won't act on anything.")
            return
        if not self._authorized(update):
            return
        await self._reply(update, f"👋 Hi! I'm your social media manager for {self.app.brand.account_name}.\n\n{HELP}")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self._reply(update, HELP)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self._reply(update, self.app.pipeline.queue_text())

    async def cmd_drafts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await asyncio.to_thread(self.app.drafts.list_open)

    async def cmd_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /draft <id>  (or /drafts to see open ones)")
            return
        await asyncio.to_thread(self.app.drafts.show, context.args[0].strip("[]"))

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await asyncio.to_thread(self.notifier.present, self.app.drafts.settings_card())

    async def cmd_trends(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if context.args and context.args[0].lower() == "refresh":
            await self._reply(update, "🔎 Researching trends (this can take a minute)…")
            try:
                await asyncio.to_thread(self.app.trends.refresh)
            except Exception as exc:
                await self._reply(update, f"❌ Trend research failed: {exc}")
                return
        text = self.app.trends.trends_for_prompt(10)
        await self._reply(update, "🔥 Current trends:\n" + text if text else "No trends yet - try /trends refresh.")

    async def cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self._reply(update, self.app.pipeline.weekly_review())

    async def cmd_highlights(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        from ..feed import highlight_checklist
        text = highlight_checklist(self.app.store, self.app.brand, self.app.pipeline.zone)
        await self._reply(update, text or "No Stories waiting to be added to Highlights.")

    async def cmd_highlights_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        name = " ".join(context.args) if context.args else None
        n = self.app.store.mark_highlights_done(name)
        await self._reply(update, f"✅ Marked {n} Story(ies) as added." if n else "Nothing to mark.")

    async def _id_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, fn: Callable[[str], bool], verb: str) -> None:
        if not self._authorized(update):
            return
        if not context.args:
            await self._reply(update, f"Usage: /{verb} <id>  (post ids are shown in brackets, e.g. [a1b2c3d4e5])")
            return
        await asyncio.to_thread(fn, context.args[0].strip("[]"))

    async def cmd_cancel(self, update, context):
        await self._id_command(update, context, self.app.pipeline.cancel, "cancel")

    async def cmd_retry(self, update, context):
        await self._id_command(update, context, self.app.pipeline.retry, "retry")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        s = self.app.settings
        prefs = self.app.prefs
        lines = [f"Mode: {'🧪 DRY RUN (API posts are simulated)' if s.dry_run else '🟢 LIVE'} · editing: {s.edit_level} · "
                 f"undo window {s.undo_window_seconds}s · vision model {s.claude_model} · writer {s.writer_model or s.claude_model}",
                 f"Defaults: tone {prefs.tone}, language {prefs.language} · Instagram: {prefs.method('instagram')}, TikTok: {prefs.method('tiktok')}"]
        for pub in self.app.publishers.values():
            lines += await asyncio.to_thread(pub.selfcheck)
        await self._reply(update, "\n".join(lines))

    async def cmd_inspo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /inspo <TikTok or Douyin link> [note]\nOr send a screenshot / screen-recording "
                                      "frame with the caption “inspo <note>”. For a TikTok data export, send the file "
                                      "with the caption “inspo export”.")
            return
        url, note = context.args[0], " ".join(context.args[1:])
        res = await asyncio.to_thread(self.app.trends.add_link, url, note)
        await self._reply(update, format_inspo(res))

    # -- media & text --------------------------------------------------------------
    async def _download(self, message: Any) -> Path | None:
        att = None
        if message.photo:
            att, ext = message.photo[-1], ".jpg"
        elif message.video:
            att, ext = message.video, ".mp4"
        elif message.animation:
            att, ext = message.animation, ".mp4"
        elif message.document:
            att = message.document
            ext = Path(att.file_name or "").suffix.lower() or (".mp4" if (att.mime_type or "").startswith("video") else ".jpg")
        if att is None:
            return None
        inbox = self.app.settings.inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / f"{int(time.time())}-{att.file_unique_id}{ext}"
        tg_file = await att.get_file()
        await tg_file.download_to_drive(custom_path=dest)
        return dest

    async def on_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        msg = update.effective_message
        caption = (msg.caption or "").strip()
        try:
            path = await self._download(msg)
        except BadRequest as exc:
            if "too big" in str(exc).lower():
                await self._reply(update, "📦 That file is over Telegram's 20 MB bot download limit. Options: send it as a "
                                          "compressed video, use the HTTP upload shortcut, or run a local Bot API server "
                                          "(TELEGRAM_API_BASE) - see the README.")
                return
            raise
        except TelegramError as exc:
            await self._reply(update, f"❌ Couldn't download that file: {exc}")
            return
        if path is None:
            return
        low = caption.lower()
        if low.startswith(("/inspo", "inspo")):
            note = caption.split(None, 1)[1] if len(caption.split(None, 1)) > 1 else ""
            if note.lower().startswith("export") and path.suffix.lower() in {".json", ".txt", ".csv"}:
                res = await asyncio.to_thread(self.app.trends.import_export, path)
                await self._reply(update, f"💡 Found {res['found']} link(s); saved {res['saved']} new ({res['duplicates']} already saved).")
            else:
                try:
                    res = await asyncio.to_thread(self.app.trends.add_media, path, note)
                    await self._reply(update, format_inspo(res))
                except Exception as exc:
                    await self._reply(update, f"❌ Couldn't analyse that reference: {exc}")
            return
        if path.suffix.lower() in {".json", ".txt", ".csv"}:  # a data file, not media
            await self._reply(update, "That looks like a data file. To import a TikTok export, resend it with the caption “inspo export”.")
            return
        key = str(update.effective_chat.id)
        ack_handle = None
        if not self.collector.has_pending(key):  # first item of a burst: acknowledge at once; this message becomes the draft card
            ack = await msg.reply_text("📥 Got it - one moment…")
            ack_handle = str(ack.message_id)
        self.collector.add(key, path=path, text=caption, meta=ack_handle)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        msg = update.effective_message
        text = (msg.text or "").strip()
        key = str(update.effective_chat.id)
        if key in self.awaiting:  # the answer to "✏️ My caption" / "💬 Change…"
            did, action = self.awaiting.pop(key)
            fn = self.app.drafts.set_caption if action == "mycap" else self.app.drafts.rewrite
            await asyncio.to_thread(fn, did, text)
            return
        replied_to = getattr(msg, "reply_to_message", None)
        if replied_to is not None:  # replying to a draft card = "change it like this"
            draft = self.app.store.find_draft_by_card(str(replied_to.message_id))
            if draft:
                await asyncio.to_thread(self.app.drafts.instruct, draft["id"], text)
                return
        if self.collector.has_pending(key):  # a follow-up instruction for media that was just sent
            self.collector.add(key, text=text)
            return
        try:
            await self._reply(update, await asyncio.to_thread(self.app.pipeline.chat, text))
        except Exception as exc:
            await self._reply(update, f"❌ {exc}")

    async def _on_ready(self, key: str, paths: list[Path], instruction: str, ack_handle: Any = None) -> None:
        await asyncio.to_thread(self.app.drafts.create, list(paths), instruction, "telegram", ack_handle)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._authorized(update):
            if query:
                await query.answer()
            return
        cb = parse_callback(query.data)
        if cb is None:
            await query.answer()
            return
        chat_key = str(update.effective_chat.id)
        if cb.kind == "draft":
            await query.answer(TOASTS.get(cb.action.split(":")[0], ""))  # answer first: Telegram shows a spinner until we do
            res = await asyncio.to_thread(self.app.drafts.handle_action, cb.ref, cb.action)
            if res.prompt:
                self.awaiting[chat_key] = (cb.ref, cb.action)
                await update.effective_message.reply_text(res.prompt)
        elif cb.kind == "settings":
            await query.answer(await asyncio.to_thread(self.app.drafts.settings_action, cb.action))
            await asyncio.to_thread(self.notifier.present, self.app.drafts.settings_card(),
                                    handle=str(update.effective_message.message_id))
        else:
            await query.answer()
            fn = {"cancel": self.app.pipeline.cancel, "retry": self.app.pipeline.retry,
                  "posted": self.app.pipeline.confirm_posted}[cb.action]
            await asyncio.to_thread(fn, cb.ref)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass  # message already edited / too old


async def setup_commands(bot: Any) -> None:
    """Show the commands in Telegram's menu. Best effort."""
    try:
        await bot.set_my_commands([BotCommand(c, d) for c, d in COMMAND_MENU])
    except TelegramError as exc:
        log.info("Could not set the command menu: %s", exc)


def build_telegram(app: App, notifier: Any) -> tuple[Application, TelegramFrontend]:
    s = app.settings
    builder = ApplicationBuilder().token(s.telegram_token)
    if s.telegram_api_base:  # local Bot API server: no 20 MB download cap
        builder = builder.base_url(f"{s.telegram_api_base}/bot").base_file_url(f"{s.telegram_api_base}/file/bot").local_mode(True)
    application = builder.build()
    fe = TelegramFrontend(app, notifier)
    owner = filters.User(user_id=s.owner_telegram_id) if s.owner_telegram_id is not None else filters.ALL
    application.add_handler(CommandHandler("start", fe.cmd_start))  # open to all: only prints the id in setup mode
    for name, fn in [("help", fe.cmd_help), ("queue", fe.cmd_queue), ("drafts", fe.cmd_drafts), ("draft", fe.cmd_draft),
                     ("settings", fe.cmd_settings), ("trends", fe.cmd_trends), ("review", fe.cmd_review),
                     ("highlights", fe.cmd_highlights), ("highlights_done", fe.cmd_highlights_done),
                     ("cancel", fe.cmd_cancel), ("retry", fe.cmd_retry), ("status", fe.cmd_status), ("inspo", fe.cmd_inspo)]:
        application.add_handler(CommandHandler(name, fn, filters=owner))
    media_filter = (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.IMAGE | filters.Document.VIDEO
                    | filters.Document.MimeType("application/json") | filters.Document.MimeType("text/plain"))
    application.add_handler(MessageHandler(media_filter & owner, fe.on_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner, fe.on_text))
    application.add_handler(CallbackQueryHandler(fe.on_callback))
    return application, fe

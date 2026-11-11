"""Telegram front end: send a photo/video (caption = instruction) to your private bot.

* Only OWNER_TELEGRAM_ID is obeyed (checked by a filter AND again inside every handler).
* Albums and "photo, then a follow-up text" within COLLECT_WINDOW_SECONDS become ONE submission.
* Cloud Bot API limits downloads to 20 MB; for bigger videos set TELEGRAM_API_BASE (local Bot API server)
  or use the HTTP endpoint (see http_api.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from ..app import App

log = logging.getLogger(__name__)

TG_LIMIT = 4000
HELP = (
    "Send me photos or videos - put your instruction in the caption (or send a short text right after).\n\n"
    "Examples:\n• (photo) “story only, travel highlight”\n• (video) “post tonight at 8, funny tone”\n"
    "• (video) “TikTok only, trim the first 3 seconds”\n\n"
    "Commands:\n/queue - what's scheduled\n/trends - current trends (/trends refresh)\n/review - weekly feed review\n"
    "/highlights - Highlights checklist, /highlights_done [name]\n/cancel <id>  /approve <id>  /retry <id>\n"
    "/inspo <TikTok or Douyin link> [note] - save inspiration; or send a screenshot with the caption “inspo”\n"
    "/status - modes and connections\n\nAnything else you type, I'll answer as your account manager."
)


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


def parse_callback(data: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"(approve|cancel|retry):([0-9a-f]{6,16})", data or "")
    return (m.group(1), m.group(2)) if m else None


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
            self.task: asyncio.Task | None = None

    def __init__(self, window: float, on_ready: Callable[[str, list[Path], str], Awaitable[None]]):
        self.window, self.on_ready = window, on_ready
        self._batches: dict[str, Collector._Batch] = {}
        self._tasks: set[asyncio.Task] = set()

    def has_pending(self, key: str) -> bool:
        return key in self._batches

    def add(self, key: str, *, path: Path | None = None, text: str = "") -> None:
        batch = self._batches.setdefault(key, Collector._Batch())
        if path is not None:
            batch.paths.append(path)
        if text.strip():
            batch.texts.append(text.strip())
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
                await self.on_ready(key, batch.paths, " ".join(batch.texts))
            except Exception:
                log.exception("Collector handler failed")


class TelegramNotifier:
    """Sends the pipeline's messages to the owner. Safe to call from worker threads."""

    def __init__(self, owner_id: int | None):
        self.owner_id = owner_id
        self._bot: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._backlog: list[tuple[str, list[tuple[str, str]] | None]] = []

    def attach(self, bot: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._bot, self._loop = bot, loop
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


class TelegramFrontend:
    def __init__(self, app: App, notifier: TelegramNotifier):
        self.app, self.notifier = app, notifier
        self.owner_id = app.settings.owner_telegram_id
        self.collector = Collector(app.settings.collect_window_seconds, self._on_ready)

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
            await self._reply(update, f"Usage: /{verb} <id>  (ids are shown in brackets, e.g. [a1b2c3d4e5])")
            return
        await asyncio.to_thread(fn, context.args[0].strip("[]"))

    async def cmd_cancel(self, update, context):
        await self._id_command(update, context, self.app.pipeline.cancel, "cancel")

    async def cmd_approve(self, update, context):
        await self._id_command(update, context, self.app.pipeline.approve, "approve")

    async def cmd_retry(self, update, context):
        await self._id_command(update, context, self.app.pipeline.retry, "retry")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        s = self.app.settings
        lines = [f"Mode: {'🧪 DRY RUN (nothing is posted)' if s.dry_run else '🟢 LIVE'} · approval {'on' if s.require_approval else 'off'}"
                 f" · undo window {s.undo_window_seconds}s · model {s.claude_model}"]
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
        self.collector.add(str(update.effective_chat.id), path=path, text=caption)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = (update.effective_message.text or "").strip()
        key = str(update.effective_chat.id)
        if self.collector.has_pending(key):  # a follow-up instruction for media that was just sent
            self.collector.add(key, text=text)
            return
        try:
            await self._reply(update, await asyncio.to_thread(self.app.pipeline.chat, text))
        except Exception as exc:
            await self._reply(update, f"❌ {exc}")

    async def _on_ready(self, key: str, paths: list[Path], instruction: str) -> None:
        await asyncio.to_thread(self.app.pipeline.submit, list(paths), instruction, "telegram")

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._authorized(update):
            if query:
                await query.answer()
            return
        parsed = parse_callback(query.data)
        await query.answer()
        if not parsed:
            return
        action, pid = parsed
        fn = {"approve": self.app.pipeline.approve, "cancel": self.app.pipeline.cancel, "retry": self.app.pipeline.retry}[action]
        await asyncio.to_thread(fn, pid)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass  # message already edited / too old


def build_telegram(app: App, notifier: TelegramNotifier) -> tuple[Application, TelegramFrontend]:
    s = app.settings
    builder = ApplicationBuilder().token(s.telegram_token)
    if s.telegram_api_base:  # local Bot API server: no 20 MB download cap
        builder = builder.base_url(f"{s.telegram_api_base}/bot").base_file_url(f"{s.telegram_api_base}/file/bot").local_mode(True)
    application = builder.build()
    fe = TelegramFrontend(app, notifier)
    owner = filters.User(user_id=s.owner_telegram_id) if s.owner_telegram_id is not None else filters.ALL
    application.add_handler(CommandHandler("start", fe.cmd_start))  # open to all: only prints the id in setup mode
    for name, fn in [("help", fe.cmd_help), ("queue", fe.cmd_queue), ("trends", fe.cmd_trends), ("review", fe.cmd_review),
                     ("highlights", fe.cmd_highlights), ("highlights_done", fe.cmd_highlights_done),
                     ("cancel", fe.cmd_cancel), ("approve", fe.cmd_approve), ("retry", fe.cmd_retry),
                     ("status", fe.cmd_status), ("inspo", fe.cmd_inspo)]:
        application.add_handler(CommandHandler(name, fn, filters=owner))
    media_filter = (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.IMAGE | filters.Document.VIDEO
                    | filters.Document.MimeType("application/json") | filters.Document.MimeType("text/plain"))
    application.add_handler(MessageHandler(media_filter & owner, fe.on_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner, fe.on_text))
    application.add_handler(CallbackQueryHandler(fe.on_callback))
    return application, fe

"""`socialagent run`: HTTP server + Telegram bot + scheduler in one asyncio process."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from .app import App, build_app
from .channels.http_api import make_http_app
from .channels.telegram_bot import TelegramNotifier, build_telegram, setup_commands
from .config import Settings
from .pipeline import ListNotifier

log = logging.getLogger(__name__)


async def run_forever(settings: Settings) -> None:
    notifier = TelegramNotifier(settings.owner_telegram_id) if settings.telegram_token else ListNotifier()
    app: App = build_app(settings, notifier)
    if settings.dry_run:
        log.warning("DRY_RUN is on: nothing will be posted. Set DRY_RUN=false once `socialagent selfcheck` passes.")

    runner = web.AppRunner(make_http_app(app))
    await runner.setup()
    await web.TCPSite(runner, settings.http_host, settings.http_port).start()
    log.info("HTTP listening on %s:%s (public URL %s)", settings.http_host, settings.http_port, settings.public_base_url)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    tg = None
    if settings.telegram_token:
        tg, _ = build_telegram(app, notifier)  # type: ignore[arg-type]
        await tg.initialize()
        await tg.start()
        await tg.updater.start_polling()  # type: ignore[union-attr]
        notifier.attach(tg.bot, loop)  # type: ignore[union-attr]
        await setup_commands(tg.bot)  # type: ignore[union-attr]
        if settings.owner_telegram_id is None:
            log.warning("OWNER_TELEGRAM_ID is not set: message the bot /start to learn your id, then set it and restart.")
        else:
            notifier.notify("🟢 Social manager online" + (" (🧪 DRY RUN)" if settings.dry_run else "") + ". Send me a photo or video and I'll draft it.")
    else:
        log.warning("TELEGRAM_BOT_TOKEN not set: only the HTTP API is active.")

    sched = asyncio.create_task(app.scheduler.run())
    try:
        await stop.wait()
    finally:
        sched.cancel()
        if tg is not None:
            await tg.updater.stop()  # type: ignore[union-attr]
            await tg.stop()
            await tg.shutdown()
        await runner.cleanup()
        app.store.close()

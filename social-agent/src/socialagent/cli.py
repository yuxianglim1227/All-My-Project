"""Command line: run the agent, or drive it by hand (this is also how Claude Code manages it)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path
from typing import Sequence

import httpx
import yaml

from . import media as media_mod
from .app import App, build_app
from .config import Settings, load_dotenv
from .feed import highlight_checklist
from .llm import LLM, LLMError
from .publishers import PublishError
from .publishers import tiktok as tt
from .runtime import run_forever


class PrintNotifier:
    def notify(self, text: str, *, actions: list[tuple[str, str]] | None = None) -> None:
        print(text)
        if actions:
            print("   [" + " | ".join(f"{label} → {data}" for label, data in actions) + "]")
        print()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="socialagent", description="Creative Instagram + TikTok manager")
    p.add_argument("--env-file", default=".env", help="path to the .env file (default ./.env)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="start Telegram bot + HTTP server + scheduler")
    s = sub.add_parser("submit", help="plan/schedule media from files (like sending it to the bot)")
    s.add_argument("files", nargs="+")
    s.add_argument("-i", "--instruction", default="")
    sub.add_parser("queue", help="show scheduled posts")
    sub.add_parser("tick", help="publish anything that is due, run one scheduler tick")
    for name in ("cancel", "approve", "retry"):
        c = sub.add_parser(name, help=f"{name} a post by id")
        c.add_argument("post_id")
    t = sub.add_parser("trends", help="show current trends")
    t.add_argument("--refresh", action="store_true", help="research now (uses web search)")
    i = sub.add_parser("inspo", help="inspiration board (TikTok / Douyin references)")
    isub = i.add_subparsers(dest="inspo_cmd", required=True)
    il = isub.add_parser("link"); il.add_argument("url"); il.add_argument("-n", "--note", default="")
    ii = isub.add_parser("image", help="analyse a screenshot of a video you like"); ii.add_argument("file"); ii.add_argument("-n", "--note", default="")
    ie = isub.add_parser("export", help="import links from a TikTok data export"); ie.add_argument("file")
    h = sub.add_parser("highlights", help="Highlights checklist")
    h.add_argument("--done", nargs="?", const="", default=None, metavar="NAME", help="mark added (optionally one collection)")
    sub.add_parser("review", help="weekly feed review")
    c = sub.add_parser("selfcheck", help="verify setup with read-only checks")
    c.add_argument("--claude", action="store_true", help="also make one tiny live Claude call")
    a = sub.add_parser("auth", help="credential helpers")
    asub = a.add_subparsers(dest="auth_cmd", required=True)
    u = asub.add_parser("tiktok-url", help="print the TikTok authorisation URL"); u.add_argument("--redirect", required=True)
    x = asub.add_parser("tiktok-exchange", help="exchange the ?code= from the redirect for tokens")
    x.add_argument("--code", required=True); x.add_argument("--redirect", required=True)
    asub.add_parser("ig-refresh", help="refresh the Instagram long-lived token now")
    return p


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx/httpcore log every request URL at INFO, and Telegram / Instagram URLs carry the bot or access token.
    for noisy in ("httpx", "httpcore", "httpx2", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _setup_file_logging(settings: Settings) -> Path:
    """A background service has no console: keep a rotating log next to the database."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "agent.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    return path


def main(argv: Sequence[str] | None = None, *, llm: LLM | None = None, http: httpx.Client | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(args.env_file)
    _setup_logging(args.verbose)
    try:
        settings = Settings.from_env()
        if args.cmd == "run":
            _setup_file_logging(settings)
            asyncio.run(run_forever(settings))
            return 0
        app = build_app(settings, PrintNotifier(), llm=llm, http=http)
    except KeyboardInterrupt:
        return 0
    except (ValueError, yaml.YAMLError) as exc:  # bad .env / brand.yaml: say so plainly instead of a traceback
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        return _dispatch(args, app, settings, http or httpx.Client(timeout=30))
    except (LLMError, PublishError, media_mod.MediaError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    finally:
        app.store.close()


def _dispatch(args: argparse.Namespace, app: App, settings: Settings, http: httpx.Client) -> int:
    pipe = app.pipeline
    if args.cmd == "submit":
        files = [Path(f) for f in args.files]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            print(f"File(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        copies = []
        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        for f in files:  # work from copies so the originals are never touched
            dest = settings.inbox_dir / f"cli-{f.name}"
            shutil.copyfile(f, dest)
            copies.append(dest)
        return 0 if pipe.submit(copies, args.instruction, "cli") else 1
    if args.cmd == "queue":
        print(pipe.queue_text())
    elif args.cmd == "tick":
        print(app.scheduler.tick())
    elif args.cmd in ("cancel", "approve", "retry"):
        return 0 if getattr(pipe, args.cmd)(args.post_id) else 1
    elif args.cmd == "trends":
        if args.refresh:
            app.trends.refresh()
        print(app.trends.trends_for_prompt(12) or "No trends yet - run with --refresh.")
    elif args.cmd == "inspo":
        from .channels.telegram_bot import format_inspo
        if args.inspo_cmd == "link":
            print(format_inspo(app.trends.add_link(args.url, args.note)))
        elif args.inspo_cmd == "image":
            print(format_inspo(app.trends.add_media(args.file, args.note)))
        else:
            print(app.trends.import_export(args.file))
    elif args.cmd == "highlights":
        if args.done is not None:
            print(f"Marked {app.store.mark_highlights_done(args.done or None)} as added.")
        else:
            print(highlight_checklist(app.store, app.brand, pipe.zone) or "No Stories waiting to be added to Highlights.")
    elif args.cmd == "review":
        print(pipe.weekly_review())
    elif args.cmd == "selfcheck":
        return _selfcheck(app, settings, args.claude)
    elif args.cmd == "auth":
        return _auth(args, app, settings, http)
    return 0


def _selfcheck(app: App, settings: Settings, live_claude: bool) -> int:
    lines: list[str] = []
    bad = False

    def add(ok: bool | None, text: str) -> None:
        nonlocal bad
        lines.append(f"{'✅' if ok else ('⚠️ ' if ok is None else '❌')} {text}")
        bad = bad or ok is False

    add(media_mod.have_ffmpeg(), "ffmpeg + ffprobe installed" if media_mod.have_ffmpeg() else "ffmpeg/ffprobe missing (apt install ffmpeg)")
    add(bool(media_mod.find_font("hello", settings.font_path)) or None, "font for on-screen text found")
    add(True if media_mod.find_font("你好", settings.font_path) else None, "CJK font found (Chinese on-screen text)")
    add(bool(app.brand.pillars) or None, f"brand profile loaded ({app.brand.account_name}; {len(app.brand.pillars)} pillars, {len(app.brand.highlights)} highlights)")
    add(None if settings.dry_run else True, "DRY_RUN is ON - nothing will be posted" if settings.dry_run else "DRY_RUN is off - posting is LIVE")
    import os
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    add(has_key or None, "Anthropic credentials present" if has_key else "ANTHROPIC_API_KEY not set (the creative brain needs it)")
    add(bool(settings.telegram_token and settings.owner_telegram_id) or None,
        "Telegram bot + owner id configured" if settings.telegram_token and settings.owner_telegram_id
        else "Telegram not fully configured (TELEGRAM_BOT_TOKEN / OWNER_TELEGRAM_ID)")
    add(bool(settings.api_token) or None, "API_TOKEN set (HTTP upload endpoint enabled)" if settings.api_token else "API_TOKEN not set: HTTP upload endpoint disabled")
    local = any(h in settings.public_base_url for h in ("localhost", "127.0.0.1", "example.com"))
    add(None if local else True, f"PUBLIC_BASE_URL={settings.public_base_url}" + (" - Instagram cannot fetch media from this; use a public HTTPS URL" if local else ""))
    for pub in app.publishers.values():
        for line in pub.selfcheck():
            add(False if "FAILED" in line else (None if ("WARNING" in line or "dry-run" in line or "NOTE" in line) else True), line)
    if live_claude:
        try:
            add(True, f"Claude replied: {app.llm.text(system='Reply with the single word OK.', content='ping', max_tokens=50)[:40]!r}")
        except LLMError as exc:
            add(False, f"Claude call failed: {exc}")
    print("\n".join(lines))
    return 1 if bad else 0


def _auth(args: argparse.Namespace, app: App, settings: Settings, http: httpx.Client) -> int:
    if args.auth_cmd == "tiktok-url":
        if not settings.tiktok_client_key:
            print("Set TIKTOK_CLIENT_KEY first.", file=sys.stderr)
            return 1
        print(tt.authorize_url(settings.tiktok_client_key, args.redirect))
        print("\nOpen it, approve, then copy the `code` value from the address you are redirected to "
              "(the page may not load - that is fine) and run: socialagent auth tiktok-exchange --code <code> --redirect <same uri>")
    elif args.auth_cmd == "tiktok-exchange":
        data = tt.exchange_code(http, settings.tiktok_client_key, settings.tiktok_client_secret, args.code, args.redirect)
        app.tokens.set_tt_tokens(data["access_token"], data.get("refresh_token"))
        print(f"✅ TikTok tokens saved (scope: {data.get('scope', '?')}). The agent refreshes them automatically.")
    elif args.auth_cmd == "ig-refresh":
        from .maintenance import refresh_instagram
        print(refresh_instagram(settings, app.tokens, http))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

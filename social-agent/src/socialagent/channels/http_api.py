"""HTTP side of the agent.

  GET  /m/<token>/<name>   public, unguessable media URLs (Instagram downloads from here)
  GET  /healthz            liveness
  POST /api/submit         multipart upload (field "file" repeated, optional "instruction"); Bearer API_TOKEN
  GET  /api/queue          JSON list of upcoming posts; Bearer API_TOKEN

/api/submit has no size limit beyond your disk, which makes it the way to send big videos from an iOS
Shortcut / Android HTTP Shortcut / curl. Without API_TOKEN set, /api/* is disabled (503).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
import uuid
from pathlib import Path

from aiohttp import web

from ..app import App

log = logging.getLogger(__name__)

MAX_UPLOAD = 2 * 1024**3
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """Strip any path and unsafe characters: '../../x y.jpg' -> 'x-y.jpg'. Never returns an empty name."""
    base = Path(name.replace("\\", "/")).name
    base = _SAFE.sub("-", base).strip(".-")
    return base[-80:] or "upload"


def _authorized(request: web.Request, token: str) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied.strip().encode(), token.encode())


def make_http_app(app: App) -> web.Application:
    settings = app.settings
    web_app = web.Application(client_max_size=MAX_UPLOAD)
    background: set[asyncio.Task] = set()

    async def healthz(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def media(request: web.Request) -> web.StreamResponse:
        path = app.host.resolve(request.match_info["token"], request.match_info["name"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})

    def guard(request: web.Request) -> None:
        if not settings.api_token:
            raise web.HTTPServiceUnavailable(text="API_TOKEN is not configured on the server")
        if not _authorized(request, settings.api_token):
            raise web.HTTPUnauthorized(text="bad or missing bearer token")

    async def submit(request: web.Request) -> web.Response:
        guard(request)
        if not request.content_type.startswith("multipart/"):
            raise web.HTTPUnsupportedMediaType(text="send multipart/form-data with a 'file' field (and optional 'instruction')")
        reader = await request.multipart()
        instruction, saved = "", []
        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "instruction":
                instruction = (await part.text()).strip()
            elif part.name in {"file", "files", "media"} and part.filename:
                dest = settings.inbox_dir / f"{int(time.time())}-{uuid.uuid4().hex[:6]}-{safe_filename(part.filename)}"
                with dest.open("wb") as fh:
                    while chunk := await part.read_chunk(1 << 20):
                        fh.write(chunk)
                saved.append(dest)
        if not saved:
            raise web.HTTPBadRequest(text='send at least one file in the multipart field "file"')
        task = asyncio.create_task(asyncio.to_thread(app.drafts.create, list(saved), instruction, "http"))
        background.add(task)
        task.add_done_callback(background.discard)
        return web.json_response({"status": "accepted", "files": len(saved)}, status=202)

    async def queue(request: web.Request) -> web.Response:
        guard(request)
        rows = app.store.list_posts(["scheduled", "publishing", "handed_off"], limit=50)
        return web.json_response([{k: r[k] for k in ("id", "platform", "kind", "status", "publish_at", "caption")} for r in rows])

    web_app.router.add_get("/healthz", healthz)
    web_app.router.add_get("/m/{token}/{name}", media)
    web_app.router.add_post("/api/submit", submit)
    web_app.router.add_get("/api/queue", queue)
    if app.auth is not None and app.assets is not None and app.tasks is not None:
        from ..studio.web import setup_studio, studio_config
        setup_studio(web_app, auth=app.auth, assets=app.assets, tasks=app.tasks,
                     config=studio_config(app, lambda: app.gemini is not None),
                     secure_cookie=settings.public_base_url.startswith("https://"),
                     max_upload_mb=settings.studio_max_upload_mb, incoming=settings.assets_dir / "_incoming",
                     composer=app.composer)
    return web_app

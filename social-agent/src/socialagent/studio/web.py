"""Studio routes on the agent's aiohttp server (all under /studio; the existing /m, /healthz and /api routes are untouched).

Security model (single owner):
* every /studio/api route except login/me requires a session cookie (HttpOnly, SameSite=Lax, Secure on https);
* state-changing API calls must also carry the header ``X-Studio: 1``, which a cross-site page cannot add without a CORS
  preflight that this server never grants (CSRF defence on top of SameSite);
* strict Content-Security-Policy, no framing, no caching of API responses; login attempts are rate-limited per client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging

import uuid
from urllib.parse import unquote
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from ..channels.http_api import safe_filename
from .aitasks import PLATFORMS, AITasks, TaskError
from .aitasks import public_view as task_view
from .assets import AssetError, AssetLibrary
from .assets import public_view as asset_view
from .auth import COOKIE, Auth, AuthError

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
API = "/studio/api"
CSP = ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
OPEN_PATHS = {f"{API}/me", f"{API}/login"}
STUDIO_KEY = web.AppKey("studio", dict)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def client_key(request: web.Request) -> str:
    """The real client address. Behind a tunnel/proxy on this machine the peer is loopback, so trust its header."""
    peer = request.remote or "?"
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        fwd = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return fwd or peer
    return peer


def setup_studio(web_app: web.Application, *, auth: Auth, assets: AssetLibrary, tasks: AITasks, config: Callable[[], dict],
                 secure_cookie: bool, max_upload_mb: int, incoming: Path) -> None:
    state = {"auth": auth, "assets": assets, "tasks": tasks}
    web_app[STUDIO_KEY] = state
    incoming.mkdir(parents=True, exist_ok=True)
    max_bytes = max_upload_mb * 1024 * 1024

    async def gate(request: web.Request, handler: Handler) -> web.StreamResponse:
        path = request.path
        if path.startswith(API) or path == "/studio/share":
            if path not in OPEN_PATHS and not auth.session_valid(request.cookies.get(COOKIE)):
                if path == "/studio/share":
                    raise web.HTTPSeeOther("/studio/#login")
                return _json({"error": "Please log in."}, status=401)
            if path.startswith(API) and request.method not in ("GET", "HEAD") and request.headers.get("X-Studio") != "1":
                return _json({"error": "Missing X-Studio header."}, status=403)
        return await handler(request)

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        path = request.path
        if not path.startswith("/studio"):
            return await handler(request)
        try:
            resp = await gate(request, handler)
        except web.HTTPException as exc:
            resp = exc
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith(API):
            resp.headers["Cache-Control"] = "no-store"
        if isinstance(resp, web.HTTPException):
            raise resp
        return resp

    web_app.middlewares.append(guard)
    r = web_app.router

    # ---- pages / PWA -----------------------------------------------------------------------
    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def root(request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/studio/")

    async def sw(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "sw.js", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/studio/"})

    async def manifest(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "manifest.webmanifest",
                                headers={"Content-Type": "application/manifest+json", "Cache-Control": "no-cache"})

    r.add_get("/", root)
    r.add_get("/studio", root)
    r.add_get("/studio/", index)
    r.add_get("/studio/sw.js", sw)
    r.add_get("/studio/manifest.webmanifest", manifest)
    r.add_static("/studio/static/", STATIC, show_index=False, append_version=False)

    # ---- auth -----------------------------------------------------------------------------------
    async def me(request: web.Request) -> web.StreamResponse:
        return _json({"authenticated": auth.session_valid(request.cookies.get(COOKIE)), "configured": auth.configured,
                      "totp": auth.totp_enabled})

    async def login(request: web.Request) -> web.StreamResponse:
        body = await _body(request)
        try:
            token = await asyncio.to_thread(auth.login, str(body.get("password", "")), str(body.get("totp", "")),
                                            client=client_key(request), user_agent=request.headers.get("User-Agent", ""))
        except AuthError as exc:
            return _json({"error": str(exc)}, status=401)
        resp = _json({"ok": True})
        resp.set_cookie(COOKIE, token, max_age=auth.session_days * 86400, httponly=True, samesite="Lax",
                        secure=secure_cookie, path="/studio")
        return resp

    async def logout(request: web.Request) -> web.StreamResponse:
        auth.logout(request.cookies.get(COOKIE))
        resp = _json({"ok": True})
        resp.del_cookie(COOKIE, path="/studio")
        return resp

    r.add_get(f"{API}/me", me)
    r.add_post(f"{API}/login", login)
    r.add_post(f"{API}/logout", logout)

    async def cfg(request: web.Request) -> web.StreamResponse:
        return _json(config())

    r.add_get(f"{API}/config", cfg)

    # ---- media library -----------------------------------------------------------------------------
    async def list_assets(request: web.Request) -> web.StreamResponse:
        q = request.query
        items = assets.list(folder=q.get("folder", ""), tag=q.get("tag", ""), kind=q.get("kind", ""), q=q.get("q", ""),
                            limit=_int(q.get("limit"), 60), offset=_int(q.get("offset"), 0))
        return _json({"items": [asset_view(a) for a in items], "folders": assets.folders()})

    async def _receive(request: web.Request, source: str) -> tuple[list[dict[str, Any]], list[str]]:
        reader = await request.multipart()
        saved, errors, folder, tags = [], [], "", ""
        tmp_files: list[tuple[Path, str]] = []
        try:
            while (part := await reader.next()) is not None:
                if part.name in ("folder", "tags") and not part.filename:
                    value = (await part.text())[:200]
                    folder, tags = (value, tags) if part.name == "folder" else (folder, value)
                    continue
                if not part.filename:
                    await part.release()
                    continue
                name = safe_filename(unquote(part.filename))  # some clients percent-encode the name
                tmp = incoming / f"{uuid.uuid4().hex}-{name}"
                size = 0
                with tmp.open("wb") as fh:
                    while chunk := await part.read_chunk(1 << 20):
                        size += len(chunk)
                        if size > max_bytes:
                            break
                        fh.write(chunk)
                if size > max_bytes:
                    tmp.unlink(missing_ok=True)
                    errors.append(f"{name}: larger than {max_upload_mb} MB.")
                    await part.release()
                    continue
                tmp_files.append((tmp, name))
            for tmp, name in tmp_files:
                try:
                    a = await asyncio.to_thread(assets.add_file, tmp, name, source=source, folder=folder or "All media", tags=tags)
                    saved.append(asset_view(a))
                except AssetError as exc:
                    errors.append(str(exc))
        finally:
            for tmp, _ in tmp_files:
                tmp.unlink(missing_ok=True)
        return saved, errors

    async def upload(request: web.Request) -> web.StreamResponse:
        if not request.content_type.startswith("multipart/"):
            return _json({"error": "Send the files as multipart/form-data."}, status=415)
        saved, errors = await _receive(request, "upload")
        return _json({"saved": saved, "errors": errors}, status=200 if saved or not errors else 400)

    async def share(request: web.Request) -> web.StreamResponse:
        """Android 'Share to Social Studio' (Web Share Target). iOS Safari does not support share targets."""
        if request.content_type.startswith("multipart/"):
            await _receive(request, "share")
        raise web.HTTPSeeOther("/studio/#library")

    async def patch_asset(request: web.Request) -> web.StreamResponse:
        body = await _body(request)
        try:
            a = assets.update(request.match_info["aid"], name=body.get("name"), folder=body.get("folder"),
                              tags=body.get("tags"), note=body.get("note"))
        except AssetError as exc:
            return _json({"error": str(exc)}, status=404)
        return _json(asset_view(a))

    async def delete_asset(request: web.Request) -> web.StreamResponse:
        ok = await asyncio.to_thread(assets.delete, request.match_info["aid"])
        return _json({"ok": ok}, status=200 if ok else 404)

    async def asset_file(request: web.Request) -> web.StreamResponse:
        variant = "thumb" if request.match_info["variant"] == "thumb" else "original"
        p = assets.file_for(request.match_info["aid"], variant)
        if p is None:
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})

    r.add_get(f"{API}/assets", list_assets)
    r.add_post(f"{API}/assets", upload)
    r.add_patch(f"{API}/assets/{{aid}}", patch_asset)
    r.add_delete(f"{API}/assets/{{aid}}", delete_asset)
    r.add_get(f"{API}/assets/{{aid}}/{{variant:thumb|file}}", asset_file)
    r.add_post("/studio/share", share)

    # ---- AI tasks ------------------------------------------------------------------------------------
    async def list_tasks(request: web.Request) -> web.StreamResponse:
        return _json({"items": [task_view(t) for t in tasks.list()]})

    async def create_task(request: web.Request) -> web.StreamResponse:
        body = await _body(request)
        try:
            t = tasks.create(str(body.get("kind", "")), dict(body.get("inputs") or {}))
        except (TaskError, ValueError) as exc:
            return _json({"error": str(exc)}, status=400)
        return _json(task_view(t) | {"bridge_text": tasks.bridge_text(t)}, status=201)

    async def get_task(request: web.Request) -> web.StreamResponse:
        t = tasks.get(request.match_info["tid"])
        if t is None:
            raise web.HTTPNotFound()
        return _json(task_view(t) | {"bridge_text": tasks.bridge_text(t)})

    async def reply_task(request: web.Request) -> web.StreamResponse:
        body = await _body(request)
        try:
            t = tasks.submit_reply(request.match_info["tid"], str(body.get("reply", "")))
        except TaskError as exc:
            return _json({"error": str(exc)}, status=422)
        return _json(task_view(t))

    async def run_task(request: web.Request) -> web.StreamResponse:
        try:
            t = await asyncio.to_thread(tasks.run_with, request.match_info["tid"])
        except TaskError as exc:
            return _json({"error": str(exc)}, status=422)
        return _json(task_view(t))

    async def delete_task(request: web.Request) -> web.StreamResponse:
        ok = tasks.delete(request.match_info["tid"])
        return _json({"ok": ok}, status=200 if ok else 404)

    r.add_get(f"{API}/tasks", list_tasks)
    r.add_post(f"{API}/tasks", create_task)
    r.add_get(f"{API}/tasks/{{tid}}", get_task)
    r.add_post(f"{API}/tasks/{{tid}}/reply", reply_task)
    r.add_post(f"{API}/tasks/{{tid}}/run", run_task)
    r.add_delete(f"{API}/tasks/{{tid}}", delete_task)


# ---- helpers ------------------------------------------------------------------------------------------
def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def studio_config(app: Any, gemini_ready: Callable[[], bool]) -> Callable[[], dict]:
    """What the UI needs to draw its pickers."""
    def build() -> dict:
        from ..styles import StyleCatalog
        cat = StyleCatalog.from_brand(app.brand.custom_tones)
        return {"account": app.brand.account_name,
                "tones": [{"key": t.key, "label": t.label, "emoji": t.emoji} for t in cat.tones],
                "languages": [{"key": x.key, "label": x.label} for x in cat.languages],
                "platforms": [{"key": k, "label": v} for k, v in PLATFORMS.items()],
                "defaults": {"tone": app.brand.default_tone, "language": app.brand.default_language},
                "ai": {"gemini": gemini_ready(), "model": app.settings.gemini_model if gemini_ready() else ""},
                "dry_run": app.settings.dry_run}
    return build




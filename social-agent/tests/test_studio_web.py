"""Studio HTTP routes: auth gate, CSRF header, uploads, share target, AI tasks, headers, app wiring."""

import json

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import needs_ffmpeg
from helpers import make_app
from socialagent.channels.http_api import make_http_app
from socialagent.gemini import GeminiLLM
from socialagent.llm import NoAutoLLM

PASSWORD = "a long enough sentence"


async def start(app):
    server = TestServer(make_http_app(app))
    await server.start_server()
    # The test server is http://127.0.0.1: let the jar keep IP-host cookies and treat it as a secure origin (Secure cookie).
    jar = aiohttp.CookieJar(unsafe=True, treat_as_secure_origin=[str(server.make_url("/"))])
    client = TestClient(server, cookie_jar=jar)
    await client.start_server()
    return client
H = {"X-Studio": "1"}


@pytest.fixture()
async def rig(tmp_path):
    app, notifier, llm = make_app(tmp_path)
    app.auth.set_password(PASSWORD)
    client = await start(app)
    yield client, app
    await client.close()


async def login(client):
    r = await client.post("/studio/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status == 200
    return r


def upload_form(*files, folder=None):
    fd = aiohttp.FormData()
    for name, data in files:
        fd.add_field("file", data, filename=name, content_type="application/octet-stream")
    if folder:
        fd.add_field("folder", folder)
    return fd


# ---------------------------------------------------------------- pages + headers
async def test_root_redirects_and_shell_is_public_with_strict_headers(rig):
    client, _ = rig
    r = await client.get("/", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/studio/"
    r = await client.get("/studio/")
    assert r.status == 200 and "Social Studio" in await r.text()
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"
    for path in ("/studio/static/app.js", "/studio/static/styles.css", "/studio/sw.js", "/studio/manifest.webmanifest",
                 "/studio/static/icon-512.png"):
        assert (await client.get(path)).status == 200, path
    manifest = json.loads(await (await client.get("/studio/manifest.webmanifest")).text())
    assert manifest["share_target"]["action"] == "/studio/share" and manifest["scope"] == "/studio/"


async def test_existing_public_routes_still_work(rig):
    client, _ = rig
    assert (await client.get("/healthz")).status == 200
    assert (await client.get("/api/queue")).status == 401                  # bearer-token API untouched


# ---------------------------------------------------------------- auth gate
async def test_api_requires_login_and_login_sets_a_safe_cookie(rig):
    client, _ = rig
    r = await client.get("/studio/api/assets")
    assert r.status == 401 and r.headers["Cache-Control"] == "no-store"
    me = await (await client.get("/studio/api/me")).json()
    assert me == {"authenticated": False, "configured": True, "totp": False}

    bad = await client.post("/studio/api/login", json={"password": "nope"}, headers=H)
    assert bad.status == 401 and "Wrong password" in (await bad.json())["error"]
    r = await login(client)
    cookie = r.headers["Set-Cookie"]
    assert "studio_session=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Secure" in cookie and "Path=/studio" in cookie                 # PUBLIC_BASE_URL is https in the rig
    assert (await client.get("/studio/api/assets")).status == 200
    assert (await (await client.get("/studio/api/me")).json())["authenticated"] is True

    assert (await client.post("/studio/api/logout", headers=H)).status == 200
    assert (await client.get("/studio/api/assets")).status == 401


async def test_state_changing_calls_need_the_csrf_header(rig):
    client, _ = rig
    await login(client)
    r = await client.post("/studio/api/tasks", json={"kind": "ideas", "inputs": {}})
    assert r.status == 403 and "X-Studio" in (await r.json())["error"]
    assert (await client.post("/studio/api/tasks", json={"kind": "ideas", "inputs": {}}, headers=H)).status == 201


async def test_brute_force_is_limited_using_the_tunnel_client_address(rig):
    client, _ = rig
    for _ in range(5):
        await client.post("/studio/api/login", json={"password": "x"}, headers={**H, "CF-Connecting-IP": "203.0.113.9"})
    r = await client.post("/studio/api/login", json={"password": PASSWORD}, headers={**H, "CF-Connecting-IP": "203.0.113.9"})
    assert r.status == 401 and "Too many" in (await r.json())["error"]
    ok = await client.post("/studio/api/login", json={"password": PASSWORD}, headers={**H, "CF-Connecting-IP": "198.51.100.1"})
    assert ok.status == 200


# ---------------------------------------------------------------- media library over HTTP
@needs_ffmpeg
async def test_upload_list_serve_edit_delete(rig, media_dir):
    client, app = rig
    await login(client)
    r = await client.post("/studio/api/assets", data=upload_form(("../../evil name.jpg", (media_dir / "photo.jpg").read_bytes()),
                                                                 ("notes.txt", b"hello"), folder="Cafe"), headers=H)
    body = await r.json()
    assert r.status == 200 and len(body["saved"]) == 1 and "not a photo or video" in body["errors"][0]
    a = body["saved"][0]
    assert a["name"] == "evil-name.jpg" and a["folder"] == "Cafe" and "path" not in a     # no server paths leak
    assert not list((app.settings.assets_dir / "_incoming").iterdir())                   # temp files cleaned up

    listing = await (await client.get("/studio/api/assets?folder=Cafe")).json()
    assert [x["id"] for x in listing["items"]] == [a["id"]]
    thumb = await client.get(a["thumb_url"])
    assert thumb.status == 200 and thumb.headers["Content-Type"] == "image/jpeg"
    assert (await client.get(a["file_url"])).status == 200

    r = await client.patch(f"/studio/api/assets/{a['id']}", json={"tags": "latte, art", "note": "oat latte"}, headers=H)
    assert (await r.json())["tags"] == ["latte", "art"]
    assert (await client.delete(f"/studio/api/assets/{a['id']}", headers=H)).status == 200
    assert (await client.get(a["thumb_url"])).status == 404

    await client.post("/studio/api/logout", headers=H)
    assert (await client.get(a["file_url"])).status == 401                             # media is private


async def test_upload_needs_multipart_and_respects_the_size_cap(tmp_path):
    app, *_ = make_app(tmp_path, env={"STUDIO_MAX_UPLOAD_MB": "1"})
    app.auth.set_password(PASSWORD)
    client = await start(app)
    try:
        await login(client)
        assert (await client.post("/studio/api/assets", json={}, headers=H)).status == 415
        r = await client.post("/studio/api/assets", data=upload_form(("big.mp4", b"0" * (1024 * 1024 + 10))), headers=H)
        assert r.status == 400 and "larger than 1 MB" in (await r.json())["errors"][0]
    finally:
        await client.close()


@needs_ffmpeg
async def test_android_share_target_saves_and_redirects(rig, media_dir):
    client, app = rig
    r = await client.post("/studio/share", data=upload_form(("s.jpg", (media_dir / "photo.jpg").read_bytes())), allow_redirects=False)
    assert r.status == 303 and r.headers["Location"] == "/studio/#login"                # not logged in: nothing saved
    assert app.assets.list() == []
    await login(client)
    r = await client.post("/studio/share", data=upload_form(("s.jpg", (media_dir / "photo.jpg").read_bytes())), allow_redirects=False)
    assert r.status == 303 and r.headers["Location"] == "/studio/#library"
    assert [a["source"] for a in app.assets.list()] == ["share"]


# ---------------------------------------------------------------- AI tasks over HTTP
async def test_ideas_task_round_trip_by_copy_paste(rig):
    client, _ = rig
    await login(client)
    cfg = await (await client.get("/studio/api/config")).json()
    assert cfg["ai"]["gemini"] is False and any(t["key"] == "funny" for t in cfg["tones"])
    r = await client.post("/studio/api/tasks", json={"kind": "ideas", "inputs": {"count": 2}}, headers=H)
    t = await r.json()
    assert r.status == 201 and "REPLY FORMAT" in t["bridge_text"] and t["status"] == "waiting"

    r = await client.post(f"/studio/api/tasks/{t['id']}/reply", json={"reply": "nope"}, headers=H)
    assert r.status == 422 and "valid JSON" in (await r.json())["error"]
    reply = {"ideas": [{"title": "t", "platform": "tiktok", "format": "short", "hook": "h", "caption": "c", "shot_list": ["s"],
                        "why": "w"}]}
    r = await client.post(f"/studio/api/tasks/{t['id']}/reply", json={"reply": json.dumps(reply)}, headers=H)
    assert r.status == 200 and (await r.json())["status"] == "done"
    r = await client.post(f"/studio/api/tasks/{t['id']}/run", headers=H)
    assert r.status == 422                                                              # already answered
    items = (await (await client.get("/studio/api/tasks")).json())["items"]
    assert [i["id"] for i in items] == [t["id"]] and "system" not in items[0]
    assert (await client.delete(f"/studio/api/tasks/{t['id']}", headers=H)).status == 200


async def test_run_without_gemini_explains_copy_paste(rig):
    client, _ = rig
    await login(client)
    t = await (await client.post("/studio/api/tasks", json={"kind": "ideas", "inputs": {}}, headers=H)).json()
    r = await client.post(f"/studio/api/tasks/{t['id']}/run", headers=H)
    assert r.status == 422 and "copy-paste" in (await r.json())["error"]


# ---------------------------------------------------------------- composition
def test_provider_selection(tmp_path):
    from socialagent.app import build_app
    from socialagent.config import Settings
    from socialagent.pipeline import ListNotifier
    from helpers import ROOT
    import shutil
    shutil.copyfile(ROOT / "config" / "brand.yaml", tmp_path / "b.yaml")
    base = {"DATA_DIR": str(tmp_path / "d"), "BRAND_FILE": str(tmp_path / "b.yaml"), "TIMEZONE": "UTC"}
    app = build_app(Settings.from_env(base), ListNotifier())
    assert isinstance(app.llm, NoAutoLLM) and app.gemini is None
    app.store.close()
    app = build_app(Settings.from_env({**base, "GEMINI_API_KEY": "k", "GEMINI_MODEL": "gemini-x"}), ListNotifier())
    assert isinstance(app.llm, GeminiLLM) and app.gemini is app.llm and app.gemini.model == "gemini-x"
    app.store.close()
    with pytest.raises(ValueError, match="AI_PROVIDER"):
        Settings.from_env({**base, "AI_PROVIDER": "openai"})


# ---------------------------------------------------------------- CLI
def test_studio_password_and_totp_commands(tmp_path, capsys):
    import argparse
    from socialagent.cli import _studio_login
    from socialagent.studio.auth import totp_code
    app, *_ = make_app(tmp_path)
    ns = lambda **kw: argparse.Namespace(**{"cmd": "studio-password", "revoke": False, "if_missing": False, **kw})
    answers = iter(["short", "short", "a long enough sentence", "typo", "a long enough sentence", "a long enough sentence"])
    assert _studio_login(ns(), app, secret=lambda prompt: next(answers)) == 0
    out = capsys.readouterr().out
    assert "at least" in out and "don't match" in out and "Password set" in out and "a long enough" not in out
    assert app.auth.configured
    assert _studio_login(ns(if_missing=True), app, secret=lambda p: (_ for _ in ()).throw(AssertionError("asked"))) == 0

    tok = app.auth.login("a long enough sentence")
    assert _studio_login(ns(revoke=True), app) == 0 and not app.auth.session_valid(tok)

    printed = {}

    def ask(prompt):
        key = capsys.readouterr().out.split("setup key: ")[1].split()[0]
        printed["key"] = key
        import time
        return totp_code(key, int(time.time() // 30))
    assert _studio_login(argparse.Namespace(cmd="studio-totp", action="on"), app, ask=ask) == 0
    assert app.auth.totp_enabled and printed["key"]
    assert _studio_login(argparse.Namespace(cmd="studio-totp", action="off"), app) == 0 and not app.auth.totp_enabled

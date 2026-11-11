import asyncio
import socket

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from helpers import make_app
from socialagent.channels.http_api import make_http_app, safe_filename
from socialagent.config import Settings
from socialagent.runtime import run_forever
from socialagent.timeutil import utcnow

AUTH = {"Authorization": "Bearer s3cret"}


@pytest.fixture()
async def rig(tmp_path):
    app, notifier, llm = make_app(tmp_path)
    submitted = []
    app.pipeline.submit = lambda paths, instruction="", source="cli": submitted.append((list(paths), instruction, source))
    client = TestClient(TestServer(make_http_app(app)))
    await client.start_server()
    yield client, app, submitted
    await client.close()


def form(*files, instruction=None, field="file"):
    fd = aiohttp.FormData()
    for name, data in files:
        fd.add_field(field, data, filename=name, content_type="application/octet-stream")
    if instruction is not None:
        fd.add_field("instruction", instruction)
    return fd


# ---------------------------------------------------------------- helpers
@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"), ("C:\\Users\\me\\IMG 001.HEIC", "IMG-001.HEIC"), ("my video (final).mp4", "my-video-final-.mp4"),
    ("", "upload"), ("...", "upload"), ("日本語.jpg", "jpg"), (".hidden", "hidden"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected
    assert "/" not in safe_filename(raw) and "\\" not in safe_filename(raw) and not safe_filename(raw).startswith(".")


# ---------------------------------------------------------------- public routes
async def test_healthz(rig):
    client, *_ = rig
    r = await client.get("/healthz")
    assert r.status == 200 and await r.text() == "ok"


async def test_media_route_serves_published_files_only(rig, tmp_path):
    client, app, _ = rig
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"\xff\xd8jpegbytes")
    url = app.host.publish(f)
    path = "/m/" + url.split("/m/")[1]
    r = await client.get(path)
    assert r.status == 200 and await r.read() == b"\xff\xd8jpegbytes"
    assert r.headers["Content-Type"] == "image/jpeg" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert (await client.head(path)).status == 200                       # Instagram may probe with HEAD
    token = path.split("/")[2]
    for bad in (f"/m/{token}/nope.jpg", "/m/short/pic.jpg", f"/m/{token}/..%2F..%2Fagent.db", "/m/%2e%2e/%2e%2e/etc/passwd"):
        assert (await client.get(bad)).status == 404, bad


# ---------------------------------------------------------------- /api/submit
async def test_submit_requires_a_valid_bearer_token(rig):
    client, _, submitted = rig
    assert (await client.post("/api/submit", data=form(("a.jpg", b"x")))).status == 401
    assert (await client.post("/api/submit", data=form(("a.jpg", b"x")), headers={"Authorization": "Bearer nope"})).status == 401
    assert (await client.post("/api/submit", data=form(("a.jpg", b"x")), headers={"Authorization": "Basic s3cret"})).status == 401
    assert (await client.post("/api/submit", data=form(("a.jpg", b"x")), headers={"Authorization": "Bearer"})).status == 401
    await asyncio.sleep(0.1)
    assert submitted == []                                               # nothing leaked through


async def test_api_is_disabled_when_no_token_is_configured(tmp_path):
    app, _, _ = make_app(tmp_path, env={"API_TOKEN": ""})
    client = TestClient(TestServer(make_http_app(app)))
    await client.start_server()
    try:
        assert (await client.post("/api/submit", data=form(("a.jpg", b"x")), headers=AUTH)).status == 503
        assert (await client.post("/api/submit", data=form(("a.jpg", b"x")), headers={"Authorization": "Bearer "})).status == 503
        assert (await client.get("/api/queue", headers=AUTH)).status == 503
    finally:
        await client.close()


async def test_submit_saves_files_safely_and_hands_them_to_the_pipeline(rig):
    client, app, submitted = rig
    r = await client.post("/api/submit", headers=AUTH, data=form(("../../evil.jpg", b"one"), ("clip 2.mp4", b"two"),
                                                                 instruction="story only, travel"))
    assert r.status == 202 and await r.json() == {"status": "accepted", "files": 2}
    await asyncio.sleep(0.15)
    (paths, instruction, source), = submitted
    assert instruction == "story only, travel" and source == "http" and len(paths) == 2
    inbox = app.settings.inbox_dir.resolve()
    for p in paths:
        assert p.resolve().parent == inbox and p.is_file()               # traversal in the filename cannot escape the inbox
    assert sorted(p.read_bytes() for p in paths) == [b"one", b"two"]
    assert sorted(p.suffix for p in paths) == [".jpg", ".mp4"]


async def test_submit_streams_large_uploads_intact(rig):
    client, app, submitted = rig
    blob = bytes(range(256)) * (5 * 1024 * 4)                            # 5 MiB
    r = await client.post("/api/submit", headers=AUTH, data=form(("big.mp4", blob)))
    assert r.status == 202
    await asyncio.sleep(0.2)
    assert submitted[0][0][0].read_bytes() == blob and submitted[0][1] == ""


async def test_submit_without_files_or_with_wrong_field_is_400(rig):
    client, _, submitted = rig
    fd = aiohttp.FormData(default_to_multipart=True)
    fd.add_field("instruction", "hello")
    assert (await client.post("/api/submit", headers=AUTH, data=fd)).status == 400            # multipart, but no file
    assert (await client.post("/api/submit", headers=AUTH, data=form(("a.jpg", b"x"), field="attachment"))).status == 400
    # not multipart at all (urlencoded / JSON): a clear client error, never a 500
    assert (await client.post("/api/submit", headers=AUTH, data={"instruction": "hello"})).status == 415
    assert (await client.post("/api/submit", headers=AUTH, json={"instruction": "hello"})).status == 415
    await asyncio.sleep(0.1)
    assert submitted == []


# ---------------------------------------------------------------- /api/queue
async def test_queue_lists_upcoming_posts_for_authorised_callers(rig):
    client, app, _ = rig
    sid = app.store.add_submission(source="t", instruction="", media=[])
    app.store.add_post(submission_id=sid, platform="instagram", kind="reel", caption="hello", hashtags=[], media=[],
                       publish_at=utcnow(), status="scheduled")
    app.store.add_post(submission_id=sid, platform="tiktok", kind="video", caption="done", hashtags=[], media=[],
                       publish_at=utcnow(), status="published")
    assert (await client.get("/api/queue")).status == 401
    r = await client.get("/api/queue", headers=AUTH)
    rows = await r.json()
    assert [(x["platform"], x["caption"]) for x in rows] == [("instagram", "hello")]


# ---------------------------------------------------------------- runtime smoke test
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_runtime_starts_serves_and_shuts_down_cleanly(tmp_path, monkeypatch):
    from helpers import ROOT
    import shutil
    port = free_port()
    brand = tmp_path / "brand.yaml"
    shutil.copyfile(ROOT / "config" / "brand.yaml", brand)
    settings = Settings.from_env({"DATA_DIR": str(tmp_path / "d"), "BRAND_FILE": str(brand), "HTTP_HOST": "127.0.0.1",
                                  "HTTP_PORT": str(port), "API_TOKEN": "s3cret", "ANTHROPIC_API_KEY": "unused"})
    # keep the scheduler from calling the real Claude API during the smoke test
    monkeypatch.setattr("socialagent.trends.TrendResearcher.refresh", lambda self, today=None: [])
    task = asyncio.create_task(run_forever(settings))
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(50):
                try:
                    async with http.get(f"http://127.0.0.1:{port}/healthz") as r:
                        assert r.status == 200 and await r.text() == "ok"
                        break
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("server never came up")
            async with http.get(f"http://127.0.0.1:{port}/api/queue", headers=AUTH) as r:
                assert r.status == 200 and await r.json() == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

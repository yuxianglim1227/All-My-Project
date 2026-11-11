import json
from urllib.parse import parse_qsl

import httpx
import pytest

from socialagent.publishers import PreparedMedia, PublishError, PublishRequest
from socialagent.publishers import tiktok as tt
from socialagent.publishers.dryrun import DryRunPublisher
from socialagent.publishers.instagram import InstagramPublisher, refresh_long_lived_token
from socialagent.publishers.tiktok import TikTokPublisher, plan_chunks


def req(kind, media, caption="Hello #x", platform="instagram"):
    return PublishRequest(post_id="p1", platform=platform, kind=kind, caption=caption, media=media)


IMG = lambda n=1: PreparedMedia(path=f"/m/{n}.jpg", kind="image", url=f"https://pub.example/{n}.jpg")   # noqa: E731
VID = lambda n=1: PreparedMedia(path=f"/m/{n}.mp4", kind="video", url=f"https://pub.example/{n}.mp4", duration=12)  # noqa: E731


# =============================================================== Instagram
class IGServer:
    """Pretends to be the Instagram Graph API and records every request."""

    def __init__(self):
        self.log: list[tuple[str, str, dict]] = []
        self.container_n = 0
        self.statuses: dict[str, list[str]] = {}
        self.overrides: dict[tuple[str, str], httpx.Response | Exception] = {}
        self.quota = {"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.content.decode())) if request.method == "POST" else dict(request.url.params)
        path = request.url.path
        self.log.append((request.method, path, params))
        key = (request.method, path.rsplit("/", 1)[-1])
        if key in self.overrides:
            res = self.overrides[key]
            if isinstance(res, Exception):
                raise res
            return res
        if request.method == "POST" and path.endswith("/media"):
            self.container_n += 1
            return httpx.Response(200, json={"id": f"c{self.container_n}"})
        if request.method == "POST" and path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "m1"})
        if path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=self.quota)
        if path.endswith("/me"):
            return httpx.Response(200, json={"username": "tester", "account_type": "BUSINESS"})
        name = path.rsplit("/", 1)[-1]
        if name == "m1":
            return httpx.Response(200, json={"permalink": "https://instagram.com/p/abc"})
        if name.startswith("c"):
            queue = self.statuses.get(name, [])
            return httpx.Response(200, json={"status_code": queue.pop(0) if queue else "FINISHED"})
        return httpx.Response(404, json={"error": {"message": "unknown route", "code": 100}})

    def posts(self, suffix):
        return [p for m, path, p in self.log if m == "POST" and path.endswith(suffix)]


def ig(server, **kw):
    kw.setdefault("poll_timeout", 5)
    return InstagramPublisher(user_id="U1", token=lambda: "TOK", graph_base="https://graph.example.com",
                              http=httpx.Client(transport=httpx.MockTransport(server)), sleep=lambda s: None, **kw)


def test_ig_photo_flow_and_params():
    s = IGServer()
    res = ig(s).publish(req("photo", [IMG()], "Nice view #sunset"))
    assert (res.remote_id, res.permalink) == ("m1", "https://instagram.com/p/abc")
    created = s.posts("/U1/media")[0]
    assert created == {"image_url": "https://pub.example/1.jpg", "caption": "Nice view #sunset", "access_token": "TOK"}
    assert s.posts("/media_publish")[0]["creation_id"] == "c1"
    order = [(m, p.rsplit("/", 1)[-1]) for m, p, _ in s.log]
    assert order.index(("POST", "media")) < order.index(("GET", "c1")) < order.index(("POST", "media_publish"))


def test_ig_versioned_base_url():
    s = IGServer()
    ig(s, api_version="v99.0").publish(req("photo", [IMG()]))
    assert all(p.startswith("/v99.0/") for _, p, _ in s.log)


def test_ig_reel_polls_until_finished():
    s = IGServer()
    s.statuses["c1"] = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]
    ig(s).publish(req("reel", [VID()], "Reel time"))
    created = s.posts("/U1/media")[0]
    assert (created["media_type"], created["video_url"], created["share_to_feed"]) == ("REELS", "https://pub.example/1.mp4", "true")
    assert sum(1 for m, p, _ in s.log if m == "GET" and p.endswith("/c1")) == 3
    assert len(s.posts("/media_publish")) == 1                       # published only after FINISHED


def test_ig_story_uses_correct_url_key_and_no_caption():
    s = IGServer()
    ig(s).publish(req("story", [IMG()], "ignored"))
    ig(s).publish(req("story", [VID()], "ignored"))
    a, b = s.posts("/U1/media")
    assert a["media_type"] == "STORIES" and "image_url" in a and "caption" not in a and "video_url" not in a
    assert b["media_type"] == "STORIES" and "video_url" in b and "image_url" not in b


def test_ig_carousel_creates_children_then_parent():
    s = IGServer()
    ig(s).publish(req("carousel", [IMG(1), VID(2), IMG(3)], "Swipe"))
    c1, c2, c3, parent = s.posts("/U1/media")
    assert c1["is_carousel_item"] == "true" and c1["image_url"].endswith("1.jpg") and "media_type" not in c1
    assert c2["media_type"] == "VIDEO" and c2["video_url"].endswith("2.mp4")
    assert parent["media_type"] == "CAROUSEL" and parent["children"] == "c1,c2,c3" and parent["caption"] == "Swipe"
    assert s.posts("/media_publish")[0]["creation_id"] == "c4"


def test_ig_container_error_is_final_with_detail():
    s = IGServer()
    s.statuses["c1"] = ["ERROR"]
    with pytest.raises(PublishError, match="could not process") as e:
        ig(s).publish(req("reel", [VID()]))
    assert e.value.retryable is False and not s.posts("/media_publish")


def test_ig_processing_timeout_is_retryable_and_never_publishes():
    s = IGServer()
    s.statuses["c1"] = ["IN_PROGRESS"] * 50
    with pytest.raises(PublishError, match="still processing") as e:
        ig(s, poll_timeout=0).publish(req("reel", [VID()]))
    assert e.value.retryable is True and not s.posts("/media_publish")


@pytest.mark.parametrize("status,body,retryable,needle", [
    (400, {"error": {"message": "Invalid parameter", "code": 100}}, False, "Invalid parameter"),
    (400, {"error": {"message": "Expired", "code": 190}}, False, "access token"),
    (429, {"error": {"message": "Too many calls", "code": 4}}, True, "Too many calls"),
    (500, {"error": {"message": "Oops", "code": 2, "is_transient": True}}, True, "Oops"),
])
def test_ig_api_errors_classified(status, body, retryable, needle):
    s = IGServer()
    s.overrides[("POST", "media")] = httpx.Response(status, json=body)
    with pytest.raises(PublishError, match=needle) as e:
        ig(s).publish(req("photo", [IMG()]))
    assert e.value.retryable is retryable


def test_ig_quota_exhausted_blocks_before_creating_anything():
    s = IGServer()
    s.quota = {"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]}
    with pytest.raises(PublishError, match="24-hour") as e:
        ig(s).publish(req("photo", [IMG()]))
    assert e.value.retryable is True and not s.posts("/U1/media")


def test_ig_quota_endpoint_failure_does_not_block_posting():
    s = IGServer()
    s.overrides[("GET", "content_publishing_limit")] = httpx.Response(400, json={"error": {"message": "nope", "code": 100}})
    assert ig(s).publish(req("photo", [IMG()])).remote_id == "m1"


def test_ig_ambiguous_failure_during_publish_is_never_auto_retried():
    s = IGServer()
    s.overrides[("POST", "media_publish")] = httpx.ReadTimeout("timed out")
    with pytest.raises(PublishError, match="may or may not") as e:
        ig(s).publish(req("photo", [IMG()]))
    assert e.value.retryable is False
    s2 = IGServer()
    s2.overrides[("POST", "media_publish")] = httpx.Response(503, json={"error": {"message": "down", "code": 2}})
    with pytest.raises(PublishError, match="may not have gone live") as e2:
        ig(s2).publish(req("photo", [IMG()]))
    assert e2.value.retryable is False


def test_ig_network_error_before_publish_is_retryable():
    s = IGServer()
    s.overrides[("POST", "media")] = httpx.ConnectError("dns")
    with pytest.raises(PublishError) as e:
        ig(s).publish(req("photo", [IMG()]))
    assert e.value.retryable is True


def test_ig_refuses_missing_url_and_unknown_kind():
    s = IGServer()
    with pytest.raises(PublishError, match="no public URL"):
        ig(s).publish(req("photo", [PreparedMedia(path="/x.jpg", kind="image")]))
    with pytest.raises(PublishError, match="can't publish"):
        ig(s).publish(req("video", [VID()]))


def test_ig_selfcheck_and_token_refresh():
    s = IGServer()
    lines = ig(s).selfcheck()
    assert "@tester (BUSINESS)" in lines[0] and "3/100" in lines[1]
    s.overrides[("GET", "me")] = httpx.Response(401, json={"error": {"message": "bad", "code": 190}})
    assert "FAILED" in ig(s).selfcheck()[0]

    def handler(request):
        assert request.url.params["grant_type"] == "ig_refresh_token" and request.url.params["access_token"] == "OLD"
        return httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert refresh_long_lived_token(http, "OLD")["access_token"] == "NEW"
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"message": "expired"}})))
    with pytest.raises(PublishError, match="expired"):
        refresh_long_lived_token(bad, "OLD")


# =============================================================== TikTok
class TTServer:
    def __init__(self, privacy=("SELF_ONLY",), statuses=("PUBLISH_COMPLETE",)):
        self.log: list[dict] = []
        self.privacy = list(privacy)
        self.statuses = list(statuses)
        self.overrides: dict[str, list[httpx.Response]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        entry = {"method": request.method, "path": path, "headers": dict(request.headers),
                 "json": json.loads(request.content) if request.method == "POST" and request.content and "json" in request.headers.get("content-type", "") else None,
                 "form": dict(parse_qsl(request.content.decode())) if "x-www-form-urlencoded" in request.headers.get("content-type", "") else None,
                 "size": len(request.content) if request.method == "PUT" else None}
        self.log.append(entry)
        if self.overrides.get(path):
            return self.overrides[path].pop(0)
        ok = {"code": "ok", "message": "", "log_id": "x"}
        if path.endswith("/creator_info/query/"):
            return httpx.Response(200, json={"data": {"creator_username": "me", "privacy_level_options": self.privacy,
                                                      "max_video_post_duration_sec": 600}, "error": ok})
        if path.endswith("/video/init/") or path.endswith("/inbox/video/init/"):
            return httpx.Response(200, json={"data": {"publish_id": "v_pub1", "upload_url": "https://upload.example/u1"}, "error": ok})
        if path.endswith("/content/init/"):
            return httpx.Response(200, json={"data": {"publish_id": "p_pub1"}, "error": ok})
        if path == "/u1":
            return httpx.Response(201)
        if path.endswith("/status/fetch/"):
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            data = {"status": st}
            if st == "FAILED":
                data["fail_reason"] = "file_format_check_failed"
            if st == "PUBLISH_COMPLETE":
                data["publicaly_available_post_id"] = [7777]
            return httpx.Response(200, json={"data": data, "error": ok})
        if path.endswith("/oauth/token/"):
            return httpx.Response(200, json={"access_token": "newA", "refresh_token": "newR", "expires_in": 86400})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}})

    def where(self, suffix):
        return [e for e in self.log if e["path"].endswith(suffix)]


@pytest.fixture()
def video_file(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 100)
    return p


def tk(server, **kw):
    kw.setdefault("access_token", lambda: "ACC")
    return TikTokPublisher(http=httpx.Client(transport=httpx.MockTransport(server)), sleep=lambda s: None,
                           poll_timeout=5, **kw)


def vreq(path, duration=12.0, caption="My video #fyp"):
    return req("video", [PreparedMedia(path=str(path), kind="video", duration=duration)], caption, "tiktok")


def test_plan_chunks_single_and_multi(monkeypatch):
    assert plan_chunks(1000) == (1000, 1, [(0, 999)])
    monkeypatch.setattr(tt, "SINGLE_CHUNK_MAX", 10)
    monkeypatch.setattr(tt, "CHUNK", 8)
    size, count, ranges = plan_chunks(25)
    assert (size, count, ranges) == (8, 3, [(0, 7), (8, 15), (16, 24)])      # last chunk absorbs the remainder
    assert sum(e - s + 1 for s, e in ranges) == 25
    with pytest.raises(PublishError):
        plan_chunks(0)


def test_tiktok_direct_video_flow(video_file):
    s = TTServer(privacy=["SELF_ONLY"], statuses=["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"])
    res = tk(s, privacy="SELF_ONLY").publish(vreq(video_file))
    assert res.remote_id == "7777"
    init = s.where("/video/init/")[0]
    assert init["headers"]["authorization"] == "Bearer ACC"
    assert init["json"]["post_info"]["title"] == "My video #fyp"
    assert init["json"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert init["json"]["source_info"] == {"source": "FILE_UPLOAD", "video_size": 100, "chunk_size": 100, "total_chunk_count": 1}
    put = s.where("/u1")[0]
    assert put["headers"]["content-range"] == "bytes 0-99/100" and put["size"] == 100
    assert put["headers"]["content-type"] == "video/mp4"
    assert len(s.where("/status/fetch/")) == 2


def test_tiktok_multichunk_upload(video_file, monkeypatch):
    monkeypatch.setattr(tt, "SINGLE_CHUNK_MAX", 30)
    monkeypatch.setattr(tt, "CHUNK", 40 // 2 + 5)        # 25 -> 100 // 25 = 4 chunks
    s = TTServer()
    tk(s).publish(vreq(video_file))
    puts = s.where("/u1")
    assert [p["headers"]["content-range"] for p in puts] == ["bytes 0-24/100", "bytes 25-49/100", "bytes 50-74/100", "bytes 75-99/100"]
    assert s.where("/video/init/")[0]["json"]["source_info"]["total_chunk_count"] == 4


def test_tiktok_privacy_falls_back_to_allowed_option(video_file):
    s = TTServer(privacy=["SELF_ONLY"])
    res = tk(s, privacy="PUBLIC_TO_EVERYONE").publish(vreq(video_file))
    assert s.where("/video/init/")[0]["json"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert "isn't allowed" in res.note
    s2 = TTServer(privacy=["PUBLIC_TO_EVERYONE", "SELF_ONLY"])
    tk(s2, privacy="PUBLIC_TO_EVERYONE").publish(vreq(video_file))
    assert s2.where("/video/init/")[0]["json"]["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"


def test_tiktok_inbox_mode_uses_inbox_endpoint_and_explains_next_step(video_file):
    s = TTServer(statuses=["SEND_TO_USER_INBOX"])
    res = tk(s, mode="inbox").publish(vreq(video_file))
    assert not s.where("/creator_info/query/") and not s.where("/publish/video/init/")
    init = s.where("/inbox/video/init/")[0]
    assert "post_info" not in init["json"] and init["json"]["source_info"]["source"] == "FILE_UPLOAD"
    assert "tap Publish" in res.note and res.remote_id == "v_pub1"


def test_tiktok_unaudited_error_is_explained(video_file):
    s = TTServer()
    s.overrides["/v2/post/publish/video/init/"] = [httpx.Response(403, json={"error": {
        "code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}})]
    with pytest.raises(PublishError, match="audit") as e:
        tk(s).publish(vreq(video_file))
    assert e.value.retryable is False and not s.where("/u1")


def test_tiktok_refreshes_token_once_then_succeeds(video_file):
    tokens = {"a": "OLD"}
    s = TTServer()
    s.overrides["/v2/post/publish/creator_info/query/"] = [
        httpx.Response(401, json={"error": {"code": "access_token_invalid", "message": "expired"}})]

    def refresh():
        tokens["a"] = "NEW"
        return "NEW"

    res = tk(s, access_token=lambda: tokens["a"], refresh=refresh).publish(vreq(video_file))
    infos = s.where("/creator_info/query/")
    assert [i["headers"]["authorization"] for i in infos] == ["Bearer OLD", "Bearer NEW"] and res.remote_id == "7777"


def test_tiktok_persistent_invalid_token_does_not_loop(video_file):
    s = TTServer()
    bad = httpx.Response(401, json={"error": {"code": "access_token_invalid", "message": "expired"}})
    s.overrides["/v2/post/publish/creator_info/query/"] = [bad, httpx.Response(401, json={"error": {"code": "access_token_invalid", "message": "expired"}})]
    with pytest.raises(PublishError, match="invalid or expired"):
        tk(s, refresh=lambda: "STILL-BAD").publish(vreq(video_file))
    assert len(s.where("/creator_info/query/")) == 2


def test_tiktok_failed_status_and_too_long_video(video_file):
    s = TTServer(statuses=["FAILED"])
    with pytest.raises(PublishError, match="file_format_check_failed") as e:
        tk(s).publish(vreq(video_file))
    assert e.value.retryable is False
    s2 = TTServer()
    with pytest.raises(PublishError, match="allows 600s"):
        tk(s2).publish(vreq(video_file, duration=900))
    assert not s2.where("/video/init/")                       # rejected before uploading anything


def test_tiktok_status_timeout_after_upload_is_not_a_failure_and_not_retried(video_file):
    s = TTServer(statuses=["PROCESSING_UPLOAD"])
    pub = TikTokPublisher(access_token=lambda: "ACC", http=httpx.Client(transport=httpx.MockTransport(s)),
                          sleep=lambda x: None, poll_timeout=0)
    res = pub.publish(vreq(video_file))
    assert "still processing" in res.note and res.remote_id == "v_pub1"


def test_tiktok_upload_http_failure(video_file):
    s = TTServer()
    s.overrides["/u1"] = [httpx.Response(500)]
    with pytest.raises(PublishError, match="rejected the upload") as e:
        tk(s).publish(vreq(video_file))
    assert e.value.retryable is True


def test_tiktok_photo_post_direct_and_inbox():
    photos = [PreparedMedia(path=f"/p{i}.jpg", kind="image", url=f"https://pub.example/p{i}.jpg") for i in range(3)]
    s = TTServer(statuses=["PUBLISH_COMPLETE"])
    res = tk(s).publish(req("photo", photos, "First line\nSecond line #tag", "tiktok"))
    body = s.where("/content/init/")[0]["json"]
    assert body["post_mode"] == "DIRECT_POST" and body["media_type"] == "PHOTO"
    assert body["source_info"] == {"source": "PULL_FROM_URL", "photo_cover_index": 0,
                                   "photo_images": [p.url for p in photos]}
    assert body["post_info"]["title"] == "First line" and body["post_info"]["description"].endswith("#tag")
    assert body["post_info"]["privacy_level"] == "SELF_ONLY" and res.remote_id == "7777"

    s2 = TTServer(statuses=["SEND_TO_USER_INBOX"])
    tk(s2, mode="inbox").publish(req("photo", photos, "c", "tiktok"))
    b2 = s2.where("/content/init/")[0]["json"]
    assert b2["post_mode"] == "MEDIA_UPLOAD" and "privacy_level" not in b2["post_info"]


def test_tiktok_photo_title_is_capped_at_90():
    s = TTServer()
    tk(s).publish(req("photo", [PreparedMedia(path="/p.jpg", kind="image", url="https://pub.example/p.jpg")], "x" * 300, "tiktok"))
    assert len(s.where("/content/init/")[0]["json"]["post_info"]["title"]) <= 90


def test_tiktok_needs_urls_only_for_photos_and_rejects_other_kinds(video_file):
    pub = tk(TTServer())
    assert pub.needs_urls("photo") and not pub.needs_urls("video")
    with pytest.raises(PublishError, match="can't publish"):
        pub.publish(req("reel", [], "c", "tiktok"))
    with pytest.raises(PublishError, match="public URLs"):
        pub.publish(req("photo", [PreparedMedia(path="/p.jpg", kind="image")], "c", "tiktok"))


def test_tiktok_selfcheck_flags_unaudited():
    lines = tk(TTServer(privacy=["SELF_ONLY"])).selfcheck()
    assert any("@me" in l for l in lines) and any("not passed TikTok's audit" in l for l in lines)
    assert any("inbox mode" in l for l in tk(TTServer(), mode="inbox").selfcheck())


def test_tiktok_oauth_helpers():
    url = tt.authorize_url("KEY", "https://example.com/cb")
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?") and "client_key=KEY" in url
    assert "video.publish" in url and "redirect_uri=https%3A%2F%2Fexample.com%2Fcb" in url
    s = TTServer()
    http = httpx.Client(transport=httpx.MockTransport(s))
    out = tt.refresh_access_token(http, "K", "S", "RT")
    assert out["access_token"] == "newA"
    assert s.log[-1]["form"] == {"client_key": "K", "client_secret": "S", "grant_type": "refresh_token", "refresh_token": "RT"}
    tt.exchange_code(http, "K", "S", "CODE", "https://example.com/cb")
    assert s.log[-1]["form"]["grant_type"] == "authorization_code" and s.log[-1]["form"]["code"] == "CODE"
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad code"})))
    with pytest.raises(PublishError, match="bad code"):
        tt.exchange_code(bad, "K", "S", "x", "https://example.com/cb")


# =============================================================== Dry run
def test_dry_run_records_and_never_needs_urls():
    d = DryRunPublisher("instagram")
    res = d.publish(req("photo", [IMG()]))
    assert res.remote_id == "dryrun-p1" and "nothing was posted" in res.note
    assert d.needs_urls("photo") is False and len(d.sent) == 1 and "dry-run" in d.selfcheck()[0]

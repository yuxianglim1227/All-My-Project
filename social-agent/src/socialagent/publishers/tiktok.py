"""TikTok publishing via the official Content Posting API.

Two modes (TIKTOK_MODE):
  direct - the API publishes for you.  NOTE: until TikTok audits your app, *every* direct post is forced to
           private (SELF_ONLY) and TikTok rejects public ones. Apply for the audit in the developer portal.
  inbox  - the video/photo is uploaded as a DRAFT to your TikTok inbox; you tap Publish in the app.

Videos are sent with FILE_UPLOAD (no public URL needed). Photo posts use PULL_FROM_URL, which requires the
URL's domain/prefix to be verified in your TikTok developer portal.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from ..textutil import TIKTOK_PHOTO_DESC_MAX, TIKTOK_PHOTO_TITLE_MAX, TIKTOK_TITLE_MAX, truncate
from .base import PublishError, PublishRequest, PublishResult

log = logging.getLogger(__name__)

API = "https://open.tiktokapis.com"
MB = 1024 * 1024
SINGLE_CHUNK_MAX = 64 * MB
CHUNK = 32 * MB
DEFAULT_MAX_SECONDS = 180

FRIENDLY = {
    "unaudited_client_can_only_post_to_private_accounts":
        "TikTok only lets apps that have not passed its audit post privately. Either finish TikTok's Content "
        "Posting API audit, set TIKTOK_MODE=inbox (uploads a draft you publish in the app), or keep "
        "TIKTOK_PRIVACY=SELF_ONLY and set the account to private.",
    "url_ownership_unverified":
        "TikTok can't pull the photo because the domain of PUBLIC_BASE_URL isn't verified in your TikTok "
        "developer portal (URL properties). Verify it, or post photos in inbox mode after verifying.",
    "access_token_invalid": "The TikTok access token is invalid or expired.",
    "scope_not_authorized": "The TikTok token lacks the required scope (video.publish for direct posts, "
                            "video.upload for inbox drafts). Re-authorize with that scope.",
}
RETRYABLE = {"rate_limit_exceeded", "internal_error", "service_unavailable"}


def plan_chunks(size: int) -> tuple[int, int, list[tuple[int, int]]]:
    """(chunk_size, total_chunk_count, inclusive byte ranges). The last chunk absorbs any remainder."""
    if size <= 0:
        raise PublishError("The video file is empty.", retryable=False)
    if size <= SINGLE_CHUNK_MAX:
        return size, 1, [(0, size - 1)]
    count = size // CHUNK
    ranges = [(i * CHUNK, (i + 1) * CHUNK - 1) for i in range(count - 1)]
    ranges.append(((count - 1) * CHUNK, size - 1))
    return CHUNK, count, ranges


class TikTokPublisher:
    platform = "tiktok"

    def __init__(self, *, access_token: Callable[[], str], refresh: Callable[[], str] | None = None,
                 mode: str = "direct", privacy: str = "SELF_ONLY", http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, poll_interval: float = 5.0, poll_timeout: float = 300.0):
        self._access = access_token
        self._refresh = refresh
        self.mode = mode
        self.privacy = privacy
        self.http = http or httpx.Client(timeout=120)
        self._sleep = sleep
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def needs_urls(self, kind: str) -> bool:
        return kind == "photo"

    # -- HTTP ----------------------------------------------------------------------
    def _api(self, path: str, body: dict[str, Any] | None = None, *, _retried: bool = False) -> dict[str, Any]:
        try:
            resp = self.http.post(f"{API}{path}", json=body or {}, headers={
                "Authorization": f"Bearer {self._access()}", "Content-Type": "application/json; charset=UTF-8"})
        except httpx.HTTPError as exc:
            raise PublishError(f"Network error talking to TikTok: {exc}", retryable=True) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        err = data.get("error") or {}
        code = err.get("code", "ok" if resp.status_code < 400 else f"http_{resp.status_code}")
        if code == "access_token_invalid" and self._refresh and not _retried:
            log.info("TikTok token rejected; refreshing and retrying once")
            self._refresh()
            return self._api(path, body, _retried=True)
        if code != "ok":
            msg = FRIENDLY.get(code) or err.get("message") or f"HTTP {resp.status_code}"
            raise PublishError(f"TikTok error ({code}): {msg}", retryable=code in RETRYABLE or resp.status_code >= 500)
        return data.get("data") or {}

    # -- helpers -------------------------------------------------------------------
    def creator_info(self) -> dict[str, Any]:
        return self._api("/v2/post/publish/creator_info/query/")

    def _pick_privacy(self, info: dict[str, Any]) -> tuple[str, str]:
        options = info.get("privacy_level_options") or []
        if not options or self.privacy in options:
            return self.privacy, ""
        fallback = "SELF_ONLY" if "SELF_ONLY" in options else options[-1]
        return fallback, (f"Requested privacy {self.privacy} isn't allowed for this account right now "
                          f"(allowed: {', '.join(options)}); used {fallback}.")

    def _wait_status(self, publish_id: str) -> tuple[str, dict[str, Any]]:
        deadline = time.monotonic() + self.poll_timeout
        terminal = {"SEND_TO_USER_INBOX"} if self.mode == "inbox" else {"PUBLISH_COMPLETE"}
        while True:
            data = self._api("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            status = str(data.get("status", ""))
            if status in terminal or (status == "PUBLISH_COMPLETE"):
                return status, data
            if status == "FAILED":
                raise PublishError(f"TikTok failed to publish: {data.get('fail_reason', 'no reason given')}.",
                                   retryable=False)
            if time.monotonic() > deadline:
                return status or "PROCESSING", data
            self._sleep(self.poll_interval)

    def _finish(self, publish_id: str, note: str) -> PublishResult:
        # After a successful upload/init a timeout is NOT retried (it could create a duplicate post).
        try:
            status, data = self._wait_status(publish_id)
        except PublishError as exc:
            if exc.retryable:
                raise PublishError(f"{exc} The upload went through, so I won't retry automatically; check TikTok.",
                                   retryable=False) from exc
            raise
        ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
        if status == "SEND_TO_USER_INBOX":
            note = (note + " Draft is waiting in your TikTok inbox - open TikTok and tap Publish.").strip()
        elif status != "PUBLISH_COMPLETE":
            note = (note + f" TikTok is still processing (status {status}); it should appear shortly.").strip()
        return PublishResult(remote_id=str(ids[0]) if ids else publish_id, permalink="", note=note)

    # -- main entry ----------------------------------------------------------------
    def publish(self, req: PublishRequest) -> PublishResult:
        info = self.creator_info() if self.mode == "direct" else {}
        privacy, note = self._pick_privacy(info) if self.mode == "direct" else ("", "")
        if req.kind == "video":
            return self._publish_video(req, info, privacy, note)
        if req.kind == "photo":
            return self._publish_photo(req, privacy, note)
        raise PublishError(f"TikTok can't publish a {req.kind!r}.", retryable=False)

    def _publish_video(self, req: PublishRequest, info: dict[str, Any], privacy: str, note: str) -> PublishResult:
        media = req.media[0]
        limit = info.get("max_video_post_duration_sec") or DEFAULT_MAX_SECONDS
        if media.duration and media.duration > float(limit):
            raise PublishError(f"The video is {media.duration:.0f}s but TikTok allows {limit}s for this account.",
                               retryable=False)
        size = os.path.getsize(media.path)
        chunk_size, count, ranges = plan_chunks(size)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}
        if self.mode == "inbox":
            init = self._api("/v2/post/publish/inbox/video/init/", {"source_info": source})
        else:
            init = self._api("/v2/post/publish/video/init/", {
                "post_info": {"title": truncate(req.caption, TIKTOK_TITLE_MAX), "privacy_level": privacy,
                              "disable_duet": False, "disable_comment": False, "disable_stitch": False},
                "source_info": source})
        publish_id, upload_url = init.get("publish_id"), init.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError("TikTok did not return an upload URL.", retryable=True)
        self._upload(upload_url, media.path, size, ranges)
        return self._finish(str(publish_id), note)

    def _upload(self, url: str, path: str, size: int, ranges: list[tuple[int, int]]) -> None:
        with open(path, "rb") as fh:
            for start, end in ranges:
                fh.seek(start)
                data = fh.read(end - start + 1)
                try:
                    resp = self.http.put(url, content=data, timeout=600, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                        "Content-Range": f"bytes {start}-{end}/{size}"})
                except httpx.HTTPError as exc:
                    raise PublishError(f"Upload to TikTok failed: {exc}", retryable=True) from exc
                if resp.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok rejected the upload chunk (HTTP {resp.status_code}).",
                                       retryable=resp.status_code >= 500)

    def _publish_photo(self, req: PublishRequest, privacy: str, note: str) -> PublishResult:
        urls = [m.url for m in req.media]
        if not all(urls):
            raise PublishError("Internal error: TikTok photo posts need public URLs.", retryable=False)
        first_line = req.caption.strip().splitlines()[0] if req.caption.strip() else ""
        post_info: dict[str, Any] = {"title": truncate(first_line, TIKTOK_PHOTO_TITLE_MAX),
                                     "description": truncate(req.caption, TIKTOK_PHOTO_DESC_MAX)}
        if self.mode == "direct":
            post_info.update({"privacy_level": privacy, "disable_comment": False, "auto_add_music": True})
        init = self._api("/v2/post/publish/content/init/", {
            "post_info": post_info,
            "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": urls},
            "post_mode": "DIRECT_POST" if self.mode == "direct" else "MEDIA_UPLOAD",
            "media_type": "PHOTO"})
        publish_id = init.get("publish_id")
        if not publish_id:
            raise PublishError("TikTok did not return a publish id.", retryable=True)
        return self._finish(str(publish_id), note)

    # -- ops -----------------------------------------------------------------------
    def selfcheck(self) -> list[str]:
        lines = [f"tiktok: mode={self.mode}, requested privacy={self.privacy}"]
        if self.mode != "direct":
            lines.append("tiktok: inbox mode - drafts are uploaded and you publish them in the TikTok app")
            return lines
        try:
            info = self.creator_info()
        except PublishError as exc:
            lines.append(f"tiktok: FAILED {exc}")
            return lines
        opts = info.get("privacy_level_options") or []
        lines.append(f"tiktok: connected as @{info.get('creator_username', '?')}; allowed privacy: {', '.join(opts) or '?'}")
        if opts and opts == ["SELF_ONLY"]:
            lines.append("tiktok: NOTE only private posting is available - the app has not passed TikTok's audit yet")
        return lines


# -- OAuth helpers (used by `socialagent auth tiktok-*`) -------------------------------
def authorize_url(client_key: str, redirect_uri: str, state: str = "socialagent",
                  scopes: str = "user.info.basic,video.publish,video.upload") -> str:
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": client_key, "scope": scopes, "response_type": "code", "redirect_uri": redirect_uri, "state": state})


def _token_request(http: httpx.Client, form: dict[str, str]) -> dict[str, Any]:
    resp = http.post(f"{API}/v2/oauth/token/", data=form,
                     headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or "access_token" not in body:
        raise PublishError(f"TikTok token request failed: {body.get('error_description') or body.get('error') or resp.status_code}",
                           retryable=False)
    return body


def exchange_code(http: httpx.Client, client_key: str, client_secret: str, code: str, redirect_uri: str) -> dict[str, Any]:
    return _token_request(http, {"client_key": client_key, "client_secret": client_secret, "code": code,
                                 "grant_type": "authorization_code", "redirect_uri": redirect_uri})


def refresh_access_token(http: httpx.Client, client_key: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    return _token_request(http, {"client_key": client_key, "client_secret": client_secret,
                                 "grant_type": "refresh_token", "refresh_token": refresh_token})

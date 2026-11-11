"""Instagram publishing via the official Instagram Graph API (container -> poll -> publish).

Flow per post:  POST /{ig-user-id}/media  ->  GET /{container-id}?fields=status_code  ->
                POST /{ig-user-id}/media_publish
Media is fetched by Instagram from a PUBLIC URL (see hosting.MediaHost).

NOT possible through the official API (so deliberately not attempted): creating Highlights, adding
trending audio, tagging products. The agent covers Highlights with a checklist instead.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from .base import PreparedMedia, PublishError, PublishRequest, PublishResult

log = logging.getLogger(__name__)

# Meta error codes that are temporary (rate limits / transient). Anything else is treated as final.
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}
TOKEN_CODES = {102, 190}


class InstagramPublisher:
    platform = "instagram"

    def __init__(self, *, user_id: str, token: Callable[[], str], graph_base: str = "https://graph.instagram.com",
                 api_version: str = "", http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 poll_interval: float = 4.0, poll_timeout: float = 360.0):
        self.user_id = user_id
        self._token = token
        self.base = f"{graph_base.rstrip('/')}/{api_version.strip('/')}" if api_version else graph_base.rstrip("/")
        self.http = http or httpx.Client(timeout=60)
        self._sleep = sleep
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def needs_urls(self, kind: str) -> bool:
        return True

    # -- HTTP ----------------------------------------------------------------------
    def _call(self, method: str, path: str, params: dict[str, Any] | None = None, *, safe_to_retry: bool = True) -> dict[str, Any]:
        """One Graph API call. ``safe_to_retry=False`` marks calls whose outcome may be ambiguous on failure."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["access_token"] = self._token()
        url = f"{self.base}/{path.lstrip('/')}"
        try:
            if method == "GET":
                resp = self.http.get(url, params=params)
            else:
                resp = self.http.post(url, data=params)
        except httpx.HTTPError as exc:
            if safe_to_retry:
                raise PublishError(f"Network error talking to Instagram: {exc}", retryable=True) from exc
            raise PublishError("The connection dropped while publishing - the post may or may not have gone live. "
                               "Check Instagram before re-sending.", retryable=False) from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or "error" in body:
            raise self._error(resp.status_code, body, safe_to_retry)
        return body

    @staticmethod
    def _error(status: int, body: dict[str, Any], safe_to_retry: bool) -> PublishError:
        err = body.get("error") or {}
        code, msg = err.get("code"), err.get("message") or f"HTTP {status}"
        if code in TOKEN_CODES:
            return PublishError("Instagram rejected the access token (expired or revoked). "
                                "Generate a new token and update IG_ACCESS_TOKEN.", retryable=False)
        transient = bool(err.get("is_transient")) or code in TRANSIENT_CODES or status >= 500
        if transient and not safe_to_retry:
            return PublishError(f"Instagram error while publishing ({msg}) - the post may not have gone live; "
                                "check Instagram before re-sending.", retryable=False)
        return PublishError(f"Instagram error {code or status}: {msg}", retryable=transient)

    # -- building blocks -----------------------------------------------------------
    def _container(self, params: dict[str, Any]) -> str:
        return str(self._call("POST", f"{self.user_id}/media", params)["id"])

    def _wait(self, container_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            info = self._call("GET", container_id, {"fields": "status_code,status"})
            status = str(info.get("status_code", ""))
            if status in {"FINISHED", "PUBLISHED"}:
                return
            if status in {"ERROR", "EXPIRED"}:
                raise PublishError(f"Instagram could not process the media ({status}: {info.get('status', 'no detail')}).",
                                   retryable=False)
            if time.monotonic() > deadline:
                raise PublishError("Instagram is still processing the media after "
                                   f"{int(self.poll_timeout)}s.", retryable=True)
            self._sleep(self.poll_interval)

    def _publish(self, container_id: str) -> str:
        # Ambiguous failure here could mean "posted but we never heard back" -> never auto-retry.
        return str(self._call("POST", f"{self.user_id}/media_publish", {"creation_id": container_id},
                              safe_to_retry=False)["id"])

    def _permalink(self, media_id: str) -> str:
        try:
            return str(self._call("GET", media_id, {"fields": "permalink"}).get("permalink", ""))
        except PublishError:
            return ""

    def _check_quota(self) -> None:
        try:
            data = self._call("GET", f"{self.user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
            row = (data.get("data") or [{}])[0]
            used, total = row.get("quota_usage"), (row.get("config") or {}).get("quota_total")
        except (PublishError, IndexError, TypeError):
            return  # best effort: never block a post because the quota endpoint hiccuped
        if isinstance(used, int) and isinstance(total, int) and used >= total:
            raise PublishError(f"Instagram's 24-hour publishing limit is used up ({used}/{total}).", retryable=True)

    # -- main entry ----------------------------------------------------------------
    def publish(self, req: PublishRequest) -> PublishResult:
        for m in req.media:
            if not m.url:
                raise PublishError("Internal error: media has no public URL for Instagram.", retryable=False)
        self._check_quota()
        kind = req.kind
        if kind == "photo":
            cid = self._container({"image_url": req.media[0].url, "caption": req.caption})
        elif kind == "story":
            m = req.media[0]
            cid = self._container({"media_type": "STORIES", _url_key(m): m.url})
        elif kind == "reel":
            cid = self._container({"media_type": "REELS", "video_url": req.media[0].url, "caption": req.caption,
                                   "share_to_feed": "true"})
        elif kind == "carousel":
            children = []
            for m in req.media:
                params: dict[str, Any] = {"is_carousel_item": "true", _url_key(m): m.url}
                if m.kind == "video":
                    params["media_type"] = "VIDEO"
                child = self._container(params)
                self._wait(child)
                children.append(child)
            cid = self._container({"media_type": "CAROUSEL", "children": ",".join(children), "caption": req.caption})
        else:
            raise PublishError(f"Instagram can't publish a {kind!r}.", retryable=False)
        self._wait(cid)
        media_id = self._publish(cid)
        return PublishResult(remote_id=media_id, permalink=self._permalink(media_id))

    # -- ops -----------------------------------------------------------------------
    def selfcheck(self) -> list[str]:
        lines = []
        try:
            me = self._call("GET", "me", {"fields": "username,account_type"})
            lines.append(f"instagram: connected as @{me.get('username', '?')} ({me.get('account_type', 'type unknown')})")
            if str(me.get("account_type", "")).upper() == "PERSONAL":
                lines.append("instagram: WARNING personal accounts cannot publish via the API - switch to Business/Creator")
        except PublishError as exc:
            lines.append(f"instagram: FAILED {exc}")
            return lines
        try:
            data = self._call("GET", f"{self.user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
            row = (data.get("data") or [{}])[0]
            lines.append(f"instagram: publishing quota used {row.get('quota_usage', '?')}/"
                         f"{(row.get('config') or {}).get('quota_total', '?')} in the last 24h")
        except PublishError as exc:
            lines.append(f"instagram: could not read publishing quota ({exc})")
        return lines


def _url_key(m: PreparedMedia) -> str:
    return "video_url" if m.kind == "video" else "image_url"


def refresh_long_lived_token(http: httpx.Client, token: str, base: str = "https://graph.instagram.com") -> dict[str, Any]:
    """Extend an Instagram long-lived token (valid ~60 days; refreshable once it is >24h old and not expired)."""
    resp = http.get(f"{base.rstrip('/')}/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": token})
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or "error" in body or "access_token" not in body:
        msg = (body.get("error") or {}).get("message", f"HTTP {resp.status_code}")
        raise PublishError(f"Could not refresh the Instagram token: {msg}", retryable=False)
    return body

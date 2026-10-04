"""A small client for the WeChat Official Account API (standard library only).

Covers what drafting an article needs: a stable access token, uploading body images
(``media/uploadimg``) and the cover (``material/add_material``), and creating or
updating a draft (``draft/add`` / ``draft/update``). Publishing is deliberately
absent: sending to followers is a human decision made in the WeChat back end.

The account's API credentials come from the ``credentials`` service; the client
never logs them, and error messages never include request URLs (which carry the
access token).
"""

from __future__ import annotations

import json
import mimetypes
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

BASE = "https://api.weixin.qq.com/cgi-bin"

# Actionable hints for common error codes.
HINTS = {
    40001: "access_token invalid: was the AppSecret reset in the WeChat back end?",
    40013: "AppID is wrong",
    40125: "AppSecret is wrong",
    40164: "this machine's public IP is not on the IP whitelist "
    "(WeChat back end: Settings and Development > Basic Configuration > IP whitelist)",
    45009: "the API's daily call quota is used up",
    48001: "this account has no permission for this API",
}

# transport(api, method, url, json_body | None, files | None) -> parsed JSON response
Transport = Callable[[str, str, str, Any, Any], dict[str, Any]]


class WeChatError(RuntimeError):
    def __init__(self, api: str, message: str, *, errcode: int | None = None) -> None:
        hint = HINTS.get(errcode or 0)
        super().__init__(f"WeChat API {api}: {message}" + (f" — {hint}" if hint else ""))
        self.api = api
        self.errcode = errcode


class WeChatClient:
    def __init__(
        self,
        appid: str,
        appsecret: str,
        *,
        transport: Transport | None = None,
        base: str = BASE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._appid = appid
        self._appsecret = appsecret
        self._transport = transport or urllib_transport
        self._base = base
        self._clock = clock
        self._token: tuple[str, float] | None = None

    def __repr__(self) -> str:  # never show the secret
        return f"<WeChatClient appid={self._appid!r}>"

    def access_token(self) -> tuple[str, int]:
        """A stable token (``stable_token``): repeated calls return the same token while
        it is valid, so other users of the account are not invalidated."""
        now = self._clock()
        if self._token and self._token[1] - now > 60:
            return self._token[0], int(self._token[1] - now)
        data = self._call(
            "stable_token",
            "POST",
            f"{self._base}/stable_token",
            {"grant_type": "client_credential", "appid": self._appid, "secret": self._appsecret},
        )
        self._token = (data["access_token"], now + int(data["expires_in"]))
        return data["access_token"], int(data["expires_in"])

    def upload_content_image(self, path: Path) -> str:
        """Upload an image used inside an article body (jpg/png, < 1 MB); returns its URL."""
        data = self._authed("media/uploadimg", f"{self._base}/media/uploadimg", files=path)
        return data["url"]

    def add_image_material(self, path: Path) -> str:
        """Upload a permanent image (a cover); returns its media_id."""
        url = f"{self._base}/material/add_material?type=image"
        return self._authed("material/add_material", url, files=path)["media_id"]

    def add_draft(self, article: dict[str, Any]) -> str:
        return self._authed("draft/add", f"{self._base}/draft/add", {"articles": [article]})[
            "media_id"
        ]

    def update_draft(self, media_id: str, article: dict[str, Any], index: int = 0) -> None:
        body = {"media_id": media_id, "index": index, "articles": article}
        self._authed("draft/update", f"{self._base}/draft/update", body)

    def _authed(self, api: str, url: str, body: Any = None, *, files: Path | None = None):
        token, _ = self.access_token()
        joined = f"{url}{'&' if '?' in url else '?'}access_token={token}"
        return self._call(api, "POST", joined, body, files)

    def _call(self, api: str, method: str, url: str, body: Any, files: Path | None = None):
        data = self._transport(api, method, url, body, files)
        errcode = data.get("errcode")
        if errcode:
            raise WeChatError(api, f"error {errcode}: {data.get('errmsg', '')}", errcode=errcode)
        return data


def urllib_transport(api: str, method: str, url: str, body: Any, files: Path | None) -> dict:
    headers: dict[str, str] = {}
    if files is not None:
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(files.name)[0] or "image/jpeg"
        payload = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="media"; '
                f'filename="{files.name}"\r\nContent-Type: {mime}\r\n\r\n'
            ).encode()
            + files.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    else:
        payload = None
    req = urlrequest.Request(url, data=payload, headers=headers, method=method)
    # The URL carries the access token: errors name the API, never the URL.
    try:
        with urlrequest.urlopen(req, timeout=60) as response:
            raw = response.read()
    except HTTPError as exc:
        raise WeChatError(api, f"HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise WeChatError(api, f"cannot reach the API ({type(exc).__name__})") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise WeChatError(api, "the API returned something that is not JSON") from None

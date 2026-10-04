"""The API client, against a fake transport."""

from __future__ import annotations

import pytest
from runspool_wechat.api import WeChatClient, WeChatError


class FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, api, method, url, body, files):
        self.calls.append((api, url, body, files))
        handler = self.responses.get(api)
        if handler is not None:
            return handler(body)
        return {
            "stable_token": {"access_token": "TOKEN", "expires_in": 7200},
            "media/uploadimg": {"url": "https://mmbiz.qpic.cn/x.png"},
            "material/add_material": {"media_id": "COVER", "url": "u"},
            "draft/add": {"media_id": "DRAFT"},
            "draft/update": {"errcode": 0},
        }[api]


def test_the_token_is_reused_while_valid():
    t = FakeTransport()
    client = WeChatClient("appid", "secret", transport=t, clock=lambda: 1000.0)
    client.add_draft({"title": "x"})
    client.add_draft({"title": "y"})
    assert [c[0] for c in t.calls].count("stable_token") == 1
    assert t.calls[0][2] == {
        "grant_type": "client_credential",
        "appid": "appid",
        "secret": "secret",
    }
    assert "access_token=TOKEN" in t.calls[1][1]


def test_errors_carry_a_hint_and_no_secret():
    t = FakeTransport({"stable_token": lambda body: {"errcode": 40164, "errmsg": "invalid ip"}})
    client = WeChatClient("appid", "very-secret", transport=t)
    with pytest.raises(WeChatError, match="IP whitelist") as caught:
        client.access_token()
    assert "very-secret" not in str(caught.value)
    assert "very-secret" not in repr(client)


def test_drafts_are_added_and_updated(tmp_path):
    t = FakeTransport()
    client = WeChatClient("a", "s", transport=t)
    image = tmp_path / "a.png"
    image.write_bytes(b"png")
    assert client.upload_content_image(image) == "https://mmbiz.qpic.cn/x.png"
    assert client.add_image_material(image) == "COVER"
    assert client.add_draft({"title": "t"}) == "DRAFT"
    client.update_draft("DRAFT", {"title": "t2"})
    update = next(c for c in t.calls if c[0] == "draft/update")
    assert update[2] == {"media_id": "DRAFT", "index": 0, "articles": {"title": "t2"}}

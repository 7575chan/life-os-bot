"""GAS 中継クライアント: 偽のサーバー (httpx.MockTransport) で挙動を検証する。"""
import base64
import json
import logging

import httpx
import pytest

import gas_relay
from gas_relay import GasRelay, RelayError
from notes_policy import AccessDenied

URL = "https://script.example/exec"
TOKEN = "SECRET-TOKEN-0123456789abcdef"


def make(handler) -> GasRelay:
    return GasRelay(URL, TOKEN, transport=httpx.MockTransport(handler))


def ok(status="created", **extra):
    return httpx.Response(200, json={"ok": True, "status": status, **extra})


def test_create_file_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["ctype"] = request.headers["content-type"]
        return ok(id="abc", url="https://drive/abc")

    res = make(handler).create_file("06-Life-OS/09-idea/x.md", "本文🗃")
    b = seen["body"]
    assert b["op"] == "create_file" and b["path"] == "06-Life-OS/09-idea/x.md" and b["content"] == "本文🗃"
    assert b["token"] == TOKEN
    assert len(b["request_id"]) == 32 and int(b["request_id"], 16) >= 0  # GAS 側の検証 ^[0-9a-f]{16,64}$
    assert seen["ctype"].startswith("text/plain")
    assert res["url"] == "https://drive/abc"


def test_create_bytes_is_base64():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok()

    make(handler).create_bytes("06-Life-OS/08-scrap/attachments/a.png", b"\x89PNG\x00\xff", "image/png")
    assert base64.b64decode(seen["body"]["content_base64"]) == b"\x89PNG\x00\xff"
    assert seen["body"]["mime"] == "image/png"


def test_rename_and_trash_ops():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return ok("renamed")

    r = make(handler)
    r.rename("06-Life-OS/a.md", "06-Life-OS/b.md")
    r.trash("06-Life-OS/b.md")
    assert bodies[0]["op"] == "rename" and bodies[0]["new_path"] == "06-Life-OS/b.md"
    assert bodies[1]["op"] == "trash" and bodies[1]["path"] == "06-Life-OS/b.md"


def test_only_four_ops_exist():
    public = {n for n in dir(GasRelay) if not n.startswith("_") and callable(getattr(GasRelay, n))}
    assert public == {"create_file", "create_bytes", "rename", "trash", "from_config"}


def test_follows_gas_redirect():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.host))
        if request.url.host == "script.example":
            return httpx.Response(302, headers={"location": "https://out.example/result"})
        return ok(id="z")

    assert make(handler).trash("06-Life-OS/a.md")["id"] == "z"
    assert calls == [("POST", "script.example"), ("GET", "out.example")]


def test_retries_reuse_request_id():
    ids = []

    def handler(request):
        ids.append(json.loads(request.content)["request_id"])
        return httpx.Response(500) if len(ids) == 1 else ok()

    assert make(handler).create_file("06-Life-OS/a.md", "x")["ok"]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_gives_up_after_retries():
    n = {"c": 0}

    def handler(request):
        n["c"] += 1
        return httpx.Response(503)

    with pytest.raises(RelayError):
        make(handler).create_file("06-Life-OS/a.md", "x")
    assert n["c"] == 1 + gas_relay.RETRIES


def test_network_error_becomes_relay_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(RelayError, match="接続できません"):
        make(handler).create_file("06-Life-OS/a.md", "x")


def test_denied_raises_access_denied_and_logs(caplog):
    def handler(request):
        return httpx.Response(200, json={"ok": False, "status": "denied", "message": "outside 06-Life-OS"})

    with caplog.at_level(logging.ERROR, logger="life-os.guard"):
        with pytest.raises(AccessDenied):
            make(handler).create_file("06-Life-OS/a.md", "x")
    assert any("DENY (GAS relay)" in r.message for r in caplog.records)


def test_unauthorized_never_leaks_token(caplog):
    def handler(request):
        return httpx.Response(200, json={"ok": False, "status": "unauthorized", "message": "unauthorized"})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RelayError) as ei:
            make(handler).create_file("06-Life-OS/a.md", "x")
    assert TOKEN not in str(ei.value)
    assert TOKEN not in caplog.text


def test_token_not_logged_on_failures(caplog):
    def handler(request):
        return httpx.Response(500)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RelayError) as ei:
            make(handler).trash("06-Life-OS/a.md")
    assert TOKEN not in caplog.text and TOKEN not in str(ei.value)


def test_not_found_maps_to_file_not_found():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "status": "not_found"})

    with pytest.raises(FileNotFoundError):
        make(handler).rename("06-Life-OS/a.md", "06-Life-OS/b.md")


def test_exists_is_returned_not_raised():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "status": "exists", "id": "old"})

    assert make(handler).create_file("06-Life-OS/a.md", "x")["status"] == "exists"


def test_gas_error_message_is_wrapped():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "status": "error", "message": "boom"})

    with pytest.raises(RelayError, match="boom"):
        make(handler).create_file("06-Life-OS/a.md", "x")


def test_non_json_response_is_explained():
    def handler(request):
        return httpx.Response(200, text="<html>ログインしてください</html>")

    with pytest.raises(RelayError, match="デプロイ設定"):
        make(handler).create_file("06-Life-OS/a.md", "x")


def test_size_limits_checked_before_sending():
    def handler(request):
        raise AssertionError("送信してはいけない")

    r = make(handler)
    with pytest.raises(RelayError, match="大きすぎ"):
        r.create_bytes("06-Life-OS/a.png", b"x" * (gas_relay.MAX_BYTES + 1))
    with pytest.raises(RelayError, match="大きすぎ"):
        r.create_file("06-Life-OS/a.md", "あ" * (gas_relay.MAX_BYTES // 3 + 1))


def test_requires_configuration():
    with pytest.raises(RelayError):
        GasRelay("", "")
    with pytest.raises(RelayError):
        GasRelay(URL, "")

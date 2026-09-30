"""天気: 取れなかったときに原因を警告ログに残し、None を返す（問いかけは止めない）。"""
import asyncio
import logging

import httpx
import pytest

import weather

OK = {"daily": {"weather_code": [1], "temperature_2m_min": [18.4], "temperature_2m_max": [25.6],
                "precipitation_probability_max": [10]}}


_REAL = httpx.AsyncClient


def fetch(monkeypatch, handler):
    def client(**kw):
        return _REAL(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(weather.httpx, "AsyncClient", client)
    return asyncio.run(weather.today_weather())


def test_ok(monkeypatch, caplog):
    assert fetch(monkeypatch, lambda req: httpx.Response(200, json=OK)) == "晴れ 18〜26℃（降水確率 10%）"
    assert not caplog.records


def test_missing_rain_probability_still_gives_weather():
    d = {**OK["daily"], "precipitation_probability_max": [None]}
    assert weather.describe(d) == "晴れ 18〜26℃"


@pytest.mark.parametrize("handler, reason", [
    (lambda req: httpx.Response(429, text="Too many requests"), "HTTP 429"),
    (lambda req: httpx.Response(200, json={"error": True}), "応答を読めません"),
    (lambda req: httpx.Response(200, json={"daily": {**OK["daily"], "temperature_2m_min": [None]}}), "値が欠けています"),
])
def test_failures_are_logged_with_reason(monkeypatch, caplog, handler, reason):
    with caplog.at_level(logging.WARNING, logger="life-os.weather"):
        assert fetch(monkeypatch, handler) is None
    assert reason in caplog.text and "天気を取得できませんでした" in caplog.text


def test_connection_error_and_timeout_are_logged(monkeypatch, caplog):
    def refuse(req):
        raise httpx.ConnectError("Name or service not known")

    def slow(req):
        raise httpx.ReadTimeout("timed out")

    with caplog.at_level(logging.WARNING, logger="life-os.weather"):
        assert fetch(monkeypatch, refuse) is None
        assert fetch(monkeypatch, slow) is None
    assert "接続できません（ConnectError: Name or service not known）" in caplog.text
    assert "10秒以内に応答がありません" in caplog.text

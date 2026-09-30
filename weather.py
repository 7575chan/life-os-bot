"""Open-Meteo (APIキー不要) から今日の天気を取得する。失敗したら原因を警告ログに残して None を返す（投稿は止めない）。

VM での確認: `.venv/bin/python weather.py`（結果か、失敗の原因を表示する）
"""
import asyncio
import logging

import httpx

import config
log = logging.getLogger("life-os.weather")
URL = "https://api.open-meteo.com/v1/forecast"

_CODES = {
    0: "快晴", 1: "晴れ", 2: "くもり時々晴れ", 3: "くもり", 45: "霧", 48: "霧",
    51: "小雨", 53: "小雨", 55: "雨", 56: "凍雨", 57: "凍雨",
    61: "雨", 63: "雨", 65: "強い雨", 66: "凍雨", 67: "凍雨",
    71: "雪", 73: "雪", 75: "大雪", 77: "雪", 80: "にわか雨", 81: "にわか雨", 82: "強いにわか雨",
    85: "にわか雪", 86: "にわか雪", 95: "雷雨", 96: "雷雨", 99: "雷雨",
}


def describe(d: dict) -> str:
    """Open-Meteo の daily から「晴れ 18〜25℃（降水確率 10%）」を作る。値が欠けていれば ValueError。"""
    code, lo, hi, rain = (d[k][0] for k in ("weather_code", "temperature_2m_min", "temperature_2m_max",
                                           "precipitation_probability_max"))
    if code is None or lo is None or hi is None:
        raise ValueError(f"値が欠けています（weather_code={code}, 最低={lo}, 最高={hi}）")
    label = _CODES.get(code, "不明")
    tail = f"（降水確率 {rain}%）" if rain is not None else ""
    return f"{label} {round(lo)}〜{round(hi)}℃{tail}"


async def today_weather() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                URL,
                params={
                    "latitude": config.WEATHER_LAT, "longitude": config.WEATHER_LON,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "Asia/Tokyo", "forecast_days": 1,
                },
            )
    except httpx.TimeoutException as e:
        log.warning("天気を取得できませんでした: 10秒以内に応答がありません（%s）", type(e).__name__)
        return None
    except httpx.HTTPError as e:
        log.warning("天気を取得できませんでした: 接続できません（%s: %s）", type(e).__name__, e)
        return None
    if r.status_code != 200:
        log.warning("天気を取得できませんでした: HTTP %s（%s）", r.status_code, r.text[:200].replace("\n", " "))
        return None
    try:
        return describe(r.json()["daily"])
    except Exception as e:  # noqa: BLE001  応答の形が想定と違う
        log.warning("天気を取得できませんでした: 応答を読めません（%s: %s）。応答の先頭: %s",
                    type(e).__name__, e, r.text[:200].replace("\n", " "))
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(asyncio.run(today_weather()) or "（天気を取得できませんでした。上の WARNING が原因です）")

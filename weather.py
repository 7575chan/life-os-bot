"""Open-Meteo (APIキー不要) から今日の天気を取得する。失敗しても None を返すだけ。"""
import httpx

import config

_CODES = {
    0: "快晴", 1: "晴れ", 2: "くもり時々晴れ", 3: "くもり", 45: "霧", 48: "霧",
    51: "小雨", 53: "小雨", 55: "雨", 56: "凍雨", 57: "凍雨",
    61: "雨", 63: "雨", 65: "強い雨", 66: "凍雨", 67: "凍雨",
    71: "雪", 73: "雪", 75: "大雪", 77: "雪", 80: "にわか雨", 81: "にわか雨", 82: "強いにわか雨",
    85: "にわか雪", 86: "にわか雪", 95: "雷雨", 96: "雷雨", 99: "雷雨",
}


async def today_weather() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": config.WEATHER_LAT, "longitude": config.WEATHER_LON,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "Asia/Tokyo", "forecast_days": 1,
                },
            )
            r.raise_for_status()
            d = r.json()["daily"]
        label = _CODES.get(d["weather_code"][0], "不明")
        return f"{label} {round(d['temperature_2m_min'][0])}〜{round(d['temperature_2m_max'][0])}℃（降水確率 {d['precipitation_probability_max'][0]}%）"
    except Exception:
        return None

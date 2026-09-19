"""体調データの集計（純関数）。「自分取扱マニュアル」の材料にする。評価や説教はここでは行わない。"""
import re
from datetime import date

import util

_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*時間(?:\s*(\d+)\s*分)?")


def parse_sleep_hours(text: str) -> float | None:
    """'10.5' / '10.5時間' / '7時間30分' -> 時間(float)。読めなければ None。"""
    t = str(text).strip()
    if not t:
        return None
    m = _HOURS.search(t)
    if m:
        return round(float(m.group(1)) + (int(m.group(2)) / 60 if m.group(2) else 0), 2)
    try:
        v = float(t)
        return v if 0 < v <= 24 else None
    except ValueError:
        return None


def sleep_mood_table(health_rows: list[dict], diary_rows: list[dict], threshold: float = 9.0) -> dict:
    """睡眠時間（前夜〜当日）とご機嫌度・体調スコアの平均を、しきい値の上下で比べる。

    同じ日付の日記のご機嫌度と、その日の体調シートの睡眠時間を突き合わせる。
    """
    mood_by_day: dict[date, list[int]] = {}
    for r in diary_rows:
        d, m = util.parse_date_any(r.get("日付", "")), util.clamp_int(r.get("ご機嫌度"), 1, 5)
        if d and m:
            mood_by_day.setdefault(d, []).append(m)
    buckets = {"under": {"n": 0, "mood": [], "score": []}, "over": {"n": 0, "mood": [], "score": []}}
    for r in health_rows:
        d, h = util.parse_date_any(r.get("日時", "")), parse_sleep_hours(r.get("睡眠時間", ""))
        if not d or h is None:
            continue
        b = buckets["under" if h < threshold else "over"]
        b["n"] += 1
        b["mood"] += mood_by_day.get(d, [])
        s = util.clamp_int(r.get("体調スコア"), 1, 5)
        if s:
            b["score"].append(s)

    def avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    return {
        "threshold_hours": threshold,
        "under": {"days": buckets["under"]["n"], "avg_mood": avg(buckets["under"]["mood"]), "avg_score": avg(buckets["under"]["score"])},
        "over": {"days": buckets["over"]["n"], "avg_mood": avg(buckets["over"]["mood"]), "avg_score": avg(buckets["over"]["score"])},
    }

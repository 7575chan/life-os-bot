"""Garmin と Habitify の記録（SPEC §5 `02-health`「Garmin と Habitify のメールの記録と分析」）。

- 記録はユーザーのアカウントで動く GAS（`gas/GarminImport.gs`）が、別のブック（`GARMIN_SHEET_ID`）の
  `Garmin` / `Habitify` タブに書く。**Bot は読むだけ**（書かない。`ensure_schema` も触らない）
- 分析は数字だけ（ご機嫌度の平均と、週・日の数）。助言・評価の言葉、達成率は出さない
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date, timedelta

import config
import sheets
import util

log = logging.getLogger("life-os.body_data")

WEEK_TREND = 26  # 週のデータを見る、直近の週の数
DAY_TREND = 90  # 日のデータ（Body Battery）を見る、直近の日数
MIN_GROUP = 2  # 比べる組それぞれに必要な数（週・日）
SLEEP_MINUTES = 480  # 8時間
BB_THRESHOLD = 60
COLS = ("日付", "期間", "項目", "値", "受信日時")
_DT = re.compile(r"(\d{4})\D(\d{1,2})\D(\d{1,2})(?:\D+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def enabled() -> bool:
    return bool(config.GARMIN_SHEET_ID)


# ---------------------------------------------------------------- 読み取り


def _received(value: str) -> tuple:
    m = _DT.search(str(value or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else ()


def parse(values: list[list[str]]) -> list[dict]:
    """タブの値（1行目が見出し）→ [{date, period, item, value, order}]。
    同じ日付・期間・項目は、受信日時が新しいほう（同じなら下の行）を使う。読めない行は飛ばす。"""
    if not values:
        return []
    idx = {str(h).strip(): i for i, h in enumerate(values[0])}
    if any(c not in idx for c in COLS[:4]):
        raise ValueError(f"見出しが足りません（必要: {'・'.join(COLS[:4])}）")

    def get(row, name):
        i = idx.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    best: dict[tuple, tuple] = {}
    for n, row in enumerate(values[1:]):
        d = util.parse_date_any(get(row, "日付"))
        item = get(row, "項目")
        try:
            v = float(get(row, "値").replace(",", ""))
        except ValueError:
            continue
        if not d or not item:
            continue
        key = (d, get(row, "期間"), item)
        rank = (_received(get(row, "受信日時")), n)
        if key not in best or rank >= best[key][0]:
            best[key] = (rank, v, n)
    return [{"date": k[0], "period": k[1], "item": k[2], "value": v, "order": n}
            for k, (_, v, n) in sorted(best.items(), key=lambda kv: kv[1][2])]


def _tab(name: str) -> list[list[str]]:
    book = sheets.client().open_by_key(config.GARMIN_SHEET_ID)
    try:
        ws = book.worksheet(name)
    except Exception as e:  # noqa: BLE001  タブがまだ無い（最初の取り込み前）は空として扱う
        if type(e).__name__ == "WorksheetNotFound":
            return []
        raise
    return ws.get_all_values()


def load() -> tuple[list[dict], list[dict]]:
    """(Garmin の記録, Habitify の記録)。読めなければ例外。"""
    return parse(_tab(config.GARMIN_TAB)), parse(_tab(config.HABITIFY_TAB))


# ---------------------------------------------------------------- 分析（純粋関数）


def _avg(xs: list[float]) -> float | None:
    return math.floor(sum(xs) / len(xs) * 10 + 0.5) / 10 if xs else None


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def daily_moods(diary: list[dict]) -> dict[date, int]:
    out = {}
    for r in diary:
        d = util.parse_date_any(r.get("日付", ""))
        m = util.clamp_int(r.get("ご機嫌度"), 1, 5)
        if d and m:
            out[d] = m
    return out


def weekly_moods(moods: dict[date, int]) -> dict[date, float]:
    by: dict[date, list[int]] = {}
    for d, m in moods.items():
        by.setdefault(week_start(d), []).append(m)
    return {w: sum(v) / len(v) for w, v in by.items()}


def _split(groups: dict[bool, list[float]]) -> tuple | None:
    yes, no = groups.get(True, []), groups.get(False, [])
    if len(yes) < MIN_GROUP or len(no) < MIN_GROUP:
        return None
    return _avg(yes), len(yes), _avg(no), len(no)


def _weeks(records: list[dict], first: date, end: date) -> dict[date, dict[str, float]]:
    """週の記録 → {月曜: {項目: 値}}（[first, end) の週だけ）。"""
    out: dict[date, dict[str, float]] = {}
    for r in records:
        if r["period"] == "週":
            w = week_start(r["date"])
            if first <= w < end:
                out.setdefault(w, {})[r["item"]] = r["value"]
    return out


def habit_names(habitify: list[dict]) -> list[str]:
    """習慣の名前（最後に届いたレポートの順。それより前にしか無い習慣は、そのあとに）。"""
    days = [r for r in habitify if r["item"].endswith("・日数")]
    if not days:
        return []
    latest = max(r["date"] for r in days)
    ordered = sorted(days, key=lambda r: (r["date"] != latest, r["order"]))
    return list(dict.fromkeys(r["item"][: -len("・日数")] for r in ordered))


def analyze(garmin: list[dict], habitify: list[dict], diary: list[dict], end: date) -> list[str]:
    """レポート・取扱マニュアルに出す行（数字だけ）。出せるものが無ければ空。end の日は含まない。"""
    moods = daily_moods(diary)
    wmood = weekly_moods(moods)
    first = week_start(end) - timedelta(weeks=WEEK_TREND)
    gw, hw = _weeks(garmin, first, end), _weeks(habitify, first, end)
    lines = []
    head_w = f"直近{WEEK_TREND}週の傾向: "

    # 1. 睡眠（同じ週に2つのメールの値があれば、新しいほう）
    g: dict[bool, list[float]] = {}
    for w, items in gw.items():
        if "睡眠(分)" in items and w in wmood:
            g.setdefault(items["睡眠(分)"] >= SLEEP_MINUTES, []).append(wmood[w])
    if (s := _split(g)):
        lines.append(head_w + f"睡眠{SLEEP_MINUTES // 60}時間以上の週のご機嫌度 {s[0]}（{s[1]}週） / "
                              f"{SLEEP_MINUTES // 60}時間未満 {s[2]}（{s[3]}週）")

    # 2. 運動（Garmin のアクティビティ ＋ Habitify の運動の習慣）
    g = {}
    fitness_marks = ("歩数", "週間運動量")
    for w in set(gw) | set(hw):
        if w not in wmood:
            continue
        gi, hi = gw.get(w, {}), hw.get(w, {})
        g_known = any(k in gi for k in fitness_marks) or any(k.endswith("・回数") for k in gi)
        h_known = bool(hi)
        if not (g_known or h_known):
            continue
        moved = any(k.endswith("・回数") and v > 0 for k, v in gi.items()) or \
            any(hi.get(f"{name}・日数", 0) > 0 for name in config.HABITIFY_EXERCISE)
        g.setdefault(moved, []).append(wmood[w])
    if (s := _split(g)):
        lines.append(head_w + f"運動した週のご機嫌度 {s[0]}（{s[1]}週） / しない週 {s[2]}（{s[3]}週）")

    # 3. Body Battery のチャージ → 翌日のご機嫌度
    since = end - timedelta(days=DAY_TREND)
    g = {}
    for r in garmin:
        if r["period"] == "日" and r["item"] == "BodyBatteryチャージ" and since <= r["date"] < end:
            nxt = moods.get(r["date"] + timedelta(days=1))
            if nxt:
                g.setdefault(r["value"] >= BB_THRESHOLD, []).append(nxt)
    if (s := _split(g)):
        lines.append(f"直近{DAY_TREND}日の傾向: Body Battery のチャージが{BB_THRESHOLD}以上の翌日のご機嫌度 {s[0]}（{s[1]}日）"
                     f" / {BB_THRESHOLD}未満 {s[2]}（{s[3]}日）")

    # 4. 習慣ごと（執筆などは記録だけ）
    for name in habit_names(habitify):
        if name in config.HABITIFY_SKIP:
            continue
        g = {}
        for w, items in hw.items():
            key = f"{name}・日数"
            if key in items and w in wmood:
                g.setdefault(items[key] > 0, []).append(wmood[w])
        if (s := _split(g)):
            lines.append(head_w + f"{name}をした週のご機嫌度 {s[0]}（{s[1]}週） / しない週 {s[2]}（{s[3]}週）")
    return lines

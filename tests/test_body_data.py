"""Garmin と Habitify の記録の分析（SPEC §5 02-health「Garmin と Habitify のメールの記録と分析」）。"""
import asyncio
from datetime import date, timedelta

import pytest

import body_data as bd
import config
import report

HEADER = ["日付", "期間", "項目", "値", "受信日時", "メールID"]
END = date(2026, 10, 19)  # 月曜（この日は含まない）


def mondays(n):
    return [END - timedelta(weeks=i) for i in range(1, n + 1)]


def recs(rows):
    return bd.parse([HEADER] + [[d.isoformat(), p, item, str(v), "2026/10/12 7:00:00", "id"] for d, p, item, v in rows])


def diary_for(week_moods):
    """{月曜: ご機嫌度} → その週の月〜水に同じご機嫌度の日記。"""
    out = []
    for w, m in week_moods.items():
        out += [{"日付": (w + timedelta(days=i)).isoformat(), "ご機嫌度": str(m)} for i in range(3)]
    return out


def test_parse_dedupes_by_received_and_skips_bad_rows():
    values = [HEADER,
              ["2026-10-05", "週", "睡眠(分)", "500", "2026/10/12 7:00:00", "a"],
              ["2026-10-05", "週", "睡眠(分)", "470", "2026/10/13 7:00:00", "b"],  # 新しいほう
              ["2026-10-05", "週", "睡眠(分)", "999", "2026/10/11 7:00:00", "c"],
              ["", "週", "x", "1", "", ""], ["2026-10-05", "週", "y", "なし", "", ""],
              ["2026-10-05", "週", "歩数", "5,948", "", ""]]
    got = {r["item"]: r["value"] for r in bd.parse(values)}
    assert got == {"睡眠(分)": 470, "歩数": 5948}
    with pytest.raises(ValueError):
        bd.parse([["日付", "値"]])
    assert bd.parse([]) == []


def test_sleep_split_needs_two_weeks_each():
    ws = mondays(4)
    garmin = recs([(ws[0], "週", "睡眠(分)", 512), (ws[1], "週", "睡眠(分)", 490),
                   (ws[2], "週", "睡眠(分)", 400), (ws[3], "週", "睡眠(分)", 450)])
    diary = diary_for({ws[0]: 4, ws[1]: 5, ws[2]: 3, ws[3]: 2})
    lines = bd.analyze(garmin, [], diary, END)
    assert lines == ["直近26週の傾向: 睡眠8時間以上の週のご機嫌度 4.5（2週） / 8時間未満 2.5（2週）"]
    assert bd.analyze(garmin[:3], [], diary, END) == []  # 未満が1週だけ → 出さない


def test_exercise_combines_garmin_and_habitify():
    ws = mondays(5)
    garmin = recs([(ws[0], "週", "ウォーキング・回数", 2), (ws[0], "週", "歩数", 5948),
                   (ws[1], "週", "歩数", 3000),  # フィットネスのメールはあるが運動なし
                   (ws[4], "週", "睡眠(分)", 500)])  # ウェルネスだけの週は数えない
    habitify = recs([(ws[2], "週", "運動をする・日数", 1), (ws[3], "週", "運動をする・日数", 0),
                     (ws[3], "週", "本を読む・日数", 2)])
    diary = diary_for({ws[0]: 4, ws[1]: 2, ws[2]: 5, ws[3]: 3, ws[4]: 1})
    lines = bd.analyze(garmin, habitify, diary, END)
    assert "直近26週の傾向: 運動した週のご機嫌度 4.5（2週） / しない週 2.5（2週）" in lines


def test_body_battery_and_next_day_mood():
    days = [END - timedelta(days=i) for i in range(2, 6)]
    garmin = recs([(days[0], "日", "BodyBatteryチャージ", 71), (days[1], "日", "BodyBatteryチャージ", 85),
                   (days[2], "日", "BodyBatteryチャージ", 46), (days[3], "日", "BodyBatteryチャージ", 30),
                   (days[0], "日", "BodyBattery消費", 92)])
    diary = [{"日付": (d + timedelta(days=1)).isoformat(), "ご機嫌度": m} for d, m in zip(days, ["5", "4", "2", "3"])]
    lines = bd.analyze(garmin, [], diary, END)
    assert lines == ["直近90日の傾向: Body Battery のチャージが60以上の翌日のご機嫌度 4.5（2日） / 60未満 2.5（2日）"]


def test_every_habit_but_writing_in_report_order():
    ws = mondays(4)
    rows = []
    for i, w in enumerate(ws):
        did = 1 if i < 2 else 0
        rows += [(w, "週", "全体の達成率(%)", 3.2), (w, "週", "本を読む・完了率(%)", 29),
                 (w, "週", "本を読む・日数", did * 2), (w, "週", "執筆・日数", did), (w, "週", "英語を勉強する・日数", did)]
    rows.append((ws[0], "週", "スクワットをする・日数", 1))  # した週1つだけ → 出さない
    habitify = recs(rows)
    diary = diary_for({ws[0]: 4, ws[1]: 4, ws[2]: 3, ws[3]: 2})
    lines = bd.analyze([], habitify, diary, END)
    habit_lines = [x for x in lines if "をした週" in x]
    assert habit_lines == ["直近26週の傾向: 本を読むをした週のご機嫌度 4.0（2週） / しない週 2.5（2週）",
                           "直近26週の傾向: 英語を勉強するをした週のご機嫌度 4.0（2週） / しない週 2.5（2週）"]
    assert all("執筆" not in x and "%" not in x and "達成率" not in x for x in lines)


def test_weeks_outside_26_are_ignored():
    old = END - timedelta(weeks=30)
    garmin = recs([(old, "週", "睡眠(分)", 500), (old - timedelta(weeks=1), "週", "睡眠(分)", 500)])
    assert bd.analyze(garmin, [], diary_for({old: 5}), END) == []


def test_report_adds_lines_only_when_configured(monkeypatch):
    ws = mondays(4)
    garmin = recs([(ws[0], "週", "睡眠(分)", 512), (ws[1], "週", "睡眠(分)", 490),
                   (ws[2], "週", "睡眠(分)", 400), (ws[3], "週", "睡眠(分)", 450)])
    d = report.Data(diary=[(i, r) for i, r in enumerate(diary_for({ws[0]: 4, ws[1]: 5, ws[2]: 3, ws[3]: 2}))],
                    garmin=garmin)
    p = report.period_for("weekly", END - timedelta(days=1))
    assert any("睡眠8時間以上の週" in x for x in report.section_health(d, p))
    assert not any("睡眠8時間以上の週" in x for x in report.section_health(report.Data(diary=d.diary), p))


def test_collect_reads_only_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(bd, "load", lambda: calls.append(1) or ([], []))
    monkeypatch.setattr(config, "GARMIN_SHEET_ID", "")
    monkeypatch.setattr(report.sheets, "records", lambda name: [])
    monkeypatch.setattr(report.sheets, "completed_count", lambda a, b: 0)
    monkeypatch.setattr(report.writing_log, "fetch", lambda: None)
    p = report.period_for("weekly", END)
    assert report.collect(p, END).missing == [] and calls == []
    monkeypatch.setattr(config, "GARMIN_SHEET_ID", "book")

    def broken():
        raise RuntimeError("no access")

    monkeypatch.setattr(bd, "load", broken)
    assert report.collect(p, END).missing == ["Garmin と Habitify の記録"]


def test_manual_section_is_written_by_code(monkeypatch):
    from handlers import health
    monkeypatch.setattr(config, "GARMIN_SHEET_ID", "book")
    ws = mondays(4)
    garmin = recs([(ws[0], "週", "睡眠(分)", 512), (ws[1], "週", "睡眠(分)", 490),
                   (ws[2], "週", "睡眠(分)", 400), (ws[3], "週", "睡眠(分)", 450)])
    diary = diary_for({ws[0]: 4, ws[1]: 5, ws[2]: 3, ws[3]: 2})
    monkeypatch.setattr(bd, "load", lambda: (garmin, []))
    monkeypatch.setattr(health.sheets, "between", lambda name, a, b, date_col=None: [(i, r) for i, r in enumerate(diary)])
    lines, section = asyncio.run(health._body_numbers(END - timedelta(days=1)))
    assert section.startswith("\n" + health.BODY_TITLE) and "- 直近26週の傾向: 睡眠8時間以上" in section
    monkeypatch.setattr(bd, "load", lambda: ([], []))
    assert "まだデータが少ないため" in asyncio.run(health._body_numbers(END))[1]
    monkeypatch.setattr(config, "GARMIN_SHEET_ID", "")
    assert asyncio.run(health._body_numbers(END)) == ([], "")

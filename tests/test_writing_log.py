from datetime import date, datetime

import writing_log as wl

# 実物（執筆記録.xlsx シート1）と同じ構造: 1行目=作品名、2=目標、3=締切、4=単位、5=Doc ID、6行目〜=日次の総文字数
D0 = 46265.4  # 2026-08-31
HEADER = ["account@example.com", "2026-09", "港町の人魚", "2025-05 K課の話", "空の作品"]


def matrix(rows):
    return [
        HEADER,
        ["目標", 50000, 30000, 20000, 10000],
        ["締切", 46295, 46288, 46203, 46203],  # 2026-09-30 / 09-23 / 2026-06-30(過去) / 過去
        ["単位", 1, 1, 1, 1],
        ["Google Doc ID", "id1", "id2", "id3", "id4"],
        *rows,
    ]


def test_normalize_title_and_serial():
    assert wl.normalize_title("2025-05 K課の話") == "K課の話"
    assert wl.normalize_title("港町の人魚") == "港町の人魚"
    assert wl.to_datetime(46284.406091875004).date() == date(2026, 9, 19)
    assert wl.to_datetime("2026/9/1").date() == date(2026, 9, 1)
    assert wl.to_datetime(1) is None and wl.to_datetime("") is None and wl.to_datetime("目標") is None


def test_parse_works_targets_deadlines_by_header():
    log = wl.parse(matrix([[D0, 0, 100, 5, 0]]))
    assert set(log.works) == {"2026-09", "港町の人魚", "K課の話", "空の作品"} or "K課の話" in log.works
    w = log.works["港町の人魚"]
    assert w.target == 30000 and w.deadline == date(2026, 9, 23) and w.title == "港町の人魚"
    assert log.works["K課の話"].deadline == date(2026, 6, 30)
    assert len(log.snaps) == 1 and log.snaps[0].totals["港町の人魚"] == 100


def test_label_rows_and_blank_cells_are_ignored():
    log = wl.parse(matrix([[D0, "", 10], [D0 + 1, 5, 20, 7, 0]]))
    assert len(log.snaps) == 2  # ラベル行(目標など)は行として数えない
    assert log.snaps[0].totals["港町の人魚"] == 10 and log.snaps[0].totals["K課の話"] == 0  # 欠損は0


def test_daily_increase_uses_previous_row():
    rows = [[D0 + i, 0, 1000 * i, 500, 0] for i in range(1, 4)]  # 港町: 1000, 2000, 3000
    log = wl.parse(matrix(rows))
    st = wl.stats_for(log, date(2026, 9, 3))
    top = st["works"][0]
    assert top["name"] == "港町の人魚" and top["added"] == 1000 and top["total"] == 3000
    assert top["pct"] == 10 and top["days_left"] == 20
    assert st["total_added"] == 1000


def test_decreases_are_not_counted_as_negative():
    rows = [[D0, 0, 5000, 500, 0], [D0 + 1, 0, 4200, 900, 0]]  # 港町は推敲で-800、K課は+400
    st = wl.stats_for(wl.parse(matrix(rows)), date(2026, 9, 1))
    by = {w["name"]: w for w in st["works"]}
    assert by["港町の人魚"]["added"] == 0 and by["K課の話"]["added"] == 400
    assert st["total_added"] == 400


def test_stats_none_when_not_comparable():
    assert wl.stats_for(wl.parse(matrix([[D0, 0, 1, 1, 0]])), date(2026, 9, 1)) is None
    assert wl.stats_for(wl.parse(matrix([])), date(2026, 9, 1)) is None
    # 対象日より前の記録が1行だけ
    assert wl.stats_for(wl.parse(matrix([[D0, 0, 1, 1, 0], [D0 + 5, 0, 9, 9, 0]])), date(2026, 9, 1)) is None


def test_uses_latest_row_up_to_day_when_yesterday_missing():
    rows = [[D0, 0, 100, 0, 0], [D0 + 1, 0, 300, 0, 0]]  # 9/1 まで。対象日は 9/5
    st = wl.stats_for(wl.parse(matrix(rows)), date(2026, 9, 5))
    assert st["date"] == date(2026, 9, 1) and st["total_added"] == 200


def test_string_dates_and_number_strings():
    values = [["x", "作品"], ["目標", "3,000"], ["締切", ""], ["単位", 1], ["id", "d"],
              ["2026-09-01", "1,000"], ["2026-09-02 09:45", "1,500"]]
    st = wl.stats_for(wl.parse(values), date(2026, 9, 2))
    assert st["total_added"] == 500 and st["works"][0]["target"] == 3000


def test_format_morning_shows_only_active_increased_works():
    rows = [[D0, 0, 34215, 500, 0], [D0 + 1, 0, 36065, 500, 0]]
    d = date(2026, 9, 1)
    text = wl.format_morning(wl.stats_for(wl.parse(matrix(rows)), d), d)
    assert "合計 +1,850文字" in text
    assert "港町の人魚 +1,850（累計 36,065 / 目標 30,000・達成 120%・締切まで22日）" in text
    assert "K課の話" not in text  # 増えていない作品は出さない


def test_format_morning_is_silent_when_nothing_increased():
    rows = [[D0, 0, 100, 100, 0], [D0 + 1, 0, 100, 100, 0]]
    d = date(2026, 9, 1)
    assert wl.format_morning(wl.stats_for(wl.parse(matrix(rows)), d), d) is None
    assert wl.format_morning(None, d) is None


def test_format_morning_marks_older_record_date():
    rows = [[D0, 0, 100, 0, 0], [D0 + 1, 0, 300, 0, 0]]
    st = wl.stats_for(wl.parse(matrix(rows)), date(2026, 9, 5))
    assert "09/01 時点" in wl.format_morning(st, date(2026, 9, 5))


def test_yesterday_is_todays_row_minus_yesterdays_row():
    # 記録は毎日 09:44。9/24=1000, 9/25=1500, 9/26=2100（当日の行）
    log = wl.parse(matrix([[D0 + 24, 0, 1000, 0, 0], [D0 + 25, 0, 1500, 0, 0], [D0 + 26, 0, 2100, 0, 0]]))
    st = wl.yesterday_stats(log, date(2026, 9, 26))
    assert st["total_added"] == 600 and st["date"] == date(2026, 9, 26)  # 2100 − 1500（当日の行 − 昨日の行）
    text = wl.format_morning(st, date(2026, 9, 26))
    assert text.startswith("📊 昨日の執筆実績\n") and "合計 +600文字" in text


def test_no_yesterday_before_todays_row_exists():
    # 今日 09:44 の行がまだ無い。昨日の行 − 一昨日の行を「昨日」として出さない
    log = wl.parse(matrix([[D0 + 24, 0, 1000, 0, 0], [D0 + 25, 0, 1500, 0, 0]]))  # 9/24, 9/25
    assert wl.yesterday_stats(log, date(2026, 9, 26)) is None
    assert wl.yesterday_stats(wl.parse(matrix([])), date(2026, 9, 26)) is None
    assert wl.yesterday_stats(wl.parse(matrix([[D0 + 26, 0, 2100, 0, 0]])), date(2026, 9, 26)) is None  # 比べる行が無い


def test_snapshot_datetime_is_jst_aware():
    log = wl.parse(matrix([[D0, 0, 1, 1, 0]]))
    assert isinstance(log.snaps[0].ts, datetime) and log.snaps[0].ts.tzinfo is not None

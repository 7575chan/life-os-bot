from datetime import date

import pytest

import task_dates as td

TODAY = date(2026, 9, 20)  # 日曜日


def ex(text, today=TODAY):
    r = td.extract(text, today)
    return r.content, r.scheduled, r.due


@pytest.mark.parametrize("text,content,scheduled", [
    ("資料を送る 今日やる", "資料を送る", date(2026, 9, 20)),
    ("今日やる資料を送る", "資料を送る", date(2026, 9, 20)),
    ("明日 資料を送る", "資料を送る", date(2026, 9, 21)),
    ("あしたやる 資料を送る", "資料を送る", date(2026, 9, 21)),
    ("明後日に会議の準備をする", "会議の準備をする", date(2026, 9, 22)),
    ("9/25にレポートを書く", "レポートを書く", date(2026, 9, 25)),
    ("9月25日に提出", "提出", date(2026, 9, 25)),
    ("２０２６年１０月１日に更新", "更新", date(2026, 10, 1)),
    ("3日後に電話する", "電話する", date(2026, 9, 23)),
    ("2週間後に見直す", "見直す", date(2026, 10, 4)),
    ("来月5日に契約を更新", "契約を更新", date(2026, 10, 5)),
    ("25日に振込", "振込", date(2026, 9, 25)),
    ("明日の朝に電話する", "電話する", date(2026, 9, 21)),
    ("今夜やる 洗濯", "洗濯", date(2026, 9, 20)),
    ("明日は第3章のプロットを書く", "第3章のプロットを書く", date(2026, 9, 21)),   # 「は」が内容に残らない
    ("今日は資料を送る", "資料を送る", date(2026, 9, 20)),
    ("9/25は会議の準備", "会議の準備", date(2026, 9, 25)),
    ("明日は雨", "雨", date(2026, 9, 21)),
])
def test_scheduled_date_phrases(text, content, scheduled):
    assert ex(text) == (content, scheduled, None)


@pytest.mark.parametrize("text,content,due", [
    ("明日までに資料を送る", "資料を送る", date(2026, 9, 21)),
    ("資料を送る 明日まで", "資料を送る", date(2026, 9, 21)),
    ("企画書 期限は9月30日", "企画書", date(2026, 9, 30)),
    ("企画書 締切：10/5", "企画書", date(2026, 10, 5)),
    ("〆切は明後日 企画書", "企画書", date(2026, 9, 22)),
    ("請求書を出す 月末までに", "請求書を出す", date(2026, 9, 30)),
    ("報告書 来月末まで", "報告書", date(2026, 10, 31)),
    ("2026-10-01までに提出", "提出", date(2026, 10, 1)),
    ("25日まで 書類", "書類", date(2026, 9, 25)),
])
def test_due_date_phrases(text, content, due):
    assert ex(text) == (content, None, due)


def test_next_week_is_the_week_after_this_monday_start_week():
    monday = date(2026, 9, 21)  # 今日が月曜日なら、来週の月曜は7日後
    assert td.extract("来週の月曜に会議", monday).scheduled == date(2026, 9, 28)
    assert td.extract("来週の日曜に会議", monday).scheduled == date(2026, 10, 4)


def test_scheduled_and_due_in_one_message():
    # 週は月曜始まり。今日が日曜日なら、「来週の月曜」は明日
    assert ex("レポートを書く 来週の月曜にやる 期限は金曜まで") == ("レポートを書く", date(2026, 9, 21), date(2026, 9, 25))
    assert ex("企画書 今日やる 期限は10/5") == ("企画書", date(2026, 9, 20), date(2026, 10, 5))


@pytest.mark.parametrize("text,expected", [
    ("月曜に会議", date(2026, 9, 21)),       # 日曜日に「月曜」→ 次の月曜
    ("日曜にやる", date(2026, 9, 27)),        # 今日と同じ曜日 → 来週の同じ曜日
    ("金曜までに", None),                     # （期限として下で確認）
    ("来週の月曜に会議", date(2026, 9, 21)),      # 週は月曜始まり: 日曜日の「来週の月曜」は明日
    ("来週水曜に会議", date(2026, 9, 23)),
    ("再来週の金曜に会議", date(2026, 10, 2)),
    ("今週の金曜に会議", date(2026, 9, 25)),  # 今週の金曜は過ぎている（月曜始まりの週）ので次の週
    ("水曜日にやる", date(2026, 9, 23)),
])
def test_weekdays(text, expected):
    r = td.extract(text, TODAY)
    if expected is None:
        assert r.due == date(2026, 9, 25) and r.scheduled is None
    else:
        assert r.scheduled == expected


def test_past_month_day_means_next_year_and_day_of_month_rolls_over():
    assert ex("9/10に更新")[1] == date(2027, 9, 10)
    assert ex("10日に振込")[1] == date(2026, 10, 10)  # 今日は20日 → 来月10日
    assert ex("20日に振込")[1] == date(2026, 9, 20)   # 今日は当日
    assert td.extract("15日に提出", date(2026, 12, 20)).scheduled == date(2027, 1, 15)  # 年をまたぐ


def test_month_end_handles_february_and_leap_years():
    assert td.extract("月末までに", date(2028, 2, 10)).due == date(2028, 2, 29)
    assert td.extract("来月末まで", date(2026, 1, 31)).due == date(2026, 2, 28)


@pytest.mark.parametrize("text", [
    "10/5のライブのチケットを取る",      # 日付は名詞の一部（の が続く）
    "明日のお弁当を考える",              # 「明日の」＋名詞
    "毎週月曜にゴミ出し",                # 繰り返し
    "1日3回薬を飲む",                     # 回数
    "3日間で終わらせる",                  # 期間
    "2/30に予約",                         # 存在しない日付
    "13月5日にやる",
    "日曜大工の道具を買う",               # 曜日の直後に漢字が続く名詞
    "月曜会議の資料を作る",
])
def test_things_that_are_not_task_dates_are_left_alone(text):
    content, scheduled, due = ex(text)
    assert scheduled is None and due is None
    assert content == text


@pytest.mark.parametrize("text,content,due", [
    ("今日中に資料を送る", "資料を送る", date(2026, 9, 20)),
    ("明日中にやる 請求書", "やる 請求書", date(2026, 9, 21)),
    ("今週中に企画書", "企画書", date(2026, 9, 20)),      # 今日は日曜日 = 今週の最終日
    ("来週中に企画書", "企画書", date(2026, 9, 27)),
    ("今月中に契約", "契約", date(2026, 9, 30)),
    ("来月中に契約", "契約", date(2026, 10, 31)),
])
def test_within_period_phrases_are_deadlines(text, content, due):
    assert ex(text) == (content, None, due)


def test_time_of_day_after_the_date_is_part_of_the_date_phrase():
    assert ex("明日の夜に電話") == ("電話", date(2026, 9, 21), None)
    assert ex("明日の午後までに提出") == ("提出", None, date(2026, 9, 21))


def test_first_scheduled_and_first_due_win_and_others_stay_in_the_text():
    content, scheduled, due = ex("明日やる 資料 明後日にも見直し")
    assert scheduled == date(2026, 9, 21) and "明後日" in content


def test_date_only_message_keeps_the_original_text():
    r = td.extract("明日", TODAY)
    assert r.content == "明日" and r.scheduled == date(2026, 9, 21)
    r = td.extract("期限は9/30", TODAY)
    assert r.content and r.due == date(2026, 9, 30)


def test_no_date_returns_text_unchanged():
    assert ex("牛乳を買う") == ("牛乳を買う", None, None)
    assert ex("") == ("", None, None)


def test_describe_and_fmt():
    assert td.fmt_md(date(2026, 9, 21)) == "9/21(月)"
    assert td.describe(date(2026, 9, 21), date(2026, 9, 30)) == "実行日 9/21(月)・期限 9/30(水)"
    assert td.describe(None, date(2026, 9, 30)) == "期限 9/30(水)"
    assert td.describe(None, None) == ""

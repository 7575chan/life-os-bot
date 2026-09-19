"""フェーズ 1 の純粋なロジック: 見出しの判定、朝の案内、夜の選択、体調の集計、ご機嫌度の解析。"""
from datetime import date, datetime

import config
import health_analysis as ha
import sheets
import util
from handlers import health, looking_back, today_task

T = sheets.TASKS  # 01-today-task


# ---------------------------------------------------------------- シート名と見出し（SPEC §3.2）


def test_sheet_names_equal_channel_names():
    assert set(sheets.SCHEMA) == set(config.CHANNELS.values())
    assert len(sheets.SCHEMA) == 14
    assert sheets.TASKS == "01-today-task" and sheets.OPLOG == "14-ai"


def test_project_rooms_have_separate_sheets():
    names = list(sheets.PROJECT_SHEETS.values())
    assert names == ["10-project-novel", "11-project-trpg", "12-project-others"] and len(set(names)) == 3
    assert all(sheets.SCHEMA[n] == ["日時", "作品名", "種別", "内容"] for n in names)


def test_header_empty_sheet_gets_header():
    assert sheets.plan_header(T, [], False) == ("write", sheets.SCHEMA[T])
    assert sheets.plan_header(T, ["", ""], False) == ("write", sheets.SCHEMA[T])


def test_header_title_only_in_a1_is_replaced():
    assert sheets.plan_header("08-scrap", ["08-scrap"], False) == ("write", sheets.SCHEMA["08-scrap"])


def test_header_never_overwrites_user_data():
    assert sheets.plan_header(T, [], True)[0] == "refuse"  # 見出し無しでデータがある
    assert sheets.plan_header(T, ["08-scrap"], True)[0] == "refuse"
    assert sheets.plan_header(T, ["名前", "メモ"], False)[0] == "refuse"  # 関係ない列


def test_header_existing_ok_or_missing_columns_appended():
    cols = sheets.SCHEMA[T]
    assert sheets.plan_header(T, list(cols), True) == ("ok", [])
    assert sheets.plan_header(T, list(reversed(cols)), True) == ("ok", [])  # 順序違いは許容
    assert sheets.plan_header(T, cols[:6] + ["自分用の列"], True) == ("append", cols[6:])


def test_task_sort_key_priority_due_row():
    a = {"priority": "", "due": "", "row": 2}
    b = {"priority": "高", "due": "2026-09-30", "row": 5}
    c = {"priority": "高", "due": "2026-09-20", "row": 9}
    d = {"priority": "", "due": "2026-09-01", "row": 3}
    assert sorted([a, b, c, d], key=sheets.task_sort_key) == [c, b, d, a]


# ---------------------------------------------------------------- 朝の案内


def tasks_(n):
    return [{"id": f"lo-{i}", "content": f"タスク{i}"} for i in range(1, n + 1)]


def test_main_max_three_then_sub_numbers_continue():
    text = today_task.format_task_list(tasks_(5))
    assert "本日のメインタスク（最大3つ）：\n1. タスク1\n2. タスク2\n3. タスク3" in text
    assert "サブタスク（気分に合わせて）：\n4. タスク4\n5. タスク5" in text


def test_only_main_when_three_or_fewer():
    text = today_task.format_task_list(tasks_(2))
    assert "サブタスク" not in text and "2. タスク2" in text


def test_morning_composition_with_and_without_extras():
    full = today_task.compose_morning(tasks_(1), "📊 昨日の執筆実績\n・合計 +1,850文字", "🩺 昨日: 体調スコア 4 / ご機嫌度 4")
    assert full.index("📊") < full.index("🩺") < full.index("本日のメインタスク")
    assert "3日" in full
    empty = today_task.compose_morning([], None, None)
    assert "まだありません" in empty and "📊" not in empty and "遅れ" not in empty


def test_health_line():
    assert today_task.health_line(4, "4") == "🩺 昨日: 体調スコア 4 / ご機嫌度 4"
    assert today_task.health_line(None, None) is None
    assert today_task.health_line(None, "3") == "🩺 昨日: ご機嫌度 3"


def test_urgent_task_lines_strip_bullets_and_blank_lines():
    assert today_task._lines("- 買い物\n・電話する\n\n[ ] 書類\n普通の行") == ["買い物", "電話する", "書類", "普通の行"]


def test_done_command_pattern():
    m = today_task._DONE.match("完了 1,3")
    assert m and util.parse_numbers(m.group(1)) == [1, 3]
    assert today_task._DONE.match("完了1と2")
    assert today_task._DONE.match("買い物に行く") is None


# ---------------------------------------------------------------- 夜の振り返り


def test_selection_target_day_boundary():
    tz = config.TZ
    assert looking_back.selection_target_day(datetime(2026, 9, 19, 22, 0, tzinfo=tz)) == date(2026, 9, 20)
    assert looking_back.selection_target_day(datetime(2026, 9, 20, 1, 30, tzinfo=tz)) == date(2026, 9, 20)
    assert looking_back.selection_target_day(datetime(2026, 9, 20, 5, 0, tzinfo=tz)) == date(2026, 9, 21)


def test_candidates_text_is_gentle_and_numbered():
    text = looking_back.format_candidates([{"content": "A"}, {"content": "B"}])
    assert "1. A\n2. B" in text and "「なし」" in text


def test_fallback_when_ai_is_unavailable():
    fb = looking_back._fallback("4 今日は疲れた。Xのポスト案もほしい")
    assert fb["mood"] == 4 and fb["wants_x"] and not fb["wants_note"] and fb["tomorrow_tasks"] == []
    assert fb["formatted"].startswith("4 今日")


def test_mood_parsing():
    assert util.parse_mood("4 今日は疲れた") == 4
    assert util.parse_mood("ご機嫌度3。頭が重い") == 3
    assert util.parse_mood("機嫌：５") == 5
    assert util.parse_mood("今日は5つやった") is None
    assert util.parse_mood("2026年の話") is None
    assert util.parse_mood("6 は範囲外") is None
    assert util.clamp_int("4", 1, 5) == 4 and util.clamp_int(9, 1, 5) is None and util.clamp_int(None, 1, 5) is None


# ---------------------------------------------------------------- 体調


def test_parse_sleep_hours():
    assert ha.parse_sleep_hours("10.5") == 10.5
    assert ha.parse_sleep_hours("10.5時間") == 10.5
    assert ha.parse_sleep_hours("7時間30分") == 7.5
    assert ha.parse_sleep_hours("10時間15分") == 10.25
    assert ha.parse_sleep_hours("") is None and ha.parse_sleep_hours("たくさん") is None
    assert ha.parse_sleep_hours("30") is None  # 24時間を超える値は睡眠時間ではない


def test_sleep_mood_table_splits_by_threshold():
    health_rows = [{"日時": "2026-09-01 08:00", "睡眠時間": "10", "体調スコア": "5"},
                   {"日時": "2026-09-02 08:00", "睡眠時間": "6.5", "体調スコア": "2"},
                   {"日時": "2026-09-03 08:00", "睡眠時間": "8", "体調スコア": "3"},
                   {"日時": "2026-09-04 08:00", "睡眠時間": "", "体調スコア": "1"}]
    diary_rows = [{"日付": "2026-09-01", "ご機嫌度": "5"}, {"日付": "2026-09-02", "ご機嫌度": "2"},
                  {"日付": "2026-09-03", "ご機嫌度": "3"}]
    t = ha.sleep_mood_table(health_rows, diary_rows)
    assert t["over"] == {"days": 1, "avg_mood": 5.0, "avg_score": 5.0}
    assert t["under"] == {"days": 2, "avg_mood": 2.5, "avg_score": 2.5}


def test_sleep_mood_table_handles_no_data():
    t = ha.sleep_mood_table([], [])
    assert t["under"]["days"] == 0 and t["under"]["avg_mood"] is None


def test_objective_and_steps_text():
    obj = {"stress": 25, "resting_heart_rate": 52, "deep_sleep_hours": 1.5, "steps": 2400, "exercise": "散歩"}
    assert health.objective_text(obj) == "ストレス 25 / 安静時心拍 52 / 深い睡眠 1.5時間"
    assert health.steps_text(obj) == "2,400歩 / 散歩"
    assert health.objective_text({}) == "" and health.steps_text({}) == ""
    assert health._hours_text(10.25) == "10.25" and health._hours_text(9.0) == "9" and health._hours_text(None) == ""


# ---------------------------------------------------------------- 別の場所が先に投稿済みなら重複しない


def test_already_posted_detects_same_day_bot_post():
    from types import SimpleNamespace as NS

    import scheduler

    tz = config.TZ
    day = date(2026, 9, 20)

    def msg(author_id, content, when):
        return NS(author=NS(id=author_id), content=content, created_at=when)

    early = datetime(2026, 9, 20, 8, 0, tzinfo=tz)
    prefix = today_task.MORNING_PREFIX
    assert scheduler.already_posted([msg(1, prefix + " ☀️\n…", early)], 1, prefix, day)
    assert not scheduler.already_posted([msg(2, prefix + " ☀️", early)], 1, prefix, day)  # 他人の投稿
    assert not scheduler.already_posted([msg(1, "別の投稿", early)], 1, prefix, day)
    yesterday = datetime(2026, 9, 19, 8, 0, tzinfo=tz)
    assert not scheduler.already_posted([msg(1, prefix, yesterday)], 1, prefix, day)  # 前日の投稿
    # UTC の時刻でも、JST の日付で判定する（JST 08:00 = UTC 前日 23:00）
    from datetime import timezone

    assert scheduler.already_posted([msg(1, prefix, datetime(2026, 9, 19, 23, 0, tzinfo=timezone.utc))], 1, prefix, day)


def test_prefixes_are_used_in_the_actual_messages():
    assert today_task.compose_morning([], None, None).startswith(today_task.MORNING_PREFIX)
    assert looking_back.EVENING_PREFIX in "今日もお疲れ様でした！ 本日の記録を残しましょう🌙"

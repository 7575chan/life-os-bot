"""01-today-task: 今日のタスクの表示と、メイン（優先度「高」・最大3件）/ サブの自由な設定（SPEC §5）。"""
import asyncio
from datetime import date
from types import SimpleNamespace as NS

import pytest

import sheets
import state
import task_sync
import util
from handlers import today_task

TODAY = date(2026, 9, 20)


def run(coro):
    return asyncio.run(coro)


def task(i, priority="", due=""):
    return {"id": f"lo-{i}", "content": f"タスク{i}", "priority": priority, "due": due, "row": i + 1,
            "done": False, "deleted": False, "scheduled": "2026-09-20"}


# ---------------------------------------------------------------- 並びと表示（純粋）


def test_main_is_only_high_priority_and_numbers_continue():
    tasks = [task(1), task(2, "高"), task(3, "中"), task(4, "高", "2026-09-22"), task(5, "高", "2026-09-21")]
    main, sub, overflow = today_task.arrange(tasks)
    assert [t["id"] for t in main] == ["lo-5", "lo-4", "lo-2"]  # 期限が近い順 → 登録順
    assert [t["id"] for t in sub] == ["lo-3", "lo-1"]  # 中 → 空
    assert not overflow
    text = today_task.format_task_list(tasks)
    assert "本日のメインタスク（最大3つ）：\n1. タスク5\n2. タスク4\n3. タスク2" in text
    assert "サブタスク（気分に合わせて）：\n4. タスク3\n5. タスク1" in text


def test_no_high_means_no_main_section_and_numbers_start_at_one():
    text = today_task.format_task_list([task(1), task(2, "低")])
    assert "メインタスク" not in text
    assert text.startswith("サブタスク（気分に合わせて）：\n1. タスク2\n2. タスク1")  # 低 → 空


def test_fewer_than_three_high_does_not_fill_main_automatically():
    main, sub, _ = today_task.arrange([task(1), task(2, "高")])
    assert [t["id"] for t in main] == ["lo-2"] and [t["id"] for t in sub] == ["lo-1"]
    assert [t["id"] for t in today_task.ordered([task(1), task(2, "高")])] == ["lo-2", "lo-1"]


def test_sort_key_orders_middle_and_low_before_blank():
    ts = [task(1), task(2, "低"), task(3, "中"), task(4, "高")]
    assert [t["id"] for t in sorted(ts, key=sheets.task_sort_key)] == ["lo-4", "lo-3", "lo-2", "lo-1"]


@pytest.mark.parametrize("text", ["今日のタスク", "タスク", "一覧", "リスト", " 今日のタスク？ ", "タスクを見せて", "一覧を表示"])
def test_show_words(text):
    assert today_task._SHOW.match(text)


@pytest.mark.parametrize("text", ["タスクを整理する", "リストを作る", "今日のタスクは多い"])
def test_show_words_do_not_catch_tasks(text):
    assert not today_task._SHOW.match(text)


def test_plan_task_main_slots_and_kinds():
    assert today_task.plan_task("牛乳", TODAY, main_free=0)["priority"] == ""
    assert today_task.plan_task("牛乳", TODAY, main_free=0)["demoted"] is True
    assert today_task.plan_task("牛乳", TODAY, main_free=1)["priority"] == "高"
    assert today_task.plan_task("牛乳", TODAY, kind="サブ")["priority"] == ""
    assert today_task.plan_task("牛乳", TODAY, kind="サブ")["demoted"] is False
    future = today_task.plan_task("レポート 明日やる", TODAY, kind="メイン")
    assert (future["priority"], future["future"]) == ("高", True)
    assert today_task.plan_task("レポート 明日やる", TODAY)["priority"] == ""


# ---------------------------------------------------------------- ハンドラ


@pytest.fixture
def env(monkeypatch):
    e = NS(tasks=[], added=[], sent=[], reactions=[], set_calls=[], pending=None, synced=0)
    monkeypatch.setattr(util, "today", lambda: TODAY)
    monkeypatch.setattr(sheets, "today_tasks", lambda day: [dict(t) for t in e.tasks])

    def add_task(content, **kw):
        e.added.append((content, kw))
        return "lo-new"

    def set_priority(ids, priority):
        e.set_calls.append((ids, priority))
        changed = []
        for t in e.tasks:
            if t["id"] in ids and t["priority"] != priority:
                t["priority"] = priority
                changed.append(t["content"])
        return changed

    def sync():
        e.synced += 1

    monkeypatch.setattr(sheets, "add_task", add_task)
    monkeypatch.setattr(sheets, "set_priority", set_priority)
    monkeypatch.setattr(task_sync, "run_safely", sync)
    monkeypatch.setattr(state, "put_pending", lambda mid, cid, kind, payload: setattr(e, "pending", payload))
    monkeypatch.setattr(state, "latest_pending", lambda cid, kind, max_age_hours: {"payload": e.pending} if e.pending else None)

    async def send(text, reference=None):
        e.sent.append(text)
        return NS(id=len(e.sent))

    async def react(emoji):
        e.reactions.append(emoji)

    e.channel = NS(id=10, send=send)
    e.message = lambda text: NS(content=text, channel=e.channel, add_reaction=react)
    return e


def test_show_posts_list_without_adding_a_task(env):
    env.tasks = [task(1, "高"), task(2)]
    run(today_task.handle(env.message("今日のタスク")))
    assert env.added == []
    assert "1. タスク1" in env.sent[0] and "2. タスク2" in env.sent[0] and "メイン 4" in env.sent[0]
    assert [i["id"] for i in env.pending["items"]] == ["lo-1", "lo-2"]


def test_show_when_empty(env):
    run(today_task.handle(env.message("タスク")))
    assert env.added == [] and "今のところありません" in env.sent[0]


def test_make_main_uses_numbers_of_the_latest_list(env):
    env.tasks = [task(1, "高"), task(2), task(3)]
    run(today_task.handle(env.message("一覧")))  # 1=タスク1(メイン) 2=タスク2 3=タスク3
    run(today_task.handle(env.message("メイン 3")))
    assert env.set_calls == [(["lo-3"], "高")]
    assert env.sent[-1].startswith("メインにしました：\n・タスク3")
    assert "本日のメインタスク（最大3つ）：\n1. タスク1\n2. タスク3" in env.sent[-1]  # 番号を振り直した一覧
    assert env.synced == 1 and env.reactions == ["🔀"]


def test_make_main_over_three_changes_nothing(env):
    env.tasks = [task(1, "高"), task(2, "高"), task(3, "高"), task(4), task(5)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("メイン 4,5")))
    assert env.set_calls == [] and env.synced == 0
    assert "メインは3件までです" in env.sent[-1]


def test_swap_after_making_room(env):
    env.tasks = [task(1, "高"), task(2, "高"), task(3, "高"), task(4)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("サブ 1")))
    assert env.set_calls == [(["lo-1"], "")]
    run(today_task.handle(env.message("メイン 4")))  # 最新の一覧: 1=タスク2 2=タスク3 3=タスク1 4=タスク4
    assert env.set_calls[-1] == (["lo-4"], "高")


def test_sub_on_a_sub_task_is_left_as_is(env):
    env.tasks = [task(1, "高"), task(2, "中")]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("サブ 2")))
    assert env.set_calls == [] and env.tasks[1]["priority"] == "中"
    assert env.sent[-1].startswith("すでにサブになっています。")


def test_main_on_an_existing_main_is_not_an_error(env):
    env.tasks = [task(1, "高"), task(2)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("メイン：1")))
    assert env.sent[-1].startswith("すでにメインになっています。")


def test_unknown_number(env):
    env.tasks = [task(1, "高")]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("メイン 9")))
    assert env.set_calls == [] and "見当たりません" in env.sent[-1]


def test_new_tasks_fill_free_main_slots_then_go_to_sub(env):
    env.tasks = [task(1, "高"), task(2, "高")]
    run(today_task.handle(env.message("牛乳を買う\n電話する")))
    assert [(c, kw["priority"]) for c, kw in env.added] == [("牛乳を買う", "高"), ("電話する", "")]
    assert "サブに入れました" in env.sent[-1]


def test_new_tasks_with_kind_prefix(env):
    env.tasks = [task(1, "高"), task(2, "高"), task(3, "高")]
    run(today_task.handle(env.message("サブ：洗濯\nメイン：企画書")))
    assert [(c, kw["priority"]) for c, kw in env.added] == [("洗濯", ""), ("企画書", "")]  # メインが満杯なのでサブに（登録は失わない）
    assert "サブに入れました" in env.sent[-1]


def test_kind_word_inside_task_text_is_just_a_task(env):
    run(today_task.handle(env.message("メインディッシュを作る")))
    assert [(c, kw["priority"]) for c, kw in env.added] == [("メインディッシュを作る", "高")]


# ---------------------------------------------------------------- 簡単な書き方（1行・1メッセージで複数指定・入れ替え）


@pytest.mark.parametrize("text, cmds", [
    ("メイン2", [("メイン", [2])]),
    ("メイン 2 サブ 5", [("メイン", [2]), ("サブ", [5])]),
    ("メイン2・サブ5", [("メイン", [2]), ("サブ", [5])]),
    ("メイン２、サブ５", [("メイン", [2]), ("サブ", [5])]),
    ("メイン 2,3", [("メイン", [2, 3])]),
    ("2をメインに", [("メイン", [2])]),
    ("2番をサブにして", [("サブ", [2])]),
    ("2番はサブ", [("サブ", [2])]),
    ("2をメインに、5をサブにしてください", [("メイン", [2]), ("サブ", [5])]),
    ("2と5を入れ替え", [("入れ替え", [2, 5])]),
    ("入れ替え 2 5", [("入れ替え", [2, 5])]),
    ("メインタスク 4", [("メイン", [4])]),
])
def test_parse_kind_commands(text, cmds):
    assert today_task.parse_kind_commands(text) == cmds


@pytest.mark.parametrize("text", ["メインディッシュを作る", "3時にメインの資料を送る", "サブスクを解約する", "牛乳",
                                  "サブ：洗濯", "2人で会議"])
def test_parse_kind_commands_ignores_tasks(text):
    assert today_task.parse_kind_commands(text) is None


def test_line_kind_suffix_and_prefix():
    assert today_task.line_kind("洗濯（サブ）") == ("サブ", "洗濯")
    assert today_task.line_kind("洗濯 #サブ") == ("サブ", "洗濯")
    assert today_task.line_kind("企画書【メイン】") == ("メイン", "企画書")
    assert today_task.line_kind("サブ：洗濯") == ("サブ", "洗濯")
    assert today_task.line_kind("洗濯") == (None, "洗濯")


def test_main_and_sub_in_one_message_swaps_even_when_main_is_full(env):
    env.tasks = [task(1, "高"), task(2, "高"), task(3, "高"), task(4), task(5)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("メイン 4 サブ 1")))
    assert env.set_calls == [(["lo-1"], ""), (["lo-4"], "高")]
    assert "メインにしました：\n・タスク4" in env.sent[-1] and "サブにしました：\n・タスク1" in env.sent[-1]
    assert env.added == [] and env.reactions == ["🔀"] and env.synced == 1


def test_no_space_command_is_not_registered_as_a_task(env):
    env.tasks = [task(1, "高"), task(2)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("メイン2・サブ1")))
    assert env.added == []
    assert [t["priority"] for t in env.tasks] == ["", "高"]


def test_swap_flips_each_number(env):
    env.tasks = [task(1, "高"), task(2, "高"), task(3, "高"), task(4)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("1と4を入れ替えて")))
    assert [t["priority"] for t in env.tasks] == ["", "高", "高", "高"]


def test_natural_form_over_three_changes_nothing(env):
    env.tasks = [task(1, "高"), task(2, "高"), task(3, "高"), task(4)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("4をメインにして")))
    assert env.set_calls == [] and "何も変えていません" in env.sent[-1] and "メイン 4 サブ 1" in env.sent[-1]


def test_missing_number_is_noted_but_others_apply(env):
    env.tasks = [task(1, "高"), task(2)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("メイン 2 サブ 9")))
    assert env.set_calls == [(["lo-2"], "高")] and "9番は見当たりませんでした" in env.sent[-1]


def test_unreadable_command_is_not_a_task(env):
    env.tasks = [task(1, "高"), task(2)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("メイン2 1をサブ")))
    assert env.added == [] and env.set_calls == [] and "読み取れませんでした" in env.sent[-1]


def test_register_as_sub_by_suffix_and_header_line(env):
    run(today_task.handle(env.message("企画書\n洗濯（サブ）\nサブ：\n買い物\n電話")))
    assert [(c, kw["priority"]) for c, kw in env.added] == [("企画書", "高"), ("洗濯", ""), ("買い物", ""), ("電話", "")]
    assert "・洗濯（サブ）" in env.sent[-1]


def test_command_and_new_task_in_one_message(env):
    env.tasks = [task(1, "高"), task(2)]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("サブ 1\n牛乳を買う")))
    assert env.set_calls == [(["lo-1"], "")]
    assert env.added[0][0] == "牛乳を買う" and env.added[0][1]["priority"] == "高"  # 空いたメイン枠に入る
    assert "サブにしました" in env.sent[-1] and "今日のタスクに追加しました" in env.sent[-1]
    assert env.reactions == ["🔀", "📝"]

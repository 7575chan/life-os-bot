"""01-today-task の週の計画・移す・今日は休む・着手・最初の5分（SPEC §5「週の計画」）。"""
import asyncio
from datetime import date
from types import SimpleNamespace as NS

import pytest

import claude_client
import sheets
import state
import task_sync
import util
import works
from handlers import today_task, week_plan as wp

SAT = date(2026, 10, 17)  # 土曜
WED = date(2026, 10, 14)


def run(coro):
    return asyncio.run(coro)


def t(i, scheduled="", due="", priority="", source="discord", done=False):
    return {"id": f"lo-{i}", "content": f"タスク{i}", "scheduled": scheduled, "due": due, "priority": priority,
            "source": source, "done": done, "deleted": False, "row": i + 1}


# ---------------------------------------------------------------- 純粋関数


def test_plan_days_and_labels():
    nxt = wp.plan_days(SAT, this_week=False)
    assert nxt[0] == date(2026, 10, 19) and nxt[-1] == date(2026, 10, 25) and len(nxt) == 7
    assert wp.plan_days(WED, this_week=True) == [date(2026, 10, d) for d in range(14, 19)]
    assert wp.label(date(2026, 10, 19)) == "月 10/19"


@pytest.mark.parametrize("word,expect", [
    ("今日", WED), ("明日", date(2026, 10, 15)), ("あさって", date(2026, 10, 16)),
    ("来週", date(2026, 10, 19)), ("棚", ""), ("金", date(2026, 10, 16)), ("金曜", date(2026, 10, 16)),
    ("水", date(2026, 10, 21)), ("10/20", date(2026, 10, 20)), ("明日に", date(2026, 10, 15)), ("どこか", None),
])
def test_parse_target(word, expect):
    assert wp.parse_target(word, WED) == expect


def test_next_weekday_skips_weekend():
    assert wp.next_weekday(date(2026, 10, 16)) == date(2026, 10, 19)
    assert wp.next_weekday(WED) == date(2026, 10, 15)


def test_build_fills_weekdays_three_a_day_and_keeps_weekend_as_buffer():
    days = wp.plan_days(SAT, this_week=False)
    tasks = ([t(1, scheduled="2026-10-21")]  # 決まっている予定はその日
             + [t(2, scheduled="2026-10-10")]  # 持ち越し
             + [t(3, due="2026-10-20")]  # 期限が週の中
             + [t(10 + i) for i in range(20)]  # 棚
             + [t(50, scheduled="2026-11-02"), t(51, done=True)])  # 先の予定・完了は入れない
    plan = wp.build(tasks, days, SAT)
    ids = [it["id"] for it in plan["items"]]
    assert "lo-50" not in ids and "lo-51" not in ids
    grid = plan["grid"]
    for iso in plan["days"][:5]:
        assert len(grid[iso]) <= 3
    assert grid["2026-10-24"] == [] and grid["2026-10-25"] == []  # 土日はバッファ
    first_mon = [plan["items"][i]["id"] for i in grid["2026-10-19"]]
    assert first_mon[:2] == ["lo-2", "lo-3"]  # 持ち越し → 期限の近いもの
    assert "lo-1" in [plan["items"][i]["id"] for i in grid["2026-10-21"]]
    seq = [i for iso in plan["days"] for i in grid[iso]] + plan["extra"]
    assert seq == list(range(len(plan["items"])))  # 番号は表の順
    text = wp.render(plan, "見出し")
    assert "**月 10/19**" in text and "**土 10/24**（バッファ）" in text and "入らなかった候補" in text


def test_build_round_robins_projects():
    days = wp.plan_days(SAT, this_week=False)
    src_a, src_b = works.SOURCE_PREFIX + "1", works.SOURCE_PREFIX + "2"
    tasks = [t(1, source=src_a), t(2, source=src_a), t(3, source=src_b)]
    plan = wp.build(tasks, days, SAT)
    assert [it["id"] for it in plan["items"]] == ["lo-1", "lo-3", "lo-2"]


def test_apply_edits():
    days = wp.plan_days(SAT, this_week=False)
    plan = wp.build([t(i) for i in range(1, 18)], days, SAT)  # 15個入って、2個あふれる
    assert len(plan["extra"]) == 2
    p2, err = wp.apply_edits(plan, "1 木、16 外す")
    assert err is None
    assert 0 in p2["grid"]["2026-10-22"] and 0 not in p2["grid"]["2026-10-19"]
    assert 15 in p2["extra"]
    p3, err = wp.apply_edits(p2, "+16")
    assert err is None and 15 in p2["grid"]["2026-10-19"] or 15 in [i for v in p3["grid"].values() for i in v]
    assert wp.apply_edits(plan, "99 木")[1]
    assert wp.apply_edits(plan, "原稿を書く") is None


def test_sleeping_picks_near_finished_projects():
    ps = [{"now": False, "status": "製作中", "kind": "小説", "title": "A"},
          {"now": False, "status": "完成間際", "kind": "小説", "title": "B"},
          {"now": True, "status": "完成間際", "kind": "小説", "title": "C"},
          {"now": False, "status": "完了", "kind": "小説", "title": "D"}]
    got = wp.sleeping([dict(p, updated="", chars=None, target=None, stage="") for p in ps])
    assert [p["title"] for p in got] == ["B", "A"]


# ---------------------------------------------------------------- Discord


@pytest.fixture
def env(monkeypatch):
    e = NS(tasks=[], sent=[], reactions=[], pending={}, kv={}, updates=[], rows=[], synced=0)
    monkeypatch.setattr(util, "today", lambda: SAT)
    monkeypatch.setattr(util, "activity_day", lambda at=None: SAT)
    monkeypatch.setattr(sheets, "all_tasks", lambda: [dict(x) for x in e.tasks])
    monkeypatch.setattr(sheets, "today_tasks", lambda day: [dict(x) for x in e.tasks if x["scheduled"] and x["scheduled"] <= day.isoformat() and not x["done"]])
    monkeypatch.setattr(sheets, "update_task", lambda tid, **f: e.updates.append((tid, f)) or True)
    monkeypatch.setattr(sheets, "update_rows", lambda name, ups: e.rows.extend(ups))
    monkeypatch.setattr(task_sync, "run_safely", lambda: setattr(e, "synced", e.synced + 1))
    monkeypatch.setattr(works, "enabled", lambda: False)

    def put(mid, cid, kind, payload):
        e.pending[kind] = {"message_id": mid, "kind": kind, "payload": payload}

    monkeypatch.setattr(state, "put_pending", put)
    monkeypatch.setattr(state, "get_pending", lambda mid: None)
    monkeypatch.setattr(state, "latest_pending", lambda cid, kind, max_age_hours: e.pending.get(kind))
    monkeypatch.setattr(state, "delete_pending", lambda mid: [e.pending.pop(k) for k, v in list(e.pending.items()) if v["message_id"] == mid])
    monkeypatch.setattr(state, "get_kv", lambda k, d=None: e.kv.get(k, d))
    monkeypatch.setattr(state, "set_kv", lambda k, v: e.kv.__setitem__(k, v))

    async def send(text, reference=None):
        e.sent.append(text)
        return NS(id=len(e.sent))

    async def react(emoji):
        e.reactions.append(emoji)

    e.channel = NS(id=10, send=send)
    e.message = lambda text: NS(content=text, channel=e.channel, add_reaction=react, reference=None)
    return e


def test_week_plan_flow_edit_and_ok(env):
    env.tasks = [t(1), t(2), t(3, scheduled="2026-10-12")]
    run(today_task.handle(env.message("来週の計画")))
    assert "来週の計画の案" in env.sent[-1] and "**月 10/19**" in env.sent[-1]
    assert env.rows == []  # 案の段階では何も変えない
    run(today_task.handle(env.message("1 外す")))  # 1番 = 持ち越し（タスク3）
    assert "直しました" in env.sent[-1]
    run(today_task.handle(env.message("OK")))
    ids_rows = {r: v["実行予定日"] for r, v in env.rows}
    assert ids_rows[4] == "" and ids_rows[2] == "2026-10-19"  # 外した持ち越しは棚へ
    assert wp.planned(date(2026, 10, 19)) and env.synced == 1 and env.reactions == ["🗓"]


def test_week_plan_cancel_changes_nothing(env):
    env.tasks = [t(1)]
    run(today_task.handle(env.message("今週の計画")))
    run(today_task.handle(env.message("やめる")))
    assert env.rows == [] and "何も変えていません" in env.sent[-1]


def test_move_uses_today_list_numbers(env):
    env.tasks = [t(1, scheduled="2026-10-15"), t(2, scheduled="2026-10-17")]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("移す 1 月")))
    assert env.updates[0][1] == {"scheduled": "2026-10-19"}
    assert "10/19" in env.sent[-1] and env.reactions == ["📆"]
    run(today_task.handle(env.message("移す 1 どこか")))
    assert "読めませんでした" in env.sent[-1]


def test_rest_today_choices(env):
    env.tasks = [t(1, scheduled="2026-10-17")]
    run(today_task.handle(env.message("今日は休む")))
    assert "ゆっくり休んで" in env.sent[-1] and "1. 明日" in env.sent[-1]
    run(today_task.handle(env.message("2")))
    assert env.updates == [("lo-1", {"scheduled": "2026-10-19"})]  # 土曜 → 次の平日は月曜
    run(today_task.handle(env.message("今日は休む")))
    run(today_task.handle(env.message("4")))
    assert len(env.updates) == 1 and "そのまま" in env.sent[-1]


def test_start_sets_start_date(env):
    env.tasks = [t(1, scheduled="2026-10-17")]
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("着手 1")))
    assert env.updates == [("lo-1", {"start": "2026-10-17"})] and "🌱" in env.sent[-1]


def test_first_five_minutes(env, monkeypatch):
    env.tasks = [t(1, scheduled="2026-10-17")]

    async def fake(prompt, default):
        assert "タスク1" in prompt
        return {"step": "ファイルを開く"}

    monkeypatch.setattr(claude_client, "complete_json", fake)
    run(today_task.handle(env.message("一覧")))
    run(today_task.handle(env.message("どうしよう")))
    assert env.sent[-1].startswith("まずは5分だけ: ファイルを開く")


def test_morning_mentions_carry_over_and_monday_plan_hint():
    carry = [{**t(1, scheduled="2026-10-16"), "priority": ""}]
    text = today_task.compose_morning(carry, None, day=SAT)
    assert "移す 番号 明日" in text and "3日" not in text
    assert "移す" not in today_task.compose_morning([t(2, scheduled="2026-10-17")], None, day=SAT)
    assert "今週の計画" in today_task.compose_morning([], None, day=date(2026, 10, 19), plan_hint=True)


# ---------------------------------------------------------------- 圧倒されない一覧（§10 の 44）


def test_to_decide_splits_and_hides_future():
    every = [t(1, scheduled="2026-10-17"), t(2, scheduled="2026-10-25"), t(3, due="2026-10-10"),
             t(4, due="2026-10-01"), t(5), t(6, due="2026-10-30"), t(7, scheduled="2026-10-25", due="2026-10-01"),
             t(8, done=True)]
    redo, undated = today_task.to_decide(every, {"lo-1"}, SAT)
    assert [x["id"] for x in redo] == ["lo-4", "lo-3"]  # 期限の古い順
    assert [x["id"] for x in undated] == ["lo-6", "lo-5"]
    assert [x["id"] for x in today_task.future_tasks(every, SAT)] == ["lo-7", "lo-2"]  # 同じ日なら期限の近い順


def test_all_list_five_per_section_numbers_continue_and_more(env):
    env.tasks = [t(1, scheduled="2026-10-17")] + [t(10 + i, due="2026-10-0" + str(i + 1)) for i in range(7)] \
        + [t(30 + i) for i in range(3)] + [t(50, scheduled="2026-11-01")]
    run(today_task.handle(env.message("今あるタスクは？")))
    text = env.sent[-1]
    assert "🔁 日を決め直すタスク：\n2. タスク10（期限 10/1(木)）" in text and "6. タスク14" in text
    assert "タスク15" not in text and "7. タスク30" in text and "タスク50" not in text
    assert "続きは『続き』" in text and "件" not in text
    assert [i["id"] for i in env.pending["today_list"]["payload"]["items"]][:2] == ["lo-1", "lo-10"]
    run(today_task.handle(env.message("続き")))
    assert "2. タスク15" in env.sent[-1] and "3. タスク16" in env.sent[-1] and "タスク30" not in env.sent[-1]
    assert "続き" not in env.sent[-1]
    run(today_task.handle(env.message("移す 2 月")))
    assert env.updates[-1] == ("lo-15", {"scheduled": "2026-10-19"})
    run(today_task.handle(env.message("続き")))  # 移したあとの一覧は今日の一覧なので、最初から出し直す
    assert "🔁 日を決め直すタスク：\n2. タスク10" in env.sent[-1]
    run(today_task.handle(env.message("続き")))
    run(today_task.handle(env.message("続き")))
    assert env.sent[-1] == "ここまでです。"


def test_future_list_only_on_request(env):
    env.tasks = [t(1, scheduled="2026-10-17"), t(2, scheduled="2026-10-20")]
    run(today_task.handle(env.message("今後の予定は？")))
    assert "📅 今後の予定：\n2. 10/20(火) タスク2" in env.sent[-1]


def test_sort_one_by_one(env):
    env.tasks = [t(1, due="2026-10-01"), t(2)]

    def upd(tid, **f):
        env.updates.append((tid, f))
        for x in env.tasks:
            if x["id"] == tid:
                x.update(f)
        return True

    env_monkey = pytest.MonkeyPatch()
    env_monkey.setattr(sheets, "update_task", upd)
    try:
        run(today_task.handle(env.message("整理")))
        assert "🔁 「タスク1（期限 10/1(木)）」はいつにしますか？" in env.sent[-1] and "Obsidian で消して" in env.sent[-1]
        run(today_task.handle(env.message("4")))
        assert env.updates == [("lo-1", {"scheduled": "2026-10-19"})]
        assert "🗂 「タスク2」" in env.sent[-1] and "Obsidian" not in env.sent[-1]
        run(today_task.handle(env.message("5")))  # このまま
        assert len(env.updates) == 1 and "ぜんぶ決まりました" in env.sent[-1]
        env.tasks.append(t(3))
        run(today_task.handle(env.message("整理")))
        run(today_task.handle(env.message("水")))
        assert env.updates[-1] == ("lo-2", {"scheduled": "2026-10-21"})  # 「このまま」にした分も、新しい整理ではまた出る
        run(today_task.handle(env.message("おわり")))
        assert "決めた分は" in env.sent[-1]
    finally:
        env_monkey.undo()


def test_sort_pending_does_not_eat_new_tasks(env, monkeypatch):
    env.tasks = [t(1)]
    added = []
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: added.append(content) or "lo-n")
    run(today_task.handle(env.message("整理")))
    run(today_task.handle(env.message("明日やる 牛乳を買う")))
    assert added == ["牛乳を買う"] and env.updates == []


def test_morning_sort_hint():
    assert today_task.SORT_HINT in today_task.compose_morning([], None, day=SAT, sort_hint=True)
    assert "整理" not in today_task.compose_morning([], None, day=SAT)

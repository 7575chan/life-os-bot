"""1日の区切り（朝5時。SPEC §7）: 完了日・やったこと・今日の一覧・日記を、同じ「活動日」で数える。"""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

import config
import done_list
import sheets
import state
import task_sync
import util
from handlers import looking_back, private, today_task


def at(d, h, m=0):
    return datetime(2026, 10, d, h, m, tzinfo=config.TZ)


@pytest.mark.parametrize("now, day", [
    (at(1, 23, 0), date(2026, 10, 1)),
    (at(2, 0, 30), date(2026, 10, 1)),
    (at(2, 4, 59), date(2026, 10, 1)),
    (at(2, 5, 0), date(2026, 10, 2)),
    (at(2, 9, 0), date(2026, 10, 2)),
])
def test_activity_day(now, day):
    assert util.activity_day(now) == day
    assert looking_back.journal_day(now) == day


def test_activity_day_uses_setting(monkeypatch):
    monkeypatch.setattr(config, "DAY_START_HOUR", 3)
    assert util.activity_day(at(2, 4, 0)) == date(2026, 10, 2)
    assert util.activity_day(at(2, 2, 59)) == date(2026, 10, 1)


@pytest.mark.parametrize("value, day", [
    ("2026-10-02 00:30", date(2026, 10, 1)),
    ("2026-10-02 04:59", date(2026, 10, 1)),
    ("2026-10-02 05:00", date(2026, 10, 2)),
    ("2026/10/02 23:10", date(2026, 10, 2)),
    ("2026-10-02", date(2026, 10, 2)),
    ("", None),
])
def test_activity_day_of_text(value, day):
    assert util.activity_day_of(value) == day


def test_selection_target_day_is_next_activity_day():
    assert looking_back.selection_target_day(at(1, 23)) == date(2026, 10, 2)
    assert looking_back.selection_target_day(at(2, 1)) == date(2026, 10, 2)  # 深夜は暦の上の「今日」
    assert looking_back.selection_target_day(at(2, 9)) == date(2026, 10, 3)


def test_complete_after_midnight_is_dated_the_previous_day(monkeypatch):
    rows = {}
    monkeypatch.setattr(util, "now", lambda: at(2, 0, 30))
    monkeypatch.setattr(sheets, "find_tasks", lambda ids: [{"id": "a", "row": 2, "content": "企画書", "done": False,
                                                             "deleted": False}])
    monkeypatch.setattr(sheets, "update_row", lambda name, row, data: rows.update(data))
    assert sheets.complete_tasks(["a"]) == ["企画書"]
    assert rows["完了日"] == "2026-10-01"


def test_sync_fills_done_date_with_the_activity_day(monkeypatch):
    seen = {}

    def plan(text, sheet_tasks, snapshot, today):
        seen["today"] = today
        return task_sync.SyncPlan(text=text)

    class Store:
        def modified(self, path):
            return 1

        def read(self, path):
            return ""

    monkeypatch.setattr(util, "now", lambda: at(2, 1, 0))
    monkeypatch.setattr(task_sync, "plan", plan)
    monkeypatch.setattr(sheets, "all_tasks", lambda: [])
    monkeypatch.setattr(state, "get_kv", lambda key, default=None: default)
    monkeypatch.setattr(state, "set_kv", lambda key, value: None)
    task_sync.run(Store())
    assert seen["today"] == date(2026, 10, 1)


def test_done_items_after_midnight_belong_to_the_previous_day():
    rows = [{"日時": "2026-10-01 22:00", "内容": "やったこと：洗濯"},
            {"日時": "2026-10-02 01:30", "内容": "やったこと：原稿"},
            {"日時": "2026-10-02 10:00", "内容": "やったこと：買い物"}]
    assert private.done_on(rows, date(2026, 10, 1)) == ["洗濯", "原稿"]
    assert private.done_on(rows, date(2026, 10, 2)) == ["買い物"]


def test_complete_reply_after_midnight_lists_the_previous_day(monkeypatch):
    asked = []
    monkeypatch.setattr(util, "now", lambda: at(2, 0, 30))
    monkeypatch.setattr(sheets, "complete_tasks", lambda ids, day=None: ["企画書"])
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(state, "latest_pending", lambda *a, **k: {"payload": {"items": [{"id": "a", "content": "企画書"}]}})
    monkeypatch.setattr(state, "put_pending", lambda *a, **k: None)

    def today_tasks(day):
        asked.append(("today", day))
        return []

    async def completed_on(day):
        asked.append(("done", day))
        return [{"id": "a", "content": "企画書"}]

    monkeypatch.setattr(sheets, "today_tasks", today_tasks)
    monkeypatch.setattr(done_list, "completed_on", completed_on)
    sent = []

    async def send(text, reference=None):
        sent.append(text)
        return NS(id=1)

    async def react(e):
        pass

    asyncio.run(today_task.handle(NS(content="完了 1", channel=NS(id=1, send=send), add_reaction=react)))
    assert ("done", date(2026, 10, 1)) in asked and ("today", date(2026, 10, 1)) in asked

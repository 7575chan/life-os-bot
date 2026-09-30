"""完了したタスクの一覧: ①完了の返信 ②朝の案内（昨夜〜今朝の分）③日記への書き足し（SPEC §5）。"""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import done_list
import notes
import scheduler
import sheets
import state
import task_sync
import util
import weather
from handlers import looking_back, today_task

DAY = date(2026, 9, 20)
_HEAD = {"ID": "", "登録日": "2026-09-18", "内容": "", "完了": "", "実行予定日": "", "期限": "", "優先度": "",
         "出典": "", "完了日": ""}


def run(coro):
    return asyncio.run(coro)


def row(r, tid, content, done="", done_date="", scheduled=""):
    return (r, {**_HEAD, "ID": tid, "内容": content, "完了": done, "完了日": done_date, "実行予定日": scheduled})


@pytest.fixture
def kv(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(state, "get_kv", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(state, "set_kv", lambda key, value: store.__setitem__(key, value))
    return store


@pytest.fixture
def sheet(monkeypatch):
    s = NS(tasks=[], private=[])
    monkeypatch.setattr(sheets, "records", lambda name: s.tasks if name == sheets.TASKS
                        else s.private if name == sheets.PRIVATE else [])
    monkeypatch.setattr(sheets, "update_row", lambda *a, **k: None)
    return s


# ---------------------------------------------------------------- 純粋


def test_completed_between_excludes_deleted_and_other_days(sheet):
    sheet.tasks = [row(2, "a", "A", "TRUE", "2026-09-19"), row(3, "b", "B", "TRUE", "2026-09-20"),
                   row(4, "c", "C", sheets.DELETED, "2026-09-20"), row(5, "d", "D"), row(6, "e", "E", "TRUE", "2026-09-21")]
    assert [t["id"] for t in sheets.completed_between(date(2026, 9, 19), date(2026, 9, 21))] == ["a", "b"]


def test_overnight_skips_what_the_evening_showed():
    tasks = [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}, {"id": "c", "content": "C"}]
    assert done_list.overnight(tasks, {"a"}) == ["B", "C"]
    assert done_list.overnight(tasks, set()) == ["A", "B", "C"]


def test_morning_puts_overnight_section_before_tasks():
    text = today_task.compose_morning([{"content": "企画書", "priority": "高", "due": "", "row": 2}], None, ["洗濯"])
    assert f"{done_list.OVERNIGHT_TITLE}\n・洗濯" in text
    assert text.index("昨夜から今朝まで") < text.index("本日のメインタスク")
    assert "昨夜から今朝まで" not in today_task.compose_morning([], None, [])


# ---------------------------------------------------------------- ② 夜に載せた分を覚え、朝は残りだけ


def test_evening_remembers_shown_and_morning_lists_the_rest(monkeypatch, kv, sheet):
    async def no_weather():
        return None

    sheet.tasks = [row(2, "a", "昼の仕事", "TRUE", "2026-09-20")]
    monkeypatch.setattr(weather, "today_weather", no_weather)
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 20, 23, 0, tzinfo=config.TZ))
    monkeypatch.setattr(util, "today", lambda: DAY)
    monkeypatch.setattr(state, "mark_done", lambda key: True)
    sent = []

    async def send(text):
        sent.append(text)

    async def not_posted(*a):
        return False

    monkeypatch.setattr(scheduler, "find_channel", lambda key: NS(send=send))
    monkeypatch.setattr(scheduler, "_posted_today", not_posted)
    assert run(scheduler.post_evening())
    assert "✅ 今日完了したタスク：\n・昼の仕事" in sent[0]
    assert done_list.evening_shown(DAY) == {"a"}

    # 23時のあと（同じ日の深夜と、日付が変わってから）に完了した分だけが、翌朝に出る
    sheet.tasks += [row(3, "b", "夜の洗濯", "TRUE", "2026-09-20"), row(4, "c", "深夜の片付け", "TRUE", "2026-09-21")]
    assert run(done_list.overnight_done(date(2026, 9, 21))) == ["夜の洗濯", "深夜の片付け"]


def test_morning_without_evening_record_lists_all_of_yesterday(kv, sheet):
    sheet.tasks = [row(2, "a", "昼の仕事", "TRUE", "2026-09-20")]
    assert run(done_list.overnight_done(date(2026, 9, 21))) == ["昼の仕事"]


def test_unreadable_sheet_gives_empty_lists(monkeypatch, kv):
    def broken(name):
        raise RuntimeError("quota")

    monkeypatch.setattr(sheets, "records", broken)
    assert run(done_list.overnight_done(DAY)) == []
    assert run(done_list.completed_on(DAY)) == []


# ---------------------------------------------------------------- ① 完了の返信に、今日完了したタスクを全部


def test_complete_reply_lists_everything_done_today(monkeypatch, sheet):
    sheet.tasks = [row(2, "a", "朝の散歩", "TRUE", "2026-09-20"), row(3, "b", "企画書", scheduled="2026-09-20")]
    monkeypatch.setattr(util, "today", lambda: DAY)
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 20, 23, 30, tzinfo=config.TZ))

    def complete(ids, day=None):
        for i, (r, rec) in enumerate(sheet.tasks):
            if rec["ID"] in ids:
                sheet.tasks[i] = row(r, rec["ID"], rec["内容"], "TRUE", "2026-09-20")
        return ["企画書"]

    monkeypatch.setattr(sheets, "complete_tasks", complete)
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(state, "latest_pending", lambda *a, **k: {"payload": {"items": [{"id": "b", "content": "企画書"}]}})
    monkeypatch.setattr(state, "put_pending", lambda *a, **k: None)
    sent = []

    async def send(text, reference=None):
        sent.append(text)
        return NS(id=1)

    async def react(e):
        pass

    run(today_task.handle(NS(content="完了 1", channel=NS(id=1, send=send), add_reaction=react)))
    assert sent[-1].startswith("✅ 完了にしました！\n・企画書")
    assert f"{done_list.SO_FAR_TITLE}\n・朝の散歩\n・企画書" in sent[-1]
    assert "件" not in sent[-1].split(done_list.SO_FAR_TITLE)[1].split("\n\n")[0]  # 件数は出さない


# ---------------------------------------------------------------- ③ 日記に書き足す（同じ日に2回書いても重ねない）


def test_journal_appends_lists_once_per_item(monkeypatch, tmp_path, kv, sheet):
    (tmp_path / "06-Life-OS").mkdir()
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    sheet.tasks = [row(2, "a", "企画書", "TRUE", "2026-09-20")]
    sheet.private = [(2, {"日時": "2026-09-20 09:00", "内容": "やったこと：洗濯", "タグ": "#やったこと"})]

    async def fake_ai(prompt, fallback, **kw):
        return {"mood": 4, "formatted": "記録", "wants_x": False, "wants_note": False, "tomorrow_tasks": []}

    async def no_weather():
        return None

    async def send(text, reference=None):
        return NS(id=2)

    async def react(emoji):
        pass

    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 20, 23, 30, tzinfo=config.TZ))
    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(weather, "today_weather", no_weather)
    monkeypatch.setattr(sheets, "append", lambda name, data: 2)
    monkeypatch.setattr(sheets, "backlog_tasks", lambda limit: [])
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(state, "put_pending", lambda *a, **k: None)
    msg = NS(content="4 いい日", channel=NS(id=20, send=send), add_reaction=react)
    run(looking_back._journal(msg, msg.content))
    diary = next((tmp_path / "06-Life-OS" / "03-looking-back").glob("*.md")).read_text(encoding="utf-8")
    assert "### ✅ 今日完了したタスク\n- 企画書" in diary and "### ✅ 今日やったこと\n- 洗濯" in diary

    sheet.tasks.append(row(3, "b", "電話", "TRUE", "2026-09-20"))
    run(looking_back._journal(msg, msg.content))
    diary = next((tmp_path / "06-Life-OS" / "03-looking-back").glob("*.md")).read_text(encoding="utf-8")
    assert diary.count("- 企画書") == 1 and diary.count("- 洗濯") == 1  # 2回目は新しい分だけ
    assert "### ✅ 今日完了したタスク\n- 電話" in diary

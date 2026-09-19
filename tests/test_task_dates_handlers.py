"""日付の言葉（今日やる / 9/25にする / 期限は〇日まで）が、3つの入口でタスクの実行日・期限に反映されること。"""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import settings
import sheets
import state
import task_dates
import task_sync
import util
import weather
from handlers import idea, looking_back, today_task

TODAY = date(2026, 9, 20)  # 日曜日


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 01-today-task


def test_plan_task_without_dates_is_todays_urgent_task():
    p = today_task.plan_task("牛乳を買う", TODAY)
    assert p == {"content": "牛乳を買う", "scheduled": "2026-09-20", "due": "", "priority": "高", "future": False,
                 "label": "牛乳を買う"}


def test_plan_task_today_and_due():
    p = today_task.plan_task("企画書 今日やる 期限は9/30", TODAY)
    assert (p["content"], p["scheduled"], p["due"], p["priority"]) == ("企画書", "2026-09-20", "2026-09-30", "高")
    assert p["label"] == "企画書（実行日 9/20(日)・期限 9/30(水)）"


def test_plan_task_due_only_still_goes_on_todays_list():
    p = today_task.plan_task("明日までに資料を送る", TODAY)
    assert (p["content"], p["scheduled"], p["due"], p["priority"], p["future"]) == ("資料を送る", "2026-09-20", "2026-09-21", "高", False)
    assert p["label"] == "資料を送る（期限 9/21(月)）"  # 書かれた日付だけを表示（実行日は書かれていない）


def test_plan_task_future_scheduled_is_not_urgent():
    p = today_task.plan_task("レポート 来週の月曜にやる", TODAY)
    assert (p["scheduled"], p["priority"], p["future"]) == ("2026-09-21", "", True)


@pytest.fixture
def today_env(monkeypatch):
    e = NS(added=[], sent=[], reactions=[])
    monkeypatch.setattr(util, "today", lambda: TODAY)
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: e.added.append((content, kw)) or "lo-x")
    monkeypatch.setattr(sheets, "today_tasks", lambda day: [])
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(state, "put_pending", lambda *a, **k: None)

    async def send(text, reference=None):
        e.sent.append(text)
        return NS(id=1)

    async def react(emoji):
        e.reactions.append(emoji)

    e.channel = NS(id=10, send=send)
    e.message = lambda text: NS(content=text, channel=e.channel, add_reaction=react)
    return e


def test_multiline_message_creates_one_task_per_line_with_dates(today_env):
    run(today_task.handle(today_env.message("牛乳を買う 今日やる\n企画書 9/30まで\nレポート 来週の月曜にやる")))
    assert today_env.added == [
        ("牛乳を買う", {"scheduled": "2026-09-20", "due": "", "priority": "高", "source": "01-today-task"}),
        ("企画書", {"scheduled": "2026-09-20", "due": "2026-09-30", "priority": "高", "source": "01-today-task"}),
        ("レポート", {"scheduled": "2026-09-21", "due": "", "priority": "", "source": "01-today-task"}),
    ]
    reply = today_env.sent[0]
    assert "・企画書（期限 9/30(水)）" in reply and "・レポート（実行日 9/21(月)）" in reply
    assert "その日の朝に案内します" in reply  # 実行日が未来のタスクがあるので説明が付く
    assert today_env.reactions == ["📝"]


def test_all_today_keeps_the_original_header(today_env):
    run(today_task.handle(today_env.message("牛乳を買う")))
    assert today_env.sent[0].startswith("今日のタスクに追加しました：\n・牛乳を買う")
    assert "その日の朝" not in today_env.sent[0]


# ---------------------------------------------------------------- 03-looking-back


def test_evening_tasks_get_dates_and_the_reply_lists_them(monkeypatch, tmp_path):
    (tmp_path / "06-Life-OS").mkdir()
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    added, sent = [], []
    now = datetime(2026, 9, 20, 22, 0, tzinfo=config.TZ)
    ai = {"mood": 4, "formatted": "今日の記録", "wants_x": False, "wants_note": False,
          "tomorrow_tasks": ["明日 第3章のプロットを書く", "資料を送る 9/30まで", "牛乳を買う"]}

    async def fake_ai(prompt, fallback, **kw):
        return ai

    async def no_weather():
        return None

    async def send(text, reference=None):
        sent.append(text)
        return NS(id=2)

    async def react(emoji):
        pass

    monkeypatch.setattr(util, "now", lambda: now)
    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(weather, "today_weather", no_weather)
    monkeypatch.setattr(sheets, "append", lambda *a, **k: 2)
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: added.append((content, kw)) or "lo-x")
    monkeypatch.setattr(sheets, "backlog_tasks", lambda limit: [])
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(state, "put_pending", lambda *a, **k: None)
    msg = NS(content="4 今日は原稿が進んだ。明日は第3章のプロットを書く。資料を9/30までに送る。牛乳を買う。",
             channel=NS(id=20, send=send), add_reaction=react)
    run(looking_back._journal(msg, msg.content))
    assert added == [
        ("第3章のプロットを書く", {"scheduled": "2026-09-21", "due": "", "source": "03-looking-back"}),
        ("資料を送る", {"scheduled": "", "due": "2026-09-30", "source": "03-looking-back"}),
        ("牛乳を買う", {"scheduled": "", "due": "", "source": "03-looking-back"}),  # 日付なし → バックログ（夜の番号選択の候補）
    ]
    text = "\n".join(sent)
    assert "📌 日付を設定しました：" in text
    assert "・第3章のプロットを書く（実行日 9/21(月)）" in text and "・資料を送る（期限 9/30(水)）" in text
    assert "牛乳を買う" not in text


def test_evening_prompt_tells_the_ai_to_keep_date_words():
    assert "日付の言葉" in looking_back._PROMPT and "投稿にあるとおりに" in looking_back._PROMPT


# ---------------------------------------------------------------- 09-idea


def test_idea_task_with_deadline_sets_due_and_stays_silent(monkeypatch, tmp_path):
    (tmp_path / "06-Life-OS" / "09-idea").mkdir(parents=True)
    (tmp_path / "06-Life-OS" / "09-idea" / "Ideas.md").write_text("", encoding="utf-8")
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    added, sent, reactions = [], [], []
    tomorrow = util.today() + timedelta(days=1)

    async def fake_ai(prompt, fallback, **kw):
        assert "日付の言葉" in prompt
        return {"tags": ["日常"], "task": "明日までに歯医者に電話する"}

    async def send(*a, **k):
        sent.append(a)

    async def react(emoji):
        reactions.append(emoji)

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(settings, "get", lambda key: ["小説", "日常"])
    monkeypatch.setattr(sheets, "append", lambda *a, **k: 2)
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: added.append((content, kw)) or "lo-x")
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(idea, "_write_lock", asyncio.Lock())
    msg = NS(content="明日までに歯医者に電話する", attachments=[], channel=NS(send=send), add_reaction=react)
    run(idea.handle(msg))
    assert added == [("歯医者に電話する", {"scheduled": "", "due": task_dates.iso(tomorrow), "source": "09-idea"})]
    assert reactions == ["📝", "📋"] and sent == []


def test_task_sync_writes_both_dates_to_obsidian_syntax():
    """シートの実行日・期限が、Obsidian Tasks プラグインの ⏳ と 📅 として書かれる。"""
    t = {"id": "lo-a1", "content": "企画書", "done": False, "deleted": False, "scheduled": "2026-09-20", "due": "2026-09-30",
         "priority": "高", "created": "2026-09-20", "done_date": "", "source": "", "row": 2}
    plan = task_sync.plan("", [t], {}, TODAY)
    assert "- [ ] 企画書 🆔 lo-a1 ⏫ ➕ 2026-09-20 ⏳ 2026-09-20 📅 2026-09-30" in plan.text
    # Obsidian 側で日付を書き換えると、シートに戻る
    edited = "- [ ] 企画書 🆔 lo-a1 ⏫ ➕ 2026-09-20 ⏳ 2026-09-22 📅 2026-10-05\n"
    back = task_sync.plan(edited, [t], {"lo-a1": task_sync.digest(t)}, TODAY)
    (tid, fields), = back.sheet_updates
    assert fields["scheduled"] == "2026-09-22" and fields["due"] == "2026-10-05"

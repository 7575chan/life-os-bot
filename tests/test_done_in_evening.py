"""05-private の「やったこと」を、23:00 の 03-looking-back の問いかけに「今日やったこと」として載せる（SPEC §5）。"""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import settings
import sheets
import state
import task_sync
import util
import weather
from handlers import looking_back, private

DAY = date(2026, 9, 20)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 「やったこと」の判定


@pytest.mark.parametrize("text, items", [
    ("やったこと：洗濯", ["洗濯"]),
    ("やったこと: 洗濯", ["洗濯"]),
    ("今日やったこと\n・洗濯\n- 買い物\n\n原稿 2000字", ["洗濯", "買い物", "原稿 2000字"]),
    ("やった事 部屋の掃除", ["部屋の掃除"]),
    ("できたこと：早起き", ["早起き"]),
    ("したこと\n散歩", ["散歩"]),
    ("やったこと", []),
    ("やったこと：写真\n[添付: a.png]", ["写真"]),  # シートの内容に付く添付の印は、やったことにしない
])
def test_done_items(text, items):
    assert private.done_items(text) == items


@pytest.mark.parametrize("text", ["洗濯やった", "駅前のカフェに行きたい", "したことない料理に挑戦したい", "やったことリストを作りたい",
                                  "昨日やったこと：洗濯"])
def test_not_done_posts(text):
    assert private.done_items(text) is None


def test_done_on_picks_only_that_day_in_order():
    rows = [
        {"日時": "2026-09-19 22:00", "内容": "やったこと：前日の分", "タグ": "#やったこと"},
        {"日時": "2026-09-20 09:00", "内容": "やったこと：洗濯", "タグ": "#やったこと"},
        {"日時": "2026-09-20 12:00", "内容": "駅前のカフェに行きたい", "タグ": "#行きたい"},
        {"日時": "2026-09-20 18:00", "内容": "今日やったこと\n・買い物\n・原稿", "タグ": "#やったこと"},
    ]
    assert private.done_on(rows, DAY) == ["洗濯", "買い物", "原稿"]


def test_today_done_survives_a_sheet_failure(monkeypatch):
    def boom(name):
        raise RuntimeError("quota")

    monkeypatch.setattr(sheets, "records", boom)
    assert run(private.today_done(DAY)) == []


# ---------------------------------------------------------------- 05-private での保存


@pytest.fixture
def private_env(tmp_path, monkeypatch):
    (tmp_path / "06-Life-OS" / "05-private").mkdir(parents=True)
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(rows=[], answered=[])

    async def fake_ai(prompt, fallback, **kw):
        return {"kind": "query", "tags": ["日常"], "search": None}  # AI が問い合わせと誤判定しても

    async def fake_complete(prompt, **kw):
        e.answered.append(prompt)
        return "答え"

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(claude_client, "complete", fake_complete)
    monkeypatch.setattr(sheets, "append", lambda name, data: e.rows.append((name, data)) or 2)
    monkeypatch.setattr(settings, "get", lambda key: ["行きたい", "日常"] if key == "private_tags" else None)
    monkeypatch.setattr(private, "_write_lock", asyncio.Lock())
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 20, 18, 0, tzinfo=config.TZ))
    return e


def test_done_post_is_saved_silently_with_tag_even_if_ai_says_query(private_env):
    reactions, sent = [], []

    async def react(emoji):
        reactions.append(emoji)

    async def send(*a, **k):
        sent.append(a)

    m = NS(content="やったこと：洗濯", attachments=[], channel=NS(send=send), add_reaction=react)
    run(private.handle(m))
    assert reactions == ["📝"] and sent == [] and private_env.answered == []
    name, row = private_env.rows[0]
    assert name == "05-private" and row["内容"] == "やったこと：洗濯" and row["タグ"].startswith("#やったこと")


# ---------------------------------------------------------------- 23:00 の問いかけ


def test_evening_default_time_is_23(monkeypatch):
    monkeypatch.delenv("EVENING_TIME", raising=False)
    assert config._parse_time(config.os.getenv("EVENING_TIME", "23:00")).hour == 23
    assert 'os.getenv("EVENING_TIME", "23:00")' in open(config.__file__, encoding="utf-8").read()
    assert "EVENING_TIME=23:00" in open(config.os.path.join(config.os.path.dirname(config.__file__), ".env.example"),
                                        encoding="utf-8").read()


def test_prompt_lists_todays_done_items(monkeypatch):
    async def fine():
        return "晴れ"

    rows = [(2, {"日時": "2026-09-20 09:00", "内容": "やったこと：洗濯\n・買い物", "タグ": "#やったこと"}),
            (3, {"日時": "2026-09-20 10:00", "内容": "ただのメモ", "タグ": "#日常"})]
    monkeypatch.setattr(weather, "today_weather", fine)
    monkeypatch.setattr(sheets, "records", lambda name: rows if name == sheets.PRIVATE else [])
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 20, 23, 0, tzinfo=config.TZ))
    text = run(looking_back.prompt_text())
    assert text.startswith(looking_back.EVENING_PREFIX)
    assert "✅ 今日やったこと（05-private より）：\n・洗濯\n・買い物" in text
    assert "ただのメモ" not in text
    assert text.index("今日の天気") < text.index("今日やったこと") < text.index("ご機嫌度")


def test_prompt_without_done_items_has_no_section(monkeypatch):
    async def none():
        return None

    monkeypatch.setattr(weather, "today_weather", none)
    monkeypatch.setattr(sheets, "records", lambda name: [])
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 20, 23, 0, tzinfo=config.TZ))
    assert "やったこと" not in run(looking_back.prompt_text())


# ---------------------------------------------------------------- 日付の区切り（05:00）


def test_journal_day_boundary():
    assert looking_back.journal_day(datetime(2026, 9, 20, 23, 30, tzinfo=config.TZ)) == date(2026, 9, 20)
    assert looking_back.journal_day(datetime(2026, 9, 21, 0, 30, tzinfo=config.TZ)) == date(2026, 9, 20)
    assert looking_back.journal_day(datetime(2026, 9, 21, 4, 59, tzinfo=config.TZ)) == date(2026, 9, 20)
    assert looking_back.journal_day(datetime(2026, 9, 21, 5, 0, tzinfo=config.TZ)) == date(2026, 9, 21)


def test_after_midnight_reply_goes_to_the_previous_days_diary(monkeypatch, tmp_path):
    (tmp_path / "06-Life-OS").mkdir()
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    rows, added = [], []

    async def fake_ai(prompt, fallback, **kw):
        return {"mood": 4, "formatted": "記録", "wants_x": False, "wants_note": False, "tomorrow_tasks": ["明日 電話する"]}

    async def no_weather():
        return None

    async def send(text, reference=None):
        return NS(id=2)

    async def react(emoji):
        pass

    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 21, 0, 30, tzinfo=config.TZ))
    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(weather, "today_weather", no_weather)
    monkeypatch.setattr(sheets, "append", lambda name, data: rows.append((name, data)) or 2)
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: added.append((content, kw)) or "lo-x")
    monkeypatch.setattr(sheets, "backlog_tasks", lambda limit: [])
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(state, "put_pending", lambda *a, **k: None)
    msg = NS(content="4 今日は原稿が進んだ", channel=NS(id=20, send=send), add_reaction=react)
    run(looking_back._journal(msg, msg.content))
    assert rows[0][1]["日付"] == "2026-09-20"
    assert (tmp_path / "06-Life-OS" / "03-looking-back").exists()
    assert added == [("電話する", {"scheduled": "2026-09-21", "due": "", "source": "03-looking-back"})]  # 日記の日の「明日」

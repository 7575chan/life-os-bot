"""プロジェクトボード（SPEC §5 03_CREATIVE「プロジェクトボード」）。"""
import asyncio
from datetime import date
from types import SimpleNamespace as NS

import pytest

import board
import notes
import sheets
import state
import stages
import util
import works
from handlers import week_plan as wp

DAY = date(2026, 10, 17)


def run(coro):
    return asyncio.run(coro)


def proj(no, title, kind="TRPG", now=True, stage="", next_="", deadline="", stop=""):
    return {"row": no + 1, "no": no, "title": title, "kind": kind, "now": now, "status": "製作中", "stage": stage,
            "next": next_, "deadline": deadline, "stop": stop, "chars": None, "target": None, "updated": ""}


TEMPLATES = stages.parse(stages.DEFAULT)


# ---------------------------------------------------------------- 純粋関数


@pytest.mark.parametrize("raw,expect", [
    ("2026/11/15", date(2026, 11, 15)), ("2026-11-15", date(2026, 11, 15)), ("11/15", date(2026, 11, 15)),
    ("11月15日", date(2026, 11, 15)), ("1/10", date(2027, 1, 10)), ("未定", None), ("", None),
])
def test_parse_deadline(raw, expect):
    assert board.parse_deadline(raw, DAY) == expect


def test_deadline_text_three_steps_and_no_judging_words():
    p = proj(4, "M.M.5s", deadline="2026/11/15")
    assert board.deadline_text(p, DAY) == "締切: 目標 11/1 ／ 予定 11/8 ／ 締切 11/15（締切まであと4週間）"
    assert board.deadline_text(proj(1, "a", deadline="2026/10/20"), DAY).endswith("（締切まであと3日）")
    past = board.deadline_text(proj(1, "a", deadline="2026/10/01"), DAY)
    assert past == "締切: 目標 9/17 ／ 予定 9/24 ／ 締切 10/1" and "過ぎ" not in past
    assert board.deadline_text(proj(1, "a"), DAY) == "締切: なし"
    assert board.deadline_text(proj(1, "a", deadline="年末くらい"), DAY) == "締切: 年末くらい"


def test_next_milestone():
    assert board.next_milestone(proj(4, "a", deadline="2026/11/15"), DAY) == "目標 11/1"
    assert board.next_milestone(proj(4, "a", deadline="2026/10/27"), DAY) == "予定 10/20"
    assert board.next_milestone(proj(4, "a", deadline="2026/10/01"), DAY) == ""


def test_render_only_now_projects():
    ps = [proj(30, "ひよの", kind="小説（長編）", stage="本文執筆"),
          proj(4, "M.M.5s", stage="校正01", next_="導入を推敲", deadline="2026/11/15", stop="戦闘が不安"),
          proj(13, "寝かせ中", now=False),
          proj(46, "運動", kind="私生活")]
    text = board.render(ps, [], TEMPLATES, DAY)
    assert text.index("M.M.5s") < text.index("ひよの")  # # の順
    assert "寝かせ中" not in text
    assert "工程: 校正01（全 " in text and "番目）" in text
    assert "次やること: 導入を推敲" in text and "次やること: まだ決まっていません" in text
    assert "止まっている理由: 戦闘が不安" in text
    assert "今週: 0回" in text
    assert "件" not in text and "%" not in text
    assert board.render([proj(1, "a", now=False)], [], TEMPLATES, DAY).startswith("今やるプロジェクトはありません")


def test_discord_text_is_cut_to_fit():
    text = board.discord_text("\n".join(["■ とても長い行" * 10] * 100), DAY)
    assert text.startswith("📂 プロジェクトボード（10/17）") and len(text) <= 2000 and "Obsidian" in text


# ---------------------------------------------------------------- 投稿と書き換え


@pytest.fixture
def env(monkeypatch, tmp_path):
    (tmp_path / "06-Life-OS" / "12-project-others").mkdir(parents=True)
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    monkeypatch.setattr(notes, "get_store", lambda: store)
    e = NS(projects=[proj(4, "M.M.5s", stage="校正01")], kv={}, done=set(), sent=[], edits=[], tmp=tmp_path)
    monkeypatch.setattr(works, "enabled", lambda: True)
    monkeypatch.setattr(works, "load", lambda refresh=False: [dict(p) for p in e.projects])
    monkeypatch.setattr(sheets, "all_tasks", lambda: [])
    monkeypatch.setattr(stages, "load", lambda: TEMPLATES)
    monkeypatch.setattr(util, "today", lambda: DAY)
    monkeypatch.setattr(util, "activity_day", lambda at=None: DAY)
    monkeypatch.setattr(state, "get_kv", lambda k, d=None: e.kv.get(k, d))
    monkeypatch.setattr(state, "set_kv", lambda k, v: e.kv.__setitem__(k, v))
    monkeypatch.setattr(state, "mark_done", lambda k: not (k in e.done or e.done.add(k)))

    class Msg:
        def __init__(self, mid, content):
            self.id, self.content, self.jump_url = mid, content, f"https://discord/{mid}"

        async def edit(self, content):
            self.content = content
            e.edits.append(content)

    e.msgs = {}

    async def send(text):
        m = Msg(500 + len(e.sent), text)
        e.sent.append(text)
        e.msgs[m.id] = m
        return m

    async def fetch(mid):
        if mid not in e.msgs:
            raise type("NotFound", (Exception,), {})()
        return e.msgs[mid]

    e.ch = NS(id=9, send=send, fetch_message=fetch)
    board.set_channel_finder(lambda key: e.ch)
    yield e
    board.set_channel_finder(None)


def note(e):
    return (e.tmp / "06-Life-OS" / "12-project-others" / "プロジェクトボード.md").read_text(encoding="utf-8")


def test_daily_post_once_then_edit_on_change(env):
    assert run(board.post_daily(env.ch)) is True
    assert run(board.post_daily(env.ch)) is False  # 同じ日に2回投稿しない
    assert len(env.sent) == 1 and env.sent[0].startswith("📂 プロジェクトボード（10/17）")
    assert "工程: 校正01" in note(env) and "手で書き足した内容は残りません" in note(env)
    run(board.refresh())
    assert env.edits == []  # 変わっていなければ書き換えない
    env.projects[0]["next"] = "導入を推敲"
    url = run(board.refresh())
    assert len(env.sent) == 1 and "次やること: 導入を推敲" in env.edits[-1] and url == "https://discord/500"
    assert "導入を推敲" in note(env)


def test_refresh_before_daily_post_updates_only_obsidian(env):
    env.projects[0]["next"] = "x"
    assert run(board.refresh()) is None
    assert env.sent == [] and "次やること: x" in note(env)


def test_deleted_post_is_not_recreated(env):
    run(board.post_daily(env.ch))
    env.msgs.clear()
    env.projects[0]["stage"] = "校正02"
    run(board.refresh())
    assert env.kv["board_msg:2026-10-17"] == "gone" and len(env.sent) == 1
    run(board.refresh(force=True))
    assert len(env.sent) == 1


# ---------------------------------------------------------------- 週の計画とのつながり


def test_week_plan_gets_next_steps_and_goal_dates():
    ps = [proj(4, "M.M.5s", next_="導入を推敲", deadline="2026/11/04"), proj(30, "ひよの", next_="2章"),
          proj(13, "寝かせ中", now=False, next_="x")]
    tasks = [{"id": "lo-1", "content": "ひよの｜2章", "scheduled": "", "due": "", "priority": "", "source": "作品#30",
              "done": False, "deleted": False, "row": 2}]
    out = wp.with_projects(tasks, ps, DAY)
    new = [t for t in out if t["id"].startswith(wp.NEW_PREFIX)]
    assert [t["content"] for t in new] == ["M.M.5s｜導入を推敲"]  # 既にタスクのものと、今やるでないものは足さない
    assert new[0]["due"] == "2026-10-21"  # 目標日（締切の2週間前）
    plan = wp.build(out, wp.plan_days(DAY, this_week=False), DAY)
    placed = {plan["items"][i]["id"]: iso for iso, v in plan["grid"].items() for i in v}
    assert placed["new:4"] <= "2026-10-21"

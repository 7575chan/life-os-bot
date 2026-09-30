"""完了したタスクの一覧（SPEC §5）。完了の返信（01-today-task）・夜の問いかけと日記（03-looking-back）・朝の案内で共通に使う。

件数や達成率は出さず、一覧だけにする（全肯定・減点しない方針）。シートを読めなければ空（投稿や返信は止めない）。
"""
import asyncio
import json
import logging
from datetime import date, timedelta

import sheets
import state

log = logging.getLogger(__name__)

TODAY_TITLE = "✅ 今日完了したタスク："
SO_FAR_TITLE = "🌟 今日完了したタスク："
OVERNIGHT_TITLE = "🌙 昨夜から今朝までに完了したタスク："


def format_items(title: str, items: list[str]) -> str | None:
    return (title + "\n" + "\n".join(f"・{t}" for t in items)) if items else None


async def completed_between(start: date, end: date) -> list[dict]:
    try:
        return await asyncio.to_thread(sheets.completed_between, start, end)
    except Exception:  # noqa: BLE001
        log.warning("完了したタスクを読めませんでした", exc_info=True)
        return []


async def completed_on(day: date) -> list[dict]:
    return await completed_between(day, day + timedelta(days=1))


# ---------------------------------------------------------------- 夜の問いかけに載せたもの（朝の案内で重ねて出さない）


def _evening_key(day: date) -> str:
    return f"evening_shown:{day.isoformat()}"


def remember_evening(day: date, ids: list[str]) -> None:
    """day の夜の問いかけに載せたタスクの ID を覚える（問いかけを投稿したあとに呼ぶ）。"""
    state.set_kv(_evening_key(day), json.dumps(sorted(set(ids))))


def evening_shown(day: date) -> set[str]:
    try:
        return set(json.loads(state.get_kv(_evening_key(day)) or "[]"))
    except (TypeError, ValueError):
        return set()


def overnight(tasks: list[dict], shown: set[str]) -> list[str]:
    """前日と当日に完了したタスクのうち、前日の夜の問いかけに載っていないもの。"""
    return [t["content"] for t in tasks if t["id"] not in shown]


async def overnight_done(day: date) -> list[str]:
    """朝の案内用: 前日の夜の問いかけのあと（〜今朝）に完了したタスク。問いかけが出ていなければ、前日の分も全部。"""
    y = day - timedelta(days=1)
    return overnight(await completed_between(y, day + timedelta(days=1)), evening_shown(y))


# ---------------------------------------------------------------- 日記に書いたもの（同じ日に2回書いても重ねない）


def _diary_key(day: date) -> str:
    return f"diary_listed:{day.isoformat()}"


def diary_new(day: date, kind: str, items: list[str]) -> list[str]:
    """その日の日記にまだ書いていない項目（kind ごとに覚える）。"""
    try:
        written = json.loads(state.get_kv(_diary_key(day)) or "{}").get(kind, [])
    except (TypeError, ValueError, AttributeError):
        written = []
    return [i for i in items if i not in written]


def remember_diary(day: date, kind: str, items: list[str]) -> None:
    try:
        data = json.loads(state.get_kv(_diary_key(day)) or "{}")
        if not isinstance(data, dict):
            data = {}
    except (TypeError, ValueError):
        data = {}
    data[kind] = list(dict.fromkeys(data.get(kind, []) + items))
    state.set_kv(_diary_key(day), json.dumps(data, ensure_ascii=False))

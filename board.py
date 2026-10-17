"""プロジェクトボード（SPEC §5 03_CREATIVE「プロジェクトボード」）。

今やるプロジェクトごとに「工程・次やること・締切の3段（目標 = 2週間前・予定 = 1週間前・締切）」を出す。
- Discord: `04-report` に毎日 BOARD_TIME（既定 09:05）に1回投稿し、その日のうちに変わったらその投稿を書き換える
- Obsidian: `06-Life-OS/12-project-others/プロジェクトボード.md` を毎回まるごと書き直す
件数・達成率・評価の言葉は書かない。締切が過ぎていても日付を並べるだけ。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from datetime import date, timedelta

import notes
import sheets
import stages
import state
import task_dates
import util
import vault_paths
import works

log = logging.getLogger("life-os.board")

PREFIX = "📂 プロジェクトボード"
MSG_KEY = "board_msg:{}"  # その日の投稿の (チャンネル ID, メッセージ ID)。"gone" なら書き換えない
HASH_KEY = "board_hash"
DISCORD_MAX = 1900
NOTE_HEAD = "> Bot が毎回まるごと書き直すノートです（手で書き足した内容は残りません）。正本は作品管理シートです。\n"
NEXT_HINT = "まだ決まっていません（『次やること 番号 内容』か『タスク案 番号』で決められます）"

_finder = None  # チャンネルを探す関数（scheduler.start が渡す）
_lock = asyncio.Lock()


def set_channel_finder(fn) -> None:
    global _finder
    _finder = fn


# ---------------------------------------------------------------- 純粋関数

_MD = re.compile(r"(\d{1,2})\s*[/月]\s*(\d{1,2})")


def parse_deadline(raw: str, today: date) -> date | None:
    """「2026/11/15」「2026-11-15」「11/15」「11月15日」。年が無ければ今年（半年以上前なら来年）。"""
    d = util.parse_date_any(raw)
    if d:
        return d
    m = _MD.search(unicodedata.normalize("NFKC", raw or ""))
    if not m:
        return None
    try:
        d = date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return d.replace(year=today.year + 1) if (today - d).days > 180 else d


def milestones(deadline: date) -> list[tuple[str, date]]:
    return [("目標", deadline - timedelta(days=14)), ("予定", deadline - timedelta(days=7)), ("締切", deadline)]


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def deadline_text(p: dict, today: date) -> str:
    raw = (p.get("deadline") or "").strip()
    if not raw:
        return "締切: なし"
    d = parse_deadline(raw, today)
    if d is None:
        return f"締切: {raw}"
    text = "締切: " + " ／ ".join(f"{name} {_md(x)}" for name, x in milestones(d))
    left = (d - today).days
    if left >= 7:
        text += f"（締切まであと{left // 7}週間）"
    elif left > 0:
        text += f"（締切まであと{left}日）"
    return text


def next_milestone(p: dict, today: date) -> str:
    """朝の案内用: 3段のうち、まだ来ていない最初の1つ（無ければ空）。"""
    d = parse_deadline(p.get("deadline") or "", today)
    if d is None:
        return ""
    for name, x in milestones(d):
        if x >= today:
            return f"{name} {_md(x)}"
    return ""


def stage_text(p: dict, steps: list[str] | None) -> str:
    if not p.get("stage"):
        return "工程: まだ決まっていません"
    if steps and p["stage"] in steps:
        return f"工程: {p['stage']}（全 {len(steps)} 工程の {steps.index(p['stage']) + 1} 番目）"
    return f"工程: {p['stage']}"


def render(projects: list[dict], tasks: list[dict], templates: dict, day: date) -> str:
    """ボードの本文（見出しなし）。"""
    now = sorted(works.now_projects(projects), key=lambda p: (p.get("no") is None, p.get("no") or 0))
    if not now:
        return "今やるプロジェクトはありません（12 などの部屋で『今やる 番号』）。"
    blocks = []
    for p in now:
        kind = f"（{p['kind']}）" if p.get("kind") else ""
        lines = [f"■ {works.label(p)}{kind}"]
        if works.is_continuing(p):
            lines.append(f"今週: {works.week_count(p, tasks, day)}回")
        else:
            lines.append(stage_text(p, stages.steps_for(templates, p.get("kind", ""), p["title"])))
        lines.append(f"次やること: {p.get('next') or NEXT_HINT}")
        if not works.is_continuing(p) or p.get("deadline"):
            lines.append(deadline_text(p, day))
        if p.get("stop"):
            lines.append(f"止まっている理由: {p['stop']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def discord_text(body: str, day: date) -> str:
    text = f"{PREFIX}（{day.month}/{day.day}）\n\n{body}"
    if len(text) > DISCORD_MAX:
        text = text[:DISCORD_MAX].rsplit("\n", 1)[0] + "\n…（続きは Obsidian の『プロジェクトボード』）"
    return text


def note_text(body: str, day: date) -> str:
    return f"# プロジェクトボード\n\n{NOTE_HEAD}\n更新: {util.fmt_date(day)}\n\n{body}\n"


# ---------------------------------------------------------------- 読み書き


def build(day: date | None = None) -> str:
    """今の内容の本文（シートを読む。読めなければ例外）。"""
    day = day or util.activity_day()
    projects = works.load()
    tasks = sheets.all_tasks() if any(works.is_continuing(p) for p in works.now_projects(projects)) else []
    return render(projects, tasks, stages.load(), day)


def _write_note(body: str, day: date) -> None:
    try:
        notes.get_store().write(vault_paths.project_board(), note_text(body, day))
    except Exception:  # noqa: BLE001  Obsidian に書けなくても、Discord は止めない
        log.warning("プロジェクトボードのノートを書けませんでした", exc_info=True)


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def post_daily(ch, force: bool = False) -> bool:
    """毎日 BOARD_TIME: その日のボードを1回投稿する（同じ日に2回投稿しない）。"""
    if not works.enabled():
        return False
    day = util.today()
    if not force and not state.mark_done(f"board:{day}"):
        return False
    async with _lock:
        body = await asyncio.to_thread(build, util.activity_day())
        msg = await ch.send(discord_text(body, day))
        state.set_kv(MSG_KEY.format(day.isoformat()), f"{ch.id}:{msg.id}")
        await asyncio.to_thread(_write_note, body, day)
        state.set_kv(HASH_KEY, _digest(body))
    return True


async def refresh(force: bool = False) -> str | None:
    """今の内容でその日の投稿と Obsidian を書き直す（変わっていなければ何もしない）。その日の投稿の URL を返す。"""
    if not works.enabled():
        return None
    async with _lock:
        try:
            body = await asyncio.to_thread(build, util.activity_day())
        except Exception:  # noqa: BLE001
            log.warning("プロジェクトボードを作れませんでした（次の機会にやり直します）", exc_info=True)
            return None
        day = util.today()
        changed = state.get_kv(HASH_KEY) != _digest(body)
        if changed or force:
            await asyncio.to_thread(_write_note, body, day)
        ref = state.get_kv(MSG_KEY.format(day.isoformat()))
        url = None
        if ref and ref != "gone":
            msg_id = int(ref.split(":")[1])
            try:
                ch = _finder("report") if _finder else None
                if ch is not None:
                    msg = await ch.fetch_message(msg_id)
                    if changed or force:
                        await msg.edit(content=discord_text(body, day))
                    url = msg.jump_url
            except Exception as e:  # noqa: BLE001  消された投稿は作り直さない
                if type(e).__name__ == "NotFound":
                    state.set_kv(MSG_KEY.format(day.isoformat()), "gone")
                    log.info("今日のプロジェクトボードの投稿が見つからないので、今日は書き換えません")
                else:
                    log.warning("プロジェクトボードの投稿を書き換えられませんでした", exc_info=True)
        state.set_kv(HASH_KEY, _digest(body))
        return url


"""プロジェクトの部屋（10〜12）で使う、作品管理シートのコマンド（SPEC §5 03_CREATIVE「作品管理シート」）。

「プロジェクト」「今やる」「今やる 番号」「今やる やめる 番号」「工程 番号 [段階]」「次の工程 番号」
「タスク案 番号」「止まった 番号 理由」「再開 番号」「やった 番号」
「次やること 番号 [内容|なし]」「今日やる 番号」「やる日 番号 日付」「ボード」（プロジェクトボード）。コマンドの投稿は作品ノートに追記しない。
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date

import board
import claude_client
import sheets
import stages
import state
import task_dates
import task_sync
import util
import works

log = logging.getLogger("life-os.works_cmd")

KIND = "works_tasks"
NOT_FOUND = "そのプロジェクトは見当たりませんでした。『#』の番号（例: 4番）か、タイトルの一部で送ってください。"
_LIST = re.compile(r"^(?:プロジェクト|プロジェクト一覧|今やる|今やるプロジェクト)$")
_BOARD = re.compile(r"^(?:ボード|プロジェクトボード)$")
_CMD = re.compile(r"^(今やる|次の工程|工程|タスク案|止まった|再開|やった|次やること|今日やる|やる日)\s+(.+)$", re.S)
_NONE = re.compile(r"^(?:なし|無し|空|消す|クリア)$")
_STOP_WORDS = re.compile(r"^(?:やめる|外す|はずす|解除)\s+(.+)$")

_PROMPT = """次のプロジェクトについて、今の工程を進めるための「15〜30分で終わる、具体的な作業」を3つ考えてください。
- 最初の1つは、気が重くても始められる、いちばん小さな一歩にする
- 作業の内容だけを書く（評価・助言・励ましは書かない）。1つ40文字以内
- 工程が空なら、そのプロジェクトのゴールに近づく最初の一歩にする
JSON だけを返す: {{"tasks": ["...", "...", "..."]}}

プロジェクト: {title}
種類: {kind}
状況: {status}
今の工程: {stage}
工程の流れ: {steps}
文字数: {chars}
止まっている理由: {stop}
まだ終わっていない場面・章（作品ごとのタブより）: {outline}"""


def parse(text: str):
    """(コマンド, 残り) か None。"""
    t = unicodedata.normalize("NFKC", text).strip()
    if _LIST.match(t):
        return "list", ""
    if _BOARD.match(t):
        return "board", ""
    m = _CMD.match(t)
    return (m.group(1), m.group(2).strip()) if m else None


def split_target(rest: str, projects: list[dict]) -> tuple[list[dict], str]:
    """「4番 校正01」「M.M.5s 校正01」→ (候補, 残りの文)。全体で見つかればそれを優先する。"""
    whole = works.find(rest, projects)
    if whole:
        return whole, ""
    tokens = rest.split()
    for k in range(len(tokens) - 1, 0, -1):
        hit = works.find(" ".join(tokens[:k]), projects)
        if hit:
            return hit, " ".join(tokens[k:])
    return [], rest


def _ambiguous(cands: list[dict]) -> str:
    return "どれのことか決められませんでした。番号で送ってください：\n" + "\n".join(f"・{works.label(p)}" for p in cands[:10])


async def handle(message, text: str) -> bool:
    """コマンドなら処理して True。作品管理シートが未設定なら何もしない（False）。"""
    if not works.enabled():
        return False
    ch = message.channel
    if await _maybe_select(message, text):
        return True
    cmd = parse(text)
    if not cmd:
        return False
    name, rest = cmd
    try:
        projects = await asyncio.to_thread(works.load)
    except Exception:  # noqa: BLE001
        log.warning("作品管理シートを読めませんでした", exc_info=True)
        await ch.send("作品管理シートを読めませんでした。少しあとでもう一度どうぞ。")
        return True
    try:
        if name == "list":
            await _list(ch, projects)
        elif name == "board":
            await _board(ch)
        elif name == "今やる":
            await _now(message, rest, projects)
        else:
            cands, extra = split_target(rest, projects)
            if not cands:
                await ch.send(NOT_FOUND)
            elif len(cands) > 1:
                await ch.send(_ambiguous(cands))
            else:
                await {"工程": _stage, "次の工程": _next, "タスク案": _ideas, "止まった": _stop,
                       "再開": _resume, "やった": _did, "次やること": _set_next, "今日やる": _do_today,
                       "やる日": _do_on}[name](message, cands[0], extra)
                if name not in ("タスク案",):
                    await board.refresh()  # 変わっていなければ何もしない
    except works.WorksError as e:
        await ch.send(str(e))
    return True


async def _board(ch) -> None:
    url = await board.refresh(force=True)
    if url:
        await ch.send(f"📂 プロジェクトボードを今の内容にしました：{url}")
        return
    body = await asyncio.to_thread(board.build)
    await util.send_long(ch, board.discord_text(body, util.today()))


async def _set_next(message, p: dict, extra: str) -> None:
    ch = message.channel
    if not extra:
        await ch.send(f"『{p['title']}』の次やること: {p.get('next') or board.NEXT_HINT}")
        return
    value = "" if _NONE.match(extra) else extra
    await asyncio.to_thread(works.write, p, "次やることを書く（作品管理シート）",
                            **{works.COL_NEXT: value, works.COL_UPDATED: works.today_text()})
    await util.ack(message, "📂")
    await ch.send(f"『{p['title']}』の次やることを空にしました。" if not value
                  else f"『{p['title']}』の次やること: {value}\n（『今日やる {p['no']}』で今日のタスクにできます）")


async def _make_task(message, p: dict, day: date) -> None:
    ch = message.channel
    if not p.get("next"):
        await ch.send(f"先に次やることを決めましょう（『次やること {p['no']} 内容』か『タスク案 {p['no']}』）。")
        return
    content = works.task_content(p, p["next"])
    existing = [t for t in await asyncio.to_thread(sheets.all_tasks)
                if t["content"] == content and not t["done"] and not t["deleted"]]
    if existing:
        await asyncio.to_thread(sheets.update_task, existing[0]["id"], scheduled=day.isoformat())
    else:
        await asyncio.to_thread(sheets.add_task, content, scheduled=day.isoformat(), source=works.source_of(p))
    await asyncio.to_thread(task_sync.run_safely)
    await util.ack(message, "📆")
    when = "今日" if day == util.activity_day() else task_dates.fmt_md(day)
    await ch.send(f"📆 {when}のタスクにしました：{content}")


async def _do_today(message, p: dict, extra: str) -> None:
    await _make_task(message, p, util.activity_day())


async def _do_on(message, p: dict, extra: str) -> None:
    from handlers import week_plan  # 「移す」と同じ日付の言葉
    target = week_plan.parse_target(extra, util.activity_day()) if extra else None
    if not isinstance(target, date):
        await message.channel.send(f"やる日が読めませんでした（例: やる日 {p['no']} 明日 / 金 / 10/25）。")
        return
    await _make_task(message, p, target)


async def _list(ch, projects: list[dict]) -> None:
    now = works.now_projects(projects)
    if not now:
        await ch.send("今やるプロジェクトは、まだありません。『今やる 番号』で選べます（3本まで）。")
        return
    tasks = await asyncio.to_thread(sheets.all_tasks)
    day = util.activity_day()
    lines = ["📂 今やるプロジェクト："] + [works.line(p, tasks, day) for p in now]
    if len(now) > works.NOW_MAX:
        lines.append(f"（今やるは{works.NOW_MAX}本までと決めていました。『今やる やめる 番号』で外せます）")
    await util.send_long(ch, "\n".join(lines))


async def _now(message, rest: str, projects: list[dict]) -> None:
    ch = message.channel
    m = _STOP_WORDS.match(rest)
    target = m.group(1) if m else rest
    cands = works.find(target, projects)
    if not cands:
        await ch.send(NOT_FOUND)
        return
    if len(cands) > 1:
        await ch.send(_ambiguous(cands))
        return
    p = cands[0]
    if m:
        if not p["now"]:
            await ch.send(f"『{p['title']}』は、今やるに入っていません。")
            return
        await asyncio.to_thread(works.write, p, "今やるから外す（作品管理シート）",
                                **{works.COL_NOW: works.NO, works.COL_UPDATED: works.today_text()})
        await util.ack(message, "📂")
        await ch.send(f"『{p['title']}』を今やるから外しました。")
        return
    if p["now"]:
        await ch.send(f"『{p['title']}』は、もう今やるに入っています。")
        return
    current = works.now_projects(projects)
    if len(current) >= works.NOW_MAX:
        await ch.send(f"今やるは{works.NOW_MAX}本そろっています。先に『今やる やめる 番号』で1本外してください：\n"
                      + "\n".join(f"・{works.label(x)}" for x in current))
        return
    await asyncio.to_thread(works.write, p, "今やるにする（作品管理シート）",
                            **{works.COL_NOW: works.YES, works.COL_UPDATED: works.today_text()})
    await util.ack(message, "📂")
    await ch.send(f"『{p['title']}』を今やるにしました。" + (f"今の工程: {p['stage']}" if p.get("stage") else "『次の工程 番号』で工程を進められます。"))


async def _steps(p: dict) -> list[str] | None:
    return stages.steps_for(await asyncio.to_thread(stages.load), p["kind"], p["title"])


async def _stage(message, p: dict, extra: str) -> None:
    ch = message.channel
    if not extra:
        steps = await _steps(p)
        if not steps or stages.is_continuing(steps):
            await ch.send(f"『{p['title']}』は工程を持たないプロジェクトです。" if steps else
                          f"『{p['title']}』の種類（{p['kind'] or '空'}）の工程テンプレートがありません。『工程 番号 段階』で好きな段階を書けます。")
            return
        await ch.send(f"『{p['title']}』の工程：\n{stages.show(steps, p['stage'])}"
                      + ("" if p["stage"] in steps or not p["stage"] else f"\n（今の工程: {p['stage']}）"))
        return
    await asyncio.to_thread(works.write, p, "工程を書く（作品管理シート）",
                            **{works.COL_STAGE: extra, works.COL_UPDATED: works.today_text()})
    await util.ack(message, "📂")
    await ch.send(f"『{p['title']}』の工程を「{extra}」にしました。")


async def _next(message, p: dict, extra: str) -> None:
    ch = message.channel
    steps = await _steps(p)
    if not steps or stages.is_continuing(steps):
        await ch.send(f"『{p['title']}』には工程テンプレートがありません。『工程 番号 段階』で好きな段階を書けます。")
        return
    nxt = stages.next_step(steps, p["stage"])
    if nxt is None:
        if p["stage"] == steps[-1]:
            await ch.send(f"『{p['title']}』は、最後の工程（{p['stage']}）まで来ています。")
        else:
            await ch.send(f"今の工程「{p['stage']}」がテンプレートにありません。『工程 番号 段階』で書いてください：\n{stages.show(steps, '')}")
        return
    await asyncio.to_thread(works.write, p, "工程を進める（作品管理シート）",
                            **{works.COL_STAGE: nxt, works.COL_UPDATED: works.today_text()})
    await util.ack(message, "📂")
    await ch.send(f"『{p['title']}』の工程を「{nxt}」に進めました。『タスク案 {p['no']}』か『次やること {p['no']} 内容』で、次の小さな作業を決められます。")


async def _ideas(message, p: dict, extra: str) -> None:
    ch = message.channel
    steps = await _steps(p)
    outline = await asyncio.to_thread(works.outline, p)
    prompt = _PROMPT.format(title=p["title"], kind=p["kind"] or "不明", status=p["status"] or "不明",
                            stage=p["stage"] or "（空）", steps=" → ".join(steps or []) or "（テンプレートなし）",
                            chars=works.chars_text(p) or "不明", stop=p["stop"] or "なし",
                            outline="、".join(outline) or "なし")
    data = await claude_client.complete_json(prompt, {"tasks": []})
    items = [str(t).strip()[:60] for t in (data.get("tasks") if isinstance(data, dict) else []) or [] if str(t).strip()][:3]
    if not items:
        await ch.send("タスクの案を作れませんでした。少しあとでもう一度どうぞ。")
        return
    body = (f"🧩 『{p['title']}』のタスク案（{p['stage'] or '工程なし'}）：\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))
            + "\n次やることにするものを番号で返信してください（例: 1）。いらなければ「なし」で大丈夫です。")
    msg = await ch.send(body)
    state.put_pending(msg.id, ch.id, KIND, {"items": items, "no": p["no"], "title": p["title"]})


async def _maybe_select(message, text: str) -> bool:
    """タスク案への「1, 3」「なし」の返信。"""
    ref = getattr(message, "reference", None)
    pending = None
    if ref and getattr(ref, "message_id", None):
        p = state.get_pending(ref.message_id)
        if p and p["kind"] == KIND:
            pending = p
    nums = util.parse_numbers(text)
    if pending is None and (nums is not None or util.is_none_choice(text)):
        pending = state.latest_pending(message.channel.id, KIND, max_age_hours=12)
    if pending is None or not (nums is not None or util.is_none_choice(text)):
        return False
    ch = message.channel
    payload = pending["payload"]
    state.delete_pending(pending["message_id"])
    if util.is_none_choice(text):
        await ch.send("了解です。")
        return True
    chosen = [payload["items"][n - 1] for n in dict.fromkeys(nums or []) if 1 <= n <= len(payload["items"])]
    if not chosen:
        state.put_pending(pending["message_id"], ch.id, KIND, payload)
        await ch.send("その番号は見当たりませんでした。もう一度どうぞ。")
        return True
    try:
        projects = await asyncio.to_thread(works.load)
        p = next((x for x in projects if x["no"] == payload["no"] and x["title"] == payload["title"]), None)
        if p is None:
            await ch.send("表の並びが変わっていたので、次やることは書きませんでした。もう一度『タスク案 番号』からどうぞ。")
            return True
        await asyncio.to_thread(works.write, p, "次やることを書く（作品管理シート）",
                                **{works.COL_NEXT: chosen[0], works.COL_UPDATED: works.today_text()})
    except works.WorksError as e:
        await ch.send(str(e))
        return True
    await util.ack(message, "📂")
    await ch.send(f"📂 『{p['title']}』の次やること: {chosen[0]}\n（『今日やる {p['no']}』で今日のタスクにできます）"
                  + ("\n（次やることは1つだけなので、最初の番号にしました）" if len(chosen) > 1 else ""))
    await board.refresh()
    return True


async def _stop(message, p: dict, extra: str) -> None:
    ch = message.channel
    if not extra:
        await ch.send(f"止まった理由を一言だけ添えてください（例: 止まった {p['no']} 戦闘バランスが不安）。")
        return
    await asyncio.to_thread(works.write, p, "ストップ理由を書く（作品管理シート）",
                            **{works.COL_STOP: extra, works.COL_UPDATED: works.today_text()})
    await util.ack(message, "📂")
    await ch.send(f"『{p['title']}』の止まっている理由を書いておきました。戻ってきたら『再開 {p['no']}』で消せます。")


async def _resume(message, p: dict, extra: str) -> None:
    await asyncio.to_thread(works.write, p, "ストップ理由を消す（作品管理シート）",
                            **{works.COL_STOP: "", works.COL_UPDATED: works.today_text()})
    await util.ack(message, "📂")
    await message.channel.send(f"『{p['title']}』を再開にしました。おかえりなさい。")


async def _did(message, p: dict, extra: str) -> None:
    day = util.fmt_date(util.activity_day())
    await asyncio.to_thread(sheets.add_task, works.task_content(p, extra or "やった"), scheduled=day,
                            source=works.source_of(p), done=True, done_date=day)
    await asyncio.to_thread(task_sync.run_safely)
    await asyncio.to_thread(works.touch, p)
    tasks = await asyncio.to_thread(sheets.all_tasks)
    await util.ack(message, "✅")
    if works.is_continuing(p):
        await message.channel.send(f"✅ 『{p['title']}』、記録しました。今週 {works.week_count(p, tasks, util.activity_day())}回目です。")
    else:
        await message.channel.send(f"✅ 『{p['title']}』、記録しました。")

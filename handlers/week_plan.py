"""`01-today-task`: 週の計画・持ち越しの「移す」・今日は休む・着手・最初の5分（SPEC §5「週の計画」）。

- 「来週の計画」「今週の計画」: 月〜金に1日3つまでの割り振り案（土日はバッファで空）。「5 木」「5 外す」「+8」で直し「OK」で確定
- 持ち越しは勝手に動かさない。「移す 番号 先」（今日・明日・曜日・来週・棚）で自分が動かす
- 「今日は休む」: 今日のタスクの移し先を番号で選ぶ。「着手 番号」: 着手日（🛫）。「どうしよう」など: 5分でできる一歩
日付はすべて活動日（SPEC §7）。週は月曜〜日曜。
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date, timedelta

import claude_client
import sheets
import state
import task_dates
import task_sync
import util
import works

log = logging.getLogger("life-os.week_plan")

KIND_PLAN = "week_plan"
KIND_REST = "rest_choice"
KIND_SORT = "sort_one"
PER_DAY = 3
EXTRA_SHOW = 10
WD = task_dates.WEEKDAYS
PLANNED_KEY = "week_planned:{}"

_PLAN = re.compile(r"^\s*(来週|今週)の計画\s*[?？!！。]*\s*$")
_REST = re.compile(r"^\s*今日は(?:休む|休みます|お休み)\s*[!！。]*\s*$")
_MOVE = re.compile(r"^\s*移す\s+([\d\s,、，と]+?)\s+(\S+)\s*$")
_START = re.compile(r"^\s*着手\s*([\d\s,、，と]+)\s*$")
_FIRST = re.compile(r"^\s*(?:どうしよう|手が止まった|やる気が出ない|最初の5分)\s*(\d+)?\s*[…。!！?？]*\s*$")
_SORT = re.compile(r"^\s*(?:タスク)?整理(?:する|したい|して)?\s*[!！。?？]*\s*$")
_SORT_END = re.compile(r"^\s*(?:おわり|終わり|おしまい|やめる|やめ|終了|ストップ)\s*[!！。]*\s*$")
_SORT_WORD = re.compile(r"^(?:今日|きょう|明日|あした|明後日|あさって|来週|棚|[月火水木金土日](?:曜日?)?|\d{1,2}/\d{1,2})[にへ]?$")
_OK = re.compile(r"^\s*(?:ok|OK|ｏｋ|オーケー|決定|確定|これで)\s*[!！。]*\s*$", re.I)
_CANCEL = re.compile(r"^\s*(?:やめる|やめ|キャンセル|中止)\s*[。]*\s*$")
_EDIT = re.compile(r"^(?:\+\s*(\d+)|(\d+)\s*(外す|はずす|消す|[月火水木金土日])曜?日?)$")


# ---------------------------------------------------------------- 純粋関数


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def plan_days(today: date, this_week: bool) -> list[date]:
    """今週の計画 = 今日から日曜まで。来週の計画 = 次の月曜から日曜まで。"""
    if this_week:
        return [today + timedelta(days=i) for i in range(7 - today.weekday())]
    monday = week_start(today) + timedelta(days=7)
    return [monday + timedelta(days=i) for i in range(7)]


def label(d: date) -> str:
    return f"{WD[d.weekday()]} {d.month}/{d.day}"


def parse_target(word: str, today: date) -> date | str | None:
    """「移す」の先 → 日付、"" （棚＝実行予定日を空にする）、None（読めない）。"""
    w = unicodedata.normalize("NFKC", word).strip().rstrip("にへ")
    if w in ("今日", "きょう"):
        return today
    if w in ("明日", "あした"):
        return today + timedelta(days=1)
    if w in ("明後日", "あさって"):
        return today + timedelta(days=2)
    if w in ("来週",):
        return week_start(today) + timedelta(days=7)
    if w in ("棚", "バックログ", "未定", "なし"):
        return ""
    m = re.fullmatch(r"([月火水木金土日])(?:曜日?)?", w)
    if m:
        delta = (WD.index(m.group(1)) - today.weekday()) % 7
        return today + timedelta(days=delta or 7)  # 次にくるその曜日（今日なら来週）
    return task_dates.extract(w, today).scheduled


def next_weekday(today: date) -> date:
    d = today + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _pending(t: dict) -> bool:
    return not t.get("done") and not t.get("deleted")


def build(tasks: list[dict], days: list[date], today: date) -> dict:
    """割り振りの案。{"days": [iso], "items": [{id, content, orig}], "grid": {iso: [番号の添字]}, "extra": [添字]}"""
    day_set = {d.isoformat() for d in days}
    first, last = days[0], days[-1]
    fixed, carry, due_wk, proj, rest = [], [], [], {}, []
    for t in tasks:
        if not _pending(t):
            continue
        sd = util.parse_date_any(t.get("scheduled", ""))
        dd = util.parse_date_any(t.get("due", ""))
        if sd and sd.isoformat() in day_set:
            fixed.append((sd, t))
        elif sd and sd < today:
            carry.append(t)
        elif sd:
            continue  # 計画の週より先に予定が入っている
        elif dd and first <= dd <= last:
            due_wk.append(t)
        elif str(t.get("source", "")).startswith(works.SOURCE_PREFIX):
            proj.setdefault(t["source"], []).append(t)
        else:
            rest.append(t)
    key = sheets.task_sort_key
    carry.sort(key=key)
    due_wk.sort(key=lambda t: (t["due"], key(t)))
    rr = []  # プロジェクトを順番に1つずつ
    queues = [sorted(v, key=key) for _, v in sorted(proj.items())]
    while any(queues):
        for q in queues:
            if q:
                rr.append(q.pop(0))
    rest.sort(key=key)
    order = carry + due_wk + rr + rest

    grid: dict[str, list[int]] = {d.isoformat(): [] for d in days}
    items: list[dict] = []

    def add(t):
        items.append({"id": t["id"], "content": t["content"], "orig": t.get("scheduled", "")})
        return len(items) - 1

    for sd, t in sorted(fixed, key=lambda x: (x[0], key(x[1]))):
        grid[sd.isoformat()].append(add(t))
    work_days = [d for d in days if d.weekday() < 5]
    extra = []
    for t in order:
        dd = util.parse_date_any(t.get("due", ""))
        spots = [d for d in work_days if len(grid[d.isoformat()]) < PER_DAY]
        if dd:
            before = [d for d in spots if d <= dd]
            spots = before or spots
        if spots:
            grid[spots[0].isoformat()].append(add(t))
        else:
            extra.append(add(t))
    # 番号を、表に出る順（曜日順 → 入らなかった候補）に振り直す
    seq = [i for d in days for i in grid[d.isoformat()]] + extra
    remap = {old: new for new, old in enumerate(seq)}
    items = [items[old] for old in seq]
    grid = {k: [remap[i] for i in v] for k, v in grid.items()}
    extra = [remap[i] for i in extra]
    return {"days": [d.isoformat() for d in days], "items": items, "grid": grid, "extra": extra}


def render(plan: dict, header: str = "") -> str:
    lines = [header] if header else []
    for iso in plan["days"]:
        d = date.fromisoformat(iso)
        idx = plan["grid"].get(iso, [])
        tag = "（バッファ）" if d.weekday() >= 5 and not idx else ""
        lines.append(f"**{label(d)}**{tag}" + (f"（{len(idx)}つ）" if len(idx) > PER_DAY else ""))
        lines += [f"　{i + 1}. {plan['items'][i]['content']}" for i in idx]
    placed = {i for v in plan["grid"].values() for i in v}
    extra = [i for i in plan["extra"] if i not in placed][:EXTRA_SHOW]
    if extra:
        lines.append("\n入らなかった候補（「+番号」で足せます）：")
        lines += [f"　{i + 1}. {plan['items'][i]['content']}" for i in extra]
    return "\n".join(lines)


def apply_edits(plan: dict, text: str) -> tuple[dict, str | None] | None:
    """「5 木」「5 外す」「+8」を反映した案と、エラー文（なければ None）。直しの文でなければ None。"""
    segs = [s for s in re.split(r"[\n、,，]+|\s{2,}", unicodedata.normalize("NFKC", text).strip()) if s.strip()]
    edits = []
    for seg in segs:
        m = _EDIT.match(seg.strip().replace(" ", "")) or _EDIT.match(seg.strip())
        if not m:
            return None
        edits.append(m)
    grid = {k: list(v) for k, v in plan["grid"].items()}
    extra = list(plan["extra"])
    n = len(plan["items"])
    by_wd = {date.fromisoformat(iso).weekday(): iso for iso in plan["days"]}
    for m in edits:
        num = int(m.group(1) or m.group(2))
        if not 1 <= num <= n:
            return plan, f"{num}番は見当たりませんでした。"
        i = num - 1
        for v in grid.values():
            if i in v:
                v.remove(i)
        if m.group(1) or m.group(3) in WD:  # +N は、空いている最初の平日へ
            if m.group(1):
                spots = [iso for iso in plan["days"] if date.fromisoformat(iso).weekday() < 5
                         and len(grid[iso]) < PER_DAY] or [plan["days"][0]]
                iso = spots[0]
            else:
                iso = by_wd.get(WD.index(m.group(3)))
                if iso is None:
                    return plan, f"{m.group(3)}曜日は、この計画の期間に入っていません。"
            grid[iso].append(i)
            if i in extra:
                extra.remove(i)
        elif i not in extra:
            extra.append(i)
    return {**plan, "grid": grid, "extra": extra}, None


def sleeping(projects: list[dict]) -> list[dict]:
    """週の計画のときだけ出す「寝かせ中の候補」（完成に近い順に3本）。"""
    def rank(p):
        st = p.get("status", "")
        if st == "完成間際":
            return 0
        if st == "製作中":
            return 1
        if st == "素材あり" and p.get("chars") and p.get("target") and p["chars"] >= p["target"]:
            return 2
        return 3
    cands = [p for p in projects if not p["now"] and not works.is_continuing(p)
             and p.get("status") not in ("完了", "保留", "")]
    return sorted(cands, key=lambda p: (rank(p), p.get("updated") or ""))[:3]


# ---------------------------------------------------------------- Discord


async def handle(message, text: str, items_fn) -> bool:
    """01-today-task のメッセージ。扱ったら True。items_fn: 今日の一覧の番号 → タスクを返す関数（today_task._items）。"""
    ch = message.channel
    if await _plan_reply(message, text):
        return True
    if await _rest_reply(message, text):
        return True
    if await _sort_reply(message, text):
        return True
    if _SORT.match(text):
        await _sort_next(ch, set(), first=True)
        return True
    m = _PLAN.match(text)
    if m:
        await start_plan(ch, this_week=m.group(1) == "今週")
        return True
    if _REST.match(text):
        await _rest(ch)
        return True
    m = _MOVE.match(unicodedata.normalize("NFKC", text))
    if m:
        await _move(message, util.parse_numbers(m.group(1)) or [], m.group(2), items_fn)
        return True
    m = _START.match(unicodedata.normalize("NFKC", text))
    if m and util.parse_numbers(m.group(1)):
        await _start(message, util.parse_numbers(m.group(1)), items_fn)
        return True
    m = _FIRST.match(unicodedata.normalize("NFKC", text))
    if m:
        await _first_step(ch, int(m.group(1)) if m.group(1) else None, items_fn)
        return True
    return False


NEW_PREFIX = "new:"  # 案の中だけの、プロジェクトの「次やること」から作るタスク（OK で作る）


def with_projects(tasks: list[dict], projects: list[dict], today: date) -> list[dict]:
    """週の計画の候補に、今やるプロジェクトの「次やること」（まだタスクになっていないもの）を足す。
    締切のあるプロジェクトのタスクは、目標日（2週間前）を置き場所の期限にする（シートには書かない）。"""
    import board
    out = [dict(t) for t in tasks]
    open_contents = {t["content"] for t in tasks if _pending(t)}
    by_src = {}
    for p in works.now_projects(projects):
        d = board.parse_deadline(p.get("deadline") or "", today)
        goal = board.milestones(d)[0][1] if d else None
        by_src[works.source_of(p)] = (p, goal)
        content = works.task_content(p, p["next"]) if p.get("next") else ""
        if content and content not in open_contents:
            out.append({"id": f"{NEW_PREFIX}{p['no']}", "content": content, "scheduled": "", "due": "",
                        "priority": "", "source": works.source_of(p), "done": False, "deleted": False, "row": 10**6})
    for t in out:
        hit = by_src.get(t.get("source", ""))
        if hit and hit[1] and _pending(t):
            due = util.parse_date_any(t.get("due", ""))
            if due is None or hit[1] < due:
                t["due"] = hit[1].isoformat()
    return out


async def start_plan(ch, this_week: bool) -> None:
    today = util.activity_day()
    days = plan_days(today, this_week)
    tasks = await asyncio.to_thread(sheets.all_tasks)
    projects = []
    if works.enabled():
        try:
            projects = await asyncio.to_thread(works.load)
        except Exception:  # noqa: BLE001
            log.warning("作品管理シートを読めませんでした（プロジェクトの次やることは省きます）", exc_info=True)
    plan = build(with_projects(tasks, projects, today), days, today)
    head = f"🗓 {'今週' if this_week else '来週'}の計画の案です（{label(date.fromisoformat(plan['days'][0]))}〜{label(date.fromisoformat(plan['days'][-1]))}）"
    body = render(plan, head) if plan["items"] else head + "\n入れるタスクが見当たりませんでした。タスクを足してから、もう一度どうぞ。"
    sleepers = []
    if projects:
        sleepers = sleeping(projects)
    if sleepers:
        body += "\n\n💤 寝かせ中の作品（入れ替えるなら、プロジェクトの部屋で『今やる 番号』）：\n" + "\n".join(
            f"・{works.label(p)}（{p.get('status')}）" for p in sleepers)
    if plan["items"]:
        body += "\n\n直すときは「5 木」「5 外す」「+8」のように返信してください。よければ「OK」、やめるなら「やめる」。"
    msg = await util.send_long(ch, body)
    if plan["items"]:
        state.put_pending(msg.id, ch.id, KIND_PLAN, plan)


def _find_pending(message, kind: str, hours: float):
    ref = getattr(message, "reference", None)
    if ref and getattr(ref, "message_id", None):
        p = state.get_pending(ref.message_id)
        if p and p["kind"] == kind:
            return p
    return state.latest_pending(message.channel.id, kind, max_age_hours=hours)


async def _plan_reply(message, text: str) -> bool:
    ok, cancel = _OK.match(text), _CANCEL.match(text)
    first = unicodedata.normalize("NFKC", text).strip().split("\n")[0].strip()
    if not (ok or cancel or _EDIT.match(first.replace(" ", "")) or _EDIT.match(first)):
        return False
    pending = _find_pending(message, KIND_PLAN, 24)
    if pending is None:
        return False
    ch = message.channel
    plan = pending["payload"]
    if cancel:
        state.delete_pending(pending["message_id"])
        await ch.send("計画はやめておきました。何も変えていません。")
        return True
    if ok:
        state.delete_pending(pending["message_id"])
        await _commit(message, plan)
        return True
    result = apply_edits(plan, text)
    if result is None:
        return False
    plan2, err = result
    if err:
        await ch.send(err + "もう一度どうぞ。")
        return True
    state.delete_pending(pending["message_id"])
    msg = await util.send_long(ch, render(plan2, "🗓 直しました：") + "\n\nよければ「OK」、さらに直すなら「5 木」のように。")
    state.put_pending(msg.id, ch.id, KIND_PLAN, plan2)
    return True


async def _commit(message, plan: dict) -> None:
    today = util.activity_day()
    tasks = {t["id"]: t for t in await asyncio.to_thread(sheets.all_tasks)}
    placed = {i: iso for iso, v in plan["grid"].items() for i in v}
    ups, created = [], False
    for i, it in enumerate(plan["items"]):
        if it["id"].startswith(NEW_PREFIX):  # プロジェクトの次やること → 置いた日のタスクとして作る
            if i in placed:
                await asyncio.to_thread(sheets.add_task, it["content"], scheduled=placed[i],
                                        source=works.SOURCE_PREFIX + it["id"][len(NEW_PREFIX):])
                created = True
            continue
        t = tasks.get(it["id"])
        if t is None or not _pending(t):
            continue
        if i in placed:
            if t.get("scheduled") != placed[i]:
                ups.append((t["row"], {"実行予定日": placed[i]}))
        else:
            orig = util.parse_date_any(it.get("orig", ""))
            if orig and orig < today and t.get("scheduled"):  # 案から外した持ち越しは棚へ
                ups.append((t["row"], {"実行予定日": ""}))
    if ups:
        await asyncio.to_thread(sheets.update_rows, sheets.TASKS, ups)
    if ups or created:
        await asyncio.to_thread(task_sync.run_safely)
    monday = week_start(date.fromisoformat(plan["days"][0]))
    state.set_kv(PLANNED_KEY.format(monday.isoformat()), "1")
    await util.ack(message, "🗓")
    lines = ["🗓 計画を決めました。変えたくなったら、いつでも『移す 番号 先』で動かせます。"]
    for iso in plan["days"]:
        idx = plan["grid"].get(iso, [])
        if idx:
            lines.append(f"{label(date.fromisoformat(iso))}: " + "、".join(plan["items"][i]["content"] for i in idx))
    await util.send_long(message.channel, "\n".join(lines))


def planned(monday: date) -> bool:
    return bool(state.get_kv(PLANNED_KEY.format(monday.isoformat())))


async def _move(message, nums: list[int], word: str, items_fn) -> None:
    ch = message.channel
    today = util.activity_day()
    target = parse_target(word, today)
    if target is None:
        await ch.send("移す先が読めませんでした。「今日」「明日」「金」「来週」「棚」のどれかで書いてください（例: 移す 2 明日）。")
        return
    items = await items_fn(ch)
    chosen = [items[n - 1] for n in dict.fromkeys(nums) if 1 <= n <= len(items)]
    if not chosen:
        await ch.send("その番号のタスクは見当たりませんでした。もう一度どうぞ。")
        return
    value = target.isoformat() if isinstance(target, date) else ""
    for it in chosen:
        await asyncio.to_thread(sheets.update_task, it["id"], scheduled=value)
    await asyncio.to_thread(task_sync.run_safely)
    await util.ack(message, "📆")
    where = "棚（日付なし）" if value == "" else task_dates.fmt_md(target)
    from handlers import today_task  # 循環を避ける
    await today_task.post_list(ch, f"📆 {where}に移しました：\n" + "\n".join(f"・{it['content']}" for it in chosen))


async def _rest(ch) -> None:
    from handlers import today_task
    tasks = today_task.ordered(await asyncio.to_thread(sheets.today_tasks, util.activity_day()))
    if not tasks:
        await ch.send("今日はゆっくり休んでください🌙 今日のタスクは、もともとありません。")
        return
    msg = await ch.send("今日はゆっくり休んでください🌙\n今日のタスク（" + "、".join(t["content"] for t in tasks[:5])
                        + ("…" if len(tasks) > 5 else "") + "）は、どうしますか？\n"
                        "1. 明日にする\n2. 次の平日にする\n3. 棚に戻す（来週の計画で決める）\n4. そのまま（明日の一覧に持ち越し）\n番号で返信してください。")
    state.put_pending(msg.id, ch.id, KIND_REST, {"ids": [t["id"] for t in tasks]})


async def _rest_reply(message, text: str) -> bool:
    t = unicodedata.normalize("NFKC", text).strip()
    if t not in ("1", "2", "3", "4"):
        return False
    pending = _find_pending(message, KIND_REST, 12)
    if pending is None:
        return False
    state.delete_pending(pending["message_id"])
    today = util.activity_day()
    value = {"1": (today + timedelta(days=1)).isoformat(), "2": next_weekday(today).isoformat(), "3": "", "4": None}[t]
    if value is not None:
        for tid in pending["payload"]["ids"]:
            await asyncio.to_thread(sheets.update_task, tid, scheduled=value)
        await asyncio.to_thread(task_sync.run_safely)
    await util.ack(message, "🌙")
    await message.channel.send({"1": "明日にしました。", "2": "次の平日にしました。", "3": "棚に戻しました。",
                                "4": "そのままにしておきます。"}[t] + "おやすみなさい🌙")
    return True


async def _start(message, nums: list[int], items_fn) -> None:
    ch = message.channel
    items = await items_fn(ch)
    chosen = [items[n - 1] for n in dict.fromkeys(nums) if 1 <= n <= len(items)]
    if not chosen:
        await ch.send("その番号のタスクは見当たりませんでした。もう一度どうぞ。")
        return
    day = util.fmt_date(util.activity_day())
    for it in chosen:
        await asyncio.to_thread(sheets.update_task, it["id"], start=day)
    await asyncio.to_thread(task_sync.run_safely)
    await util.ack(message, "🌱")
    await ch.send("手を付けました🌱\n" + "\n".join(f"・{it['content']}" for it in chosen))


_FIRST_PROMPT = """次のタスクに取りかかるための「5分でできる、いちばん小さな一歩」を1つだけ、40文字以内で書いてください。
作業の内容だけ（評価・説教・励ましは書かない）。JSON だけを返す: {{"step": "..."}}
タスク: {task}"""


async def _first_step(ch, num: int | None, items_fn) -> None:
    items = await items_fn(ch)
    if not items:
        await ch.send("今日のタスクはありません。休むのも大事な予定です。")
        return
    it = items[num - 1] if num and 1 <= num <= len(items) else items[0]
    data = await claude_client.complete_json(_FIRST_PROMPT.format(task=it["content"]), {"step": ""})
    step = str((data or {}).get("step") or "").strip() if isinstance(data, dict) else ""
    await ch.send(f"まずは5分だけ: {step or 'このタスクのファイル（または道具）を開くところまで'}\n（{it['content']}）")


# ---------------------------------------------------------------- 整理（1つずつ日を決める）


def sort_choices(today: date) -> list[tuple[str, date | None]]:
    fri = parse_target("金", today) if today.weekday() != 4 else today
    return [("今日", today), ("明日", today + timedelta(days=1)), (f"金（{task_dates.fmt_md(fri)}）", fri),
            (f"来週（{task_dates.fmt_md(week_start(today) + timedelta(days=7))}）", week_start(today) + timedelta(days=7)),
            ("このまま（あとで）", None)]


async def _sort_next(ch, skip: set[str], first: bool = False) -> None:
    from handlers import today_task
    day = util.activity_day()
    today = await asyncio.to_thread(sheets.today_tasks, day)
    redo, undated = today_task.to_decide(await asyncio.to_thread(sheets.all_tasks), {t["id"] for t in today}, day)
    nxt = next((t for t in redo + undated if t["id"] not in skip), None)
    if nxt is None:
        await ch.send("ぜんぶ決まりました🌿" if not first else "日を決めたいタスクは、今のところありません🌿")
        return
    head = "🔁" if nxt in redo else "🗂"
    opts = "\n".join(f"{i}. {lbl}" for i, (lbl, _) in enumerate(sort_choices(day), 1))
    note = "\n（『おわり』でいつでも終われます。やらなくなったタスクは Obsidian で消してください）" if first else ""
    msg = await ch.send(f"{head} 「{today_task._with_due(nxt)}」はいつにしますか？\n{opts}\n"
                        f"番号か「水」「10/25」のように返信してください。{note}")
    state.put_pending(msg.id, ch.id, KIND_SORT, {"id": nxt["id"], "content": nxt["content"], "skip": sorted(skip)})


async def _sort_reply(message, text: str) -> bool:
    pending = _find_pending(message, KIND_SORT, 12)
    if pending is None:
        return False
    t = unicodedata.normalize("NFKC", text).strip()
    payload = pending["payload"]
    ch = message.channel
    if _SORT_END.match(t):
        state.delete_pending(pending["message_id"])
        await ch.send("ここまでにしておきます。決めた分は、そのまま入っています。")
        return True
    today = util.activity_day()
    choices = sort_choices(today)
    if t.isdigit() and 1 <= int(t) <= len(choices):
        target = choices[int(t) - 1][1]
        keep = target is None
    elif _SORT_WORD.match(t) and (target := parse_target(t, today)) is not None:
        keep = target == ""  # 「棚」= 日付なしのまま
    else:
        return False  # 整理の答えでなければ、ふつうのメッセージとして扱う
    state.delete_pending(pending["message_id"])
    skip = set(payload.get("skip", [])) | {payload["id"]}
    if not keep:
        await asyncio.to_thread(sheets.update_task, payload["id"], scheduled=target.isoformat())
        await asyncio.to_thread(task_sync.run_safely)
        await ch.send(f"📆 {task_dates.fmt_md(target)}にしました。")
    await _sort_next(ch, skip)
    return True

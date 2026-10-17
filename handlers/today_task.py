"""`01-today-task`: 朝の案内、今日のタスクの表示、緊急タスクの追加、完了の記録、メインとサブの入れ替え（SPEC §5）。"""
import asyncio
import logging
import re
import unicodedata
from datetime import date, timedelta

import done_list
import sheets
import state
import task_dates
import task_sync
import util
import works
import board
from handlers import week_plan

log = logging.getLogger("life-os.today_task")

MAIN_MAX = 3
MAIN = "高"  # メイン = 優先度「高」（Obsidian の ⏫）。サブ = それ以外（SPEC §5 01-today-task）
_DONE = re.compile(r"^\s*(?:完了|done|かんりょう)[\s:：]*(.+)$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*・•]|\[[ xX]?\])\s*")
# 一覧を聞く言葉（メッセージ全体がこれだけのとき。タスクとして登録しない。SPEC §5 01-today-task）
_ASK_TAIL = r"\s*(?:は|って|を)?\s*(?:何|なに|なん|ある|どれ|どんな|教えて|おしえて|見せて|みせて|表示|一覧|ですか|かな|でしょう|して)*\s*[?？!！。]*\s*$"
_SHOW = re.compile(r"^\s*(?:今日の(?:タスク|予定|やること)|今日やること|タスク|一覧|リスト)" + _ASK_TAIL)
_SHOW_ALL = re.compile(r"^\s*(?:(?:今ある|いまある|全部の|ぜんぶの|全ての|すべての|残りの|残ってる|のこりの|未完了の)(?:タスク|やること)"
                       r"|タスク(?:全部|ぜんぶ|すべて|全て)|やることリスト|全タスク)" + _ASK_TAIL)
_MORE = re.compile(r"^\s*(?:続き|つづき|もっと)(?:を)?(?:見せて|みせて|表示)?\s*[!！。?？]*\s*$")
_FUTURE = re.compile(r"^\s*(?:今後|これから|先|未来)の(?:予定|タスク)" + _ASK_TAIL)
_KIND = re.compile(r"^\s*(メイン|サブ)(?:\s*[:：]\s*|\s+)(.+)$", re.S)
# 登録時の区分: 行末の「（サブ）」「【メイン】」「#サブ」、または「サブ：」だけの行（以降の行に効く）
_KIND_SUFFIX = re.compile(r"^(.*?)\s*(?:[(（【\[]\s*(メイン|サブ)(?:タスク)?\s*[)）】\]]|[#＃](メイン|サブ))\s*$", re.S)
_KIND_HEADER = re.compile(r"^\s*(メイン|サブ)(?:タスク)?\s*[:：]?\s*$")

# 番号でメイン/サブを変える命令（NFKC 後の1行に対して使う）。例: 「メイン 2 サブ 5」「メイン2・サブ5」
# 「2をメインに、5をサブにして」「2番はサブ」「2と5を入れ替え」「入れ替え 2 5」
_NUMS = r"\d+(?:\s*(?:[,、・&]|と|\s)\s*\d+)*"
_BAN = r"\s*(?:番目?)?"
_TAIL = r"\s*(?:して|する|変更|変えて|移して|移動|お願い(?:します)?|ください|下さい|で|て|る)*"
_SWAP = r"(?:入れ替え|入替え?|交換)"
_CMD = re.compile(
    rf"(?P<k1>メイン|サブ)(?:タスク)?\s*(?:[:は]|に)?\s*(?P<n1>{_NUMS}){_BAN}"
    rf"|(?P<n2>{_NUMS}){_BAN}\s*(?:の(?:タスク)?)?\s*(?:を|は|が|も)?\s*(?P<k2>メイン|サブ)(?:タスク)?\s*(?:に|へ)?{_TAIL}"
    rf"|{_SWAP}\s*:?\s*(?P<n3>{_NUMS}){_BAN}{_TAIL}"
    rf"|(?P<n4>{_NUMS}){_BAN}\s*(?:を|は)?\s*{_SWAP}{_TAIL}")
_CMD_SEP = re.compile(r"(?:[\s、,・/。.!]|と|そして|それから)*")
_CMD_WORDS = re.compile(r"(?:メイン|サブ|タスク|入れ替え|入替え?|交換|\d+|番|目|を|は|が|に|へ|の|と|して|する|て|"
                        r"ください|お願い|します|[\s、,・/:。.!])+")
SWAP = "入れ替え"
SOURCE = "01-today-task"
MORNING_PREFIX = "おはようございます！ 本日のタスク案内"
PLAN_HINT = "🗓 今週の計画がまだなら『今週の計画』と書くと一緒に作れます（なしでも大丈夫です）"
HINT = "「完了 1,3」で完了、「メイン 4」「サブ 1」「メイン 4 サブ 1」「2と5を入れ替え」でメインとサブを変えられます"
FULL_NOTE = "メインが3件そろっているので、サブに入れました（「メイン 4 サブ 1」のように番号で入れ替えられます）"
OVER_NOTE = "メインは3件までです。今回は何も変えていません。「メイン 4 サブ 1」のように、サブにする番号も一緒に書くと入れ替えられます。"
CMD_HELP = "番号の指定を読み取れませんでした。例: 「メイン 2」「サブ 5」「メイン 2 サブ 5」「2をメインに」「2と5を入れ替え」"


def arrange(tasks: list[dict]) -> tuple[list[dict], list[dict], bool]:
    """(メイン, サブ, 「高」が4件以上あったか)。メインは優先度「高」の先頭3件だけ。「高」が無ければメインは空（自動で選ばない）。"""
    ordered_ = sorted(tasks, key=sheets.task_sort_key)
    high = [t for t in ordered_ if t.get("priority") == MAIN]
    main = high[:MAIN_MAX]
    picked = {id(t) for t in main}
    return main, [t for t in ordered_ if id(t) not in picked], len(high) > MAIN_MAX


def ordered(tasks: list[dict]) -> list[dict]:
    """番号の順（メインから通し番号）。"""
    main, sub, _ = arrange(tasks)
    return main + sub


def format_task_list(tasks: list[dict]) -> str:
    """メイン(最大3件)とサブ(無制限)を、通し番号つきで表示する。"""
    main, sub, overflow = arrange(tasks)
    lines: list[str] = []
    if main:
        lines.append("本日のメインタスク（最大3つ）：")
        lines += [f"{i}. {t['content']}" for i, t in enumerate(main, 1)]
    if sub:
        if lines:
            lines.append("")
        lines.append("サブタスク（気分に合わせて）：")
        lines += [f"{i}. {t['content']}" for i, t in enumerate(sub, len(main) + 1)]
    if overflow:
        lines.append("")
        lines.append("（メインは3件までなので、4件目からはサブに表示しています）")
    return "\n".join(lines)


def compose_morning(tasks: list[dict], health_line: str | None, overnight: list[str] | None = None,
                    projects: str | None = None, day: date | None = None, plan_hint: bool = False,
                    sort_hint: bool = False) -> str:
    """朝の案内の本文。昨日の執筆実績は、当日の記録（09:44）が入ったあとの別の投稿（scheduler.post_writing）で出す。

    overnight: 前日の夜の問いかけのあと（〜今朝）に完了したタスク。夜の問いかけに載らなかった分をここで出す。
    """
    day = day or util.activity_day()
    parts = [MORNING_PREFIX + " ☀️"]
    if health_line:
        parts.append(health_line)
    done = done_list.format_items(done_list.OVERNIGHT_TITLE, overnight or [])
    if done:
        parts.append(done)
    if projects:  # 📂 今やるプロジェクト（作品管理シート。SPEC §5）
        parts.append(projects)
    if tasks:
        parts.append(format_task_list(tasks))
        note = HINT
        if any((d := util.parse_date_any(t.get("scheduled", ""))) and d < day for t in tasks):
            note += "。持ち越しは『移す 番号 明日』のように動かせます"
        parts.append(f"（{note}）")
    else:
        parts.append("今日のタスクはまだありません。ここに書き込むと、今日のタスクに追加されます。夜の振り返りで、明日やるものを選ぶこともできます🌙")
    if sort_hint:  # 日を決めたいタスクがあるときだけ。一覧も件数も出さない
        parts.append(SORT_HINT)
    if plan_hint:
        parts.append(PLAN_HINT)
    return "\n\n".join(parts)


def health_line(score: int | None, mood: str | None) -> str | None:
    bits = []
    if score:
        bits.append(f"体調スコア {score}")
    if mood:
        bits.append(f"ご機嫌度 {mood}")
    return ("🩺 昨日: " + " / ".join(bits)) if bits else None


async def build_morning(day: date) -> tuple[str, list[dict]]:
    """朝の案内の本文と、番号に対応するタスク一覧（番号の順）。持ち越しは勝手に動かさない（SPEC §5「週の計画」）。"""
    await asyncio.to_thread(task_sync.run_safely)
    tasks = ordered(await asyncio.to_thread(sheets.today_tasks, day))
    y = day - timedelta(days=1)
    score = await asyncio.to_thread(sheets.health_score_on, y)
    diary = await asyncio.to_thread(sheets.diary_on, y)
    line = health_line(score, (diary or {}).get("ご機嫌度") or None)
    plan_hint = day.weekday() == 0 and not week_plan.planned(day)
    try:
        redo, undated = to_decide(await asyncio.to_thread(sheets.all_tasks), {t["id"] for t in tasks}, day)
    except Exception:  # noqa: BLE001
        log.warning("日を決めたいタスクを確かめられませんでした（朝の案内では省きます）", exc_info=True)
        redo, undated = [], []
    return compose_morning(tasks, line, await done_list.overnight_done(day), await projects_section(day),
                           day, plan_hint, bool(redo or undated)), tasks


async def projects_section(day: date) -> str | None:
    """朝の案内の「📂 今やるプロジェクト」。作品管理シートが未設定・読めないときは省く。"""
    if not works.enabled():
        return None
    try:
        projects = await asyncio.to_thread(works.load, True)
        tasks = await asyncio.to_thread(sheets.all_tasks) if any(works.is_continuing(p) for p in works.now_projects(projects)) else []
        return works.morning_section(projects, tasks, day)
    except Exception:  # noqa: BLE001
        log.warning("作品管理シートを読めませんでした（朝の案内では省きます）", exc_info=True)
        return None


async def post_list(channel, header: str | None = None) -> None:
    """現在の今日のタスクを投稿し、番号に対応する状態を保存する。"""
    tasks = ordered(await asyncio.to_thread(sheets.today_tasks, util.activity_day()))
    body = (format_task_list(tasks) + f"\n\n（{HINT}）") if tasks else "今日のタスクは、今のところありません。"
    msg = await util.send_long(channel, (header + "\n\n" if header else "") + body)
    state.put_pending(msg.id, channel.id, "today_list", {"items": [{"id": t["id"], "content": t["content"]} for t in tasks]})


SECTION_MAX = 5
REDO_TITLE = "🔁 日を決め直すタスク："
UNDATED_TITLE = "🗂 日がまだ決まっていないタスク："
MORE_NOTE = "（続きは『続き』）"
SORT_HINT = "日を決めたいタスクがあれば『整理』でどうぞ（しなくても大丈夫です）"


def next_hint(touched: list[dict], day: date) -> str | None:
    """プロジェクトのタスクを完了したときの一言（その日1回まで。急かさない）。"""
    key = f"board_hint:{day.isoformat()}"
    if state.get_kv(key):
        return None
    p = touched[0]
    state.set_kv(key, "1")
    return f"📂 『{p['title']}』の次やることは、12 の部屋で『次やること {p['no']} 内容』か『タスク案 {p['no']}』で決められます"


def to_decide(all_tasks: list[dict], today_ids: set[str], day: date) -> tuple[list[dict], list[dict]]:
    """実行日の無い未完了タスクを「日を決め直す」（期限が過ぎた）と「日がまだ決まっていない」に分ける。
    実行日が今日以前のもの（今日の一覧に出る）と未来のもの（出さない）は入れない。"""
    redo, undated = [], []
    for t in all_tasks:
        if t["done"] or t["deleted"] or t["id"] in today_ids or util.parse_date_any(t["scheduled"]):
            continue
        due = util.parse_date_any(t["due"])
        (redo if due and due < day else undated).append(t)
    redo.sort(key=lambda t: (util.parse_date_any(t["due"]), sheets.task_sort_key(t)))
    undated.sort(key=sheets.task_sort_key)
    return redo, undated


def future_tasks(all_tasks: list[dict], day: date) -> list[dict]:
    out = [t for t in all_tasks if not t["done"] and not t["deleted"]
           and (d := util.parse_date_any(t["scheduled"])) and d > day]
    return sorted(out, key=lambda t: (util.parse_date_any(t["scheduled"]), sheets.task_sort_key(t)))


def _with_due(t: dict) -> str:
    d = util.parse_date_any(t["due"])
    return t["content"] + (f"（期限 {task_dates.fmt_md(d)}）" if d else "")


def format_sections(redo: list[dict], undated: list[dict], start: int, offset: int = 0) -> tuple[list[str], list[dict], bool]:
    """②③を offset から5つずつ。番号は start から。(本文の欄, 番号順のタスク, まだ残りがあるか)"""
    parts, shown, n = [], [], start
    for title, tasks in ((REDO_TITLE, redo), (UNDATED_TITLE, undated)):
        page = tasks[offset:offset + SECTION_MAX]
        if page:
            parts.append(title + "\n" + "\n".join(f"{i}. {_with_due(t)}" for i, t in enumerate(page, n)))
            n += len(page)
            shown += page
    more = len(redo) > offset + SECTION_MAX or len(undated) > offset + SECTION_MAX
    return parts, shown, more


def format_all(today: list[dict], all_tasks: list[dict], day: date) -> tuple[str, list[dict]]:
    """「今あるタスクは？」: 今日（番号つき）・日を決め直す・日がまだ決まっていない（5つずつ）。未来のタスクと件数は出さない。"""
    redo, undated = to_decide(all_tasks, {t["id"] for t in today}, day)
    parts = [format_task_list(today) if today else "今日のタスクは、今のところありません。"]
    sections, shown, more = format_sections(redo, undated, len(today) + 1)
    parts += sections
    if more:
        parts.append(MORE_NOTE)
    return "\n\n".join(parts), shown


def _save_list(msg, channel, tasks: list[dict], **extra) -> None:
    state.put_pending(msg.id, channel.id, "today_list",
                      {"items": [{"id": t["id"], "content": t["content"]} for t in tasks], **extra})


async def post_all(channel) -> None:
    """今あるタスク（完了・削除・未来を除く）を投稿する。番号は今日の一覧から続けて振る。"""
    day = util.activity_day()
    today = ordered(await asyncio.to_thread(sheets.today_tasks, day))
    every = await asyncio.to_thread(sheets.all_tasks)
    body, shown = format_all(today, every, day)
    if today or shown:
        body += f"\n\n（{HINT}。日を決めるなら『移す 番号 金』や『整理』）"
    msg = await util.send_long(channel, body)
    _save_list(msg, channel, today + shown, offset=0)


async def post_more(channel) -> None:
    """「続き」: 直前の「今あるタスク」の②③の、次の5つずつ。"""
    pending = state.latest_pending(channel.id, "today_list", max_age_hours=36)
    offset = (pending["payload"].get("offset") if pending else None)
    if offset is None:
        await post_all(channel)
        return
    offset += SECTION_MAX
    day = util.activity_day()
    today = ordered(await asyncio.to_thread(sheets.today_tasks, day))
    redo, undated = to_decide(await asyncio.to_thread(sheets.all_tasks), {t["id"] for t in today}, day)
    sections, shown, more = format_sections(redo, undated, len(today) + 1, offset)
    if not shown:
        await channel.send("ここまでです。")
        return
    msg = await util.send_long(channel, "\n\n".join(sections + ([MORE_NOTE] if more else [])))
    _save_list(msg, channel, today + shown, offset=offset)


async def post_future(channel) -> None:
    """「今後の予定」: 実行日が未来のタスク（見たいときだけ）。"""
    day = util.activity_day()
    today = ordered(await asyncio.to_thread(sheets.today_tasks, day))
    future = future_tasks(await asyncio.to_thread(sheets.all_tasks), day)
    if not future:
        await channel.send("先の日付が入っているタスクは、今のところありません。")
        return
    lines = [f"{i}. {task_dates.fmt_md(util.parse_date_any(t['scheduled']))} {t['content']}"
             for i, t in enumerate(future, len(today) + 1)]
    msg = await util.send_long(channel, "📅 今後の予定：\n" + "\n".join(lines) + "\n\n（動かすなら『移す 番号 先』）")
    _save_list(msg, channel, today + future)


async def _items(channel) -> list[dict]:
    """番号に対応するタスク。チャンネル内の最新の一覧（36時間以内）、無ければ今の今日のタスク。"""
    pending = state.latest_pending(channel.id, "today_list", max_age_hours=36)
    if pending:
        return pending["payload"]["items"]
    return [{"id": t["id"], "content": t["content"]} for t in ordered(await asyncio.to_thread(sheets.today_tasks, util.activity_day()))]


def plan_task(line: str, today: date, main_free: int = MAIN_MAX, kind: str | None = None) -> dict:
    """1行のタスク文から、登録する内容・実行日・期限・優先度と、確認用の表記を決める。

    実行日の言葉が無ければ「今日」（この部屋は「今日絶対やる」タスクの入口）。実行日が今日以前なら、
    メインに空き（main_free）があれば優先度「高」（メイン）、無ければ優先度なし（サブ。demoted=True）。
    実行日が未来なら優先度なしで、その日の朝の案内に出る（今日の一覧には出ない）。
    kind: 「メイン」「サブ」の指定。「メイン」なら未来の日でも「高」、「サブ」なら常に優先度なし。
    """
    ext = task_dates.extract(line, today)
    scheduled = ext.scheduled or today
    future = scheduled > today
    detail = task_dates.describe(ext.scheduled, ext.due)  # 書かれた日付だけを表示する
    priority, demoted = "", False
    if kind == "メイン" and future:
        priority = MAIN
    elif kind != "サブ" and not future:
        if main_free > 0:
            priority = MAIN
        else:
            demoted = True
    return {"content": ext.content, "scheduled": task_dates.iso(scheduled), "due": task_dates.iso(ext.due),
            "priority": priority, "future": future, "demoted": demoted,
            "label": f"{ext.content}（{detail}）" if detail else ext.content}


def _lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = _BULLET.sub("", raw).strip()
        if line:
            out.append(line)
    return out


def parse_kind_commands(line: str) -> list[tuple[str, list[int]]] | None:
    """1行が「番号でメイン/サブを変える命令」だけでできていれば [(メイン|サブ|入れ替え, 番号)]、そうでなければ None。"""
    t = unicodedata.normalize("NFKC", line).strip()
    out, pos = [], 0
    for m in _CMD.finditer(t):
        if not _CMD_SEP.fullmatch(t[pos:m.start()]):
            return None
        g = m.groupdict()
        kind = g["k1"] or g["k2"] or SWAP
        nums = g["n1"] or g["n2"] or g["n3"] or g["n4"]
        out.append((kind, [int(n) for n in re.findall(r"\d+", nums)]))
        pos = m.end()
    if not out or not _CMD_SEP.fullmatch(t[pos:]):
        return None
    return out


def looks_like_command(line: str) -> bool:
    """命令の言葉と番号だけの行（読み取れなかった命令）。タスクとして登録しないため。"""
    t = unicodedata.normalize("NFKC", line).strip()
    return bool(_CMD_WORDS.fullmatch(t) and re.search(r"\d", t) and re.search(r"メイン|サブ|入れ替え|入替|交換", t))


def line_kind(line: str) -> tuple[str | None, str]:
    """登録する1行の区分（先頭の「メイン：」「サブ：」、または行末の「（サブ）」「#サブ」）と本文。"""
    m = _KIND.match(line)
    if m:
        return m.group(1), m.group(2).strip()
    m = _KIND_SUFFIX.match(line)
    if m and m.group(1).strip():
        return m.group(2) or m.group(3), m.group(1).strip()
    return None, line


def plan_kinds(cmds: list[tuple[str, list[int]]], items: list[dict], current: dict[str, dict]) -> dict:
    """命令をまとめて1つの変更にする（後の指定が勝つ）。メインが3件を超えて増えるなら over=True（何も変えない）。"""
    target: dict[str, str] = {}
    missing: list[int] = []
    for kind, nums in cmds:
        for n in nums:
            tid = items[n - 1]["id"] if 1 <= n <= len(items) else None
            if tid not in current:
                missing.append(n)
                continue
            if kind == SWAP:
                now = target.get(tid) or ("メイン" if current[tid]["priority"] == MAIN else "サブ")
                kind_ = "サブ" if now == "メイン" else "メイン"
            else:
                kind_ = kind
            target[tid] = kind_
    before = {i for i, t in current.items() if t["priority"] == MAIN}
    after = set(before)
    for tid, k in target.items():
        (after.add if k == "メイン" else after.discard)(tid)
    return {"to_main": [i for i, k in target.items() if k == "メイン" and current[i]["priority"] != MAIN],
            "to_sub": [i for i, k in target.items() if k == "サブ" and current[i]["priority"] == MAIN],
            "kinds": set(target.values()), "missing": list(dict.fromkeys(missing)),
            "over": len(after) > MAIN_MAX and len(after) > len(before), "found": bool(target)}


async def _apply_kinds(channel, cmds: list[tuple[str, list[int]]]) -> tuple[str, bool]:
    """(返信の文, 変更したか)。メインが3件を超えるなら何も変えない（一部だけ反映しない）。"""
    items = await _items(channel)
    current = {t["id"]: t for t in await asyncio.to_thread(sheets.today_tasks, util.activity_day())}
    plan = plan_kinds(cmds, items, current)
    if not plan["found"]:
        return "その番号のタスクは見当たりませんでした。もう一度どうぞ。", False
    if plan["over"]:
        return OVER_NOTE, False
    to_sub = await asyncio.to_thread(sheets.set_priority, plan["to_sub"], "") if plan["to_sub"] else []
    to_main = await asyncio.to_thread(sheets.set_priority, plan["to_main"], MAIN) if plan["to_main"] else []
    parts = []
    if to_main:
        parts.append("メインにしました：\n" + "\n".join(f"・{c}" for c in to_main))
    if to_sub:
        parts.append("サブにしました：\n" + "\n".join(f"・{c}" for c in to_sub))
    if not parts:
        kinds = plan["kinds"]
        parts.append(f"すでに{next(iter(kinds))}になっています。" if len(kinds) == 1 else "すでにそのとおりになっています。")
    if plan["missing"]:
        parts.append("（" + "・".join(f"{n}番" for n in plan["missing"]) + "は見当たりませんでした）")
    return "\n".join(parts), bool(to_main or to_sub)


async def _add_tasks(entries: list[tuple[str | None, str]]) -> str:
    """今日絶対やる緊急タスクを登録する。実行日が今日以前なら、メインの空きを上の行から埋める。"""
    today = util.activity_day()
    current = await asyncio.to_thread(sheets.today_tasks, today)
    main_free = MAIN_MAX - sum(1 for t in current if t["priority"] == MAIN)
    added, future, demoted = [], False, False
    for kind, body in entries:
        plan = plan_task(body, today, main_free, kind)
        await asyncio.to_thread(sheets.add_task, plan["content"], scheduled=plan["scheduled"], due=plan["due"],
                                priority=plan["priority"], source=SOURCE)
        if plan["priority"] == MAIN and not plan["future"]:
            main_free -= 1
        added.append(plan["label"] + ("（サブ）" if kind == "サブ" else ""))
        future = future or plan["future"]
        demoted = demoted or plan["demoted"]
    header = ("追加しました：\n" if future else "今日のタスクに追加しました：\n") + "\n".join(f"・{a}" for a in added)
    if future:
        header += "\n（実行日が今日ではないタスクは、その日の朝に案内します）"
    if demoted:
        header += f"\n（{FULL_NOTE}）"
    return header


async def handle(message) -> None:
    text = message.content.strip()
    if not text:
        return
    channel = message.channel
    if await week_plan.handle(message, text, _items):  # 週の計画・移す・今日は休む・着手・最初の5分
        return
    if _SHOW_ALL.match(text):
        await post_all(channel)
        return
    if _MORE.match(text):
        await post_more(channel)
        return
    if _FUTURE.match(text):
        await post_future(channel)
        return
    if _SHOW.match(text):
        await post_list(channel)
        return
    m = _DONE.match(text)
    if m:
        nums = util.parse_numbers(m.group(1))
        if not nums:
            await channel.send("番号で教えてください（例: 完了 1,3）。")
            return
        items = await _items(channel)
        ids = [items[n - 1]["id"] for n in nums if 1 <= n <= len(items)]
        done = await asyncio.to_thread(sheets.complete_tasks, ids) if ids else []
        if not done:
            await channel.send("その番号のタスクは見当たりませんでした。もう一度どうぞ。")
            return
        await asyncio.to_thread(task_sync.run_safely)
        touched = []
        if works.enabled():  # プロジェクトのタスクなら、作品管理シートの最終更新日を書く（失敗しても止めない）
            touched = await asyncio.to_thread(lambda: works.touch_tasks(sheets.find_tasks(ids)))
        await util.ack(message, "✅")
        header = "✅ 完了にしました！\n" + "\n".join(f"・{c}" for c in done) + "\nおつかれさまです。"
        if touched:
            hint = next_hint(touched, util.activity_day())
            if hint:
                header += "\n" + hint
            await board.refresh()
        so_far = done_list.format_items(done_list.SO_FAR_TITLE,  # 今日完了したものを毎回全部見せる（件数は出さない）
                                        [t["content"] for t in await done_list.completed_on(util.activity_day())])
        await post_list(channel, header + (f"\n\n{so_far}" if so_far else ""))
        return
    # 1行ずつ: 番号でメイン/サブを変える命令か、タスク（1行1タスク。「サブ：」「（サブ）」などで区分を指定できる）
    cmds: list[tuple[str, list[int]]] = []
    entries: list[tuple[str | None, str]] = []
    unclear = False
    header_kind = None
    for line in _lines(text):
        c = parse_kind_commands(line)
        if c is not None:
            cmds += c
            continue
        h = _KIND_HEADER.match(line)
        if h:
            header_kind = h.group(1)
            continue
        if looks_like_command(line):
            unclear = True
            continue
        kind, body = line_kind(line)
        entries.append((kind or header_kind, body))
    if not cmds and not entries:
        if unclear or header_kind:
            await channel.send(CMD_HELP)
        return

    parts, changed = [], False
    if cmds:  # 番号は、タスクを追加する前の一覧のもの。先に反映する
        reply, changed = await _apply_kinds(channel, cmds)
        if not entries and not changed and not reply.startswith("すでに"):
            await channel.send(reply)  # 見当たらない・3件を超える: 何も変えていないので、一覧は出し直さない
            return
        parts.append(reply)
    if entries:
        parts.append(await _add_tasks(entries))
    if unclear:
        parts.append(f"（{CMD_HELP}）")
    if changed or entries:
        await asyncio.to_thread(task_sync.run_safely)
    if changed:
        await util.ack(message, "🔀")
    if entries:
        await util.ack(message, "📝")
    await post_list(channel, "\n\n".join(parts))

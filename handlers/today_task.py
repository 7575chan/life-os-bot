"""`01-today-task`: 朝の案内、今日のタスクの表示、緊急タスクの追加、完了の記録、メインとサブの入れ替え（SPEC §5）。"""
import asyncio
import re
import unicodedata
from datetime import date, timedelta

import done_list
import sheets
import state
import task_dates
import task_sync
import util

MAIN_MAX = 3
MAIN = "高"  # メイン = 優先度「高」（Obsidian の ⏫）。サブ = それ以外（SPEC §5 01-today-task）
_DONE = re.compile(r"^\s*(?:完了|done|かんりょう)[\s:：]*(.+)$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*・•]|\[[ xX]?\])\s*")
_SHOW = re.compile(r"^\s*(?:今日のタスク|タスク|一覧|リスト)\s*(?:を見せて|を表示)?\s*[?？]*\s*$")
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


def compose_morning(tasks: list[dict], health_line: str | None, overnight: list[str] | None = None) -> str:
    """朝の案内の本文。昨日の執筆実績は、当日の記録（09:44）が入ったあとの別の投稿（scheduler.post_writing）で出す。

    overnight: 前日の夜の問いかけのあと（〜今朝）に完了したタスク。夜の問いかけに載らなかった分をここで出す。
    """
    parts = [MORNING_PREFIX + " ☀️"]
    if health_line:
        parts.append(health_line)
    done = done_list.format_items(done_list.OVERNIGHT_TITLE, overnight or [])
    if done:
        parts.append(done)
    if tasks:
        parts.append(format_task_list(tasks))
        parts.append(f"（※未完了のタスクは、3日たつとバックログへ静かに移ります。{HINT}）")
    else:
        parts.append("今日のタスクはまだありません。ここに書き込むと、今日のタスクに追加されます。夜の振り返りで、明日やるものを選ぶこともできます🌙")
    return "\n\n".join(parts)


def health_line(score: int | None, mood: str | None) -> str | None:
    bits = []
    if score:
        bits.append(f"体調スコア {score}")
    if mood:
        bits.append(f"ご機嫌度 {mood}")
    return ("🩺 昨日: " + " / ".join(bits)) if bits else None


async def build_morning(day: date) -> tuple[str, list[dict]]:
    """朝の案内の本文と、番号に対応するタスク一覧（番号の順）。日付が変わった直後の整理（3日たった未完了の退避）を先に行う。"""
    await asyncio.to_thread(sheets.cleanup_stale, day)
    await asyncio.to_thread(task_sync.run_safely)
    tasks = ordered(await asyncio.to_thread(sheets.today_tasks, day))
    y = day - timedelta(days=1)
    score = await asyncio.to_thread(sheets.health_score_on, y)
    diary = await asyncio.to_thread(sheets.diary_on, y)
    line = health_line(score, (diary or {}).get("ご機嫌度") or None)
    return compose_morning(tasks, line, await done_list.overnight_done(day)), tasks


async def post_list(channel, header: str | None = None) -> None:
    """現在の今日のタスクを投稿し、番号に対応する状態を保存する。"""
    tasks = ordered(await asyncio.to_thread(sheets.today_tasks, util.today()))
    body = (format_task_list(tasks) + f"\n\n（{HINT}）") if tasks else "今日のタスクは、今のところありません。"
    msg = await util.send_long(channel, (header + "\n\n" if header else "") + body)
    state.put_pending(msg.id, channel.id, "today_list", {"items": [{"id": t["id"], "content": t["content"]} for t in tasks]})


async def _items(channel) -> list[dict]:
    """番号に対応するタスク。チャンネル内の最新の一覧（36時間以内）、無ければ今の今日のタスク。"""
    pending = state.latest_pending(channel.id, "today_list", max_age_hours=36)
    if pending:
        return pending["payload"]["items"]
    return [{"id": t["id"], "content": t["content"]} for t in ordered(await asyncio.to_thread(sheets.today_tasks, util.today()))]


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
    current = {t["id"]: t for t in await asyncio.to_thread(sheets.today_tasks, util.today())}
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
    today = util.today()
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
        await util.ack(message, "✅")
        header = "✅ 完了にしました！\n" + "\n".join(f"・{c}" for c in done) + "\nおつかれさまです。"
        so_far = done_list.format_items(done_list.SO_FAR_TITLE,  # 今日完了したものを毎回全部見せる（件数は出さない）
                                        [t["content"] for t in await done_list.completed_on(util.today())])
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

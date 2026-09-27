"""`01-today-task`: 朝の案内、今日のタスクの表示、緊急タスクの追加、完了の記録、メインとサブの入れ替え（SPEC §5）。"""
import asyncio
import re
from datetime import date, timedelta

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
SOURCE = "01-today-task"
MORNING_PREFIX = "おはようございます！ 本日のタスク案内"
HINT = "「完了 1,3」で完了、「メイン 4」「サブ 1」でメインとサブを入れ替えられます"
FULL_NOTE = "メインが3件そろっているので、サブに入れました（「メイン 番号」で入れ替えられます）"


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


def compose_morning(tasks: list[dict], health_line: str | None) -> str:
    """朝の案内の本文。昨日の執筆実績は、当日の記録（09:44）が入ったあとの別の投稿（scheduler.post_writing）で出す。"""
    parts = [MORNING_PREFIX + " ☀️"]
    if health_line:
        parts.append(health_line)
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
    return compose_morning(tasks, line), tasks


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


async def _set_kind(channel, message, kind: str, nums: list[int]) -> None:
    """「メイン 4」「サブ 1」: 優先度を「高」/空にする。メインが3件を超えるなら何も変えない。"""
    items = await _items(channel)
    ids = list(dict.fromkeys(items[n - 1]["id"] for n in nums if 1 <= n <= len(items)))
    current = {t["id"]: t for t in await asyncio.to_thread(sheets.today_tasks, util.today())}
    ids = [i for i in ids if i in current]
    if not ids:
        await channel.send("その番号のタスクは見当たりませんでした。もう一度どうぞ。")
        return
    if kind == "メイン":
        mains = {i for i, t in current.items() if t["priority"] == MAIN} | set(ids)
        if len(mains) > MAIN_MAX:
            await channel.send("メインは3件までです。先に「サブ 1」のように、どれかをサブにしてください。")
            return
        changed = await asyncio.to_thread(sheets.set_priority, ids, MAIN)
    else:  # すでにサブ（中・低・空）のものは、そのまま
        ids = [i for i in ids if current[i]["priority"] == MAIN]
        changed = await asyncio.to_thread(sheets.set_priority, ids, "") if ids else []
    if changed:
        await asyncio.to_thread(task_sync.run_safely)
        header = f"{kind}にしました：\n" + "\n".join(f"・{c}" for c in changed)
    else:
        header = f"すでに{kind}になっています。"
    await util.ack(message, "🔀")
    await post_list(channel, header)


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
        await post_list(channel, "✅ 完了にしました！\n" + "\n".join(f"・{c}" for c in done) + "\nおつかれさまです。")
        return
    m = _KIND.match(text)
    if m:
        nums = util.parse_numbers(m.group(2))
        if nums:
            await _set_kind(channel, message, m.group(1), nums)
            return

    # それ以外の文は「今日絶対やる緊急タスク」。1行1タスク。先頭の「メイン：」「サブ：」で区分を指定できる。
    today = util.today()
    lines = _lines(text)
    if not lines:
        return
    current = await asyncio.to_thread(sheets.today_tasks, today)
    main_free = MAIN_MAX - sum(1 for t in current if t["priority"] == MAIN)
    added, future, demoted = [], False, False
    for line in lines:
        km = _KIND.match(line)
        kind, body = (km.group(1), km.group(2).strip()) if km else (None, line)
        plan = plan_task(body, today, main_free, kind)
        await asyncio.to_thread(sheets.add_task, plan["content"], scheduled=plan["scheduled"], due=plan["due"],
                                priority=plan["priority"], source=SOURCE)
        if plan["priority"] == MAIN and not plan["future"]:
            main_free -= 1
        added.append(plan["label"])
        future = future or plan["future"]
        demoted = demoted or plan["demoted"]
    await asyncio.to_thread(task_sync.run_safely)
    await util.ack(message, "📝")
    header = ("追加しました：\n" if future else "今日のタスクに追加しました：\n") + "\n".join(f"・{a}" for a in added)
    if future:
        header += "\n（実行日が今日ではないタスクは、その日の朝に案内します）"
    if demoted:
        header += f"\n（{FULL_NOTE}）"
    await post_list(channel, header)

"""`10-project-novel` / `11-project-trpg` / `12-project-others`: 作品ごとのノートと相談（SPEC §5）。

- 作品ごとに `Novel_<作品名>.md`（TRPG は `TRPG_…`、その他は `Project_…`）を、その部屋のフォルダに作って追記する
- 部屋ごとの別シート（`10-project-novel` など）にも記録する（日時 / 作品名 / 種別 / 内容）
- 投稿を AI が分類する（進捗・次やる・後回し・アイデア・詰まり・設定・検索・相談・その他）。
  「次やる」は優先「高」のタスク、「後回し」はバックログのタスクとして連携する
- 作品名が分からないときは直前に触った作品、それも無ければ `未命名_YYYYMMDD`。
  「タイトルは『〇〇』に決まった」と書くと、ファイル名とノート内の見出しを改名する
- **相談・検索のときだけ返信する**。それ以外は📝のリアクションだけ（📋=タスクにも連携、⚠️=連携に失敗、🏷️=改名、💬=相談への返信）
- 相談では、その作品のノート、過去のノート・スクラップの検索結果、執筆記録シートの目標・締切・実績を材料にする
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date, datetime

import claude_client
import notes
import sheets
import state
import task_dates
import task_sync
import util
import vault_paths
import writing_log
from handlers import idea

log = logging.getLogger("life-os.project")

ROOMS = {
    "novel": {"prefix": "Novel", "label": "小説", "desc": "小説の執筆（プロット・キャラクター・世界観設定・進捗）"},
    "trpg": {"prefix": "TRPG", "label": "TRPG", "desc": "TRPG のシナリオ作成（ギミック・NPC・進行メモ）"},
    "others": {"prefix": "Project", "label": "その他の制作・開発", "desc": "小説・TRPG 以外の制作・開発プロジェクト"},
}
KINDS = ["進捗", "次やる", "後回し", "アイデア", "詰まり", "設定", "検索", "相談", "その他"]
CONSULT_KINDS = {"検索", "相談"}
UNTITLED = "未命名"
MAX_NAME = 40
NOTE_CONTEXT_LIMIT = 15_000
_locks: dict[str, asyncio.Lock] = {}

_CLASSIFY = """あなたは{label}のプロジェクトの記録係です。次の投稿を分類し、JSONだけを返してください。
この部屋の対象: {desc}
既存の作品: {works}
直前に触った作品: {last}
{{
  "work": 投稿がどの作品についてか。既存の作品のどれかに当てはまるなら、その名前をそのまま。新しい作品を始める投稿なら、その新しい作品名。はっきり分からなければ null,
  "is_new_work": 新しい作品名を返したときだけ true,
  "kind": 次のどれか1つ: {kinds}。「どう思う？」「相談したい」「日程・スケジュールを分けたい」は 相談。「前に言っていた〇〇は？」「〇〇の設定って何だっけ」は 検索。「次は〜」「まず〜を書く」は 次やる。「〜は後回し」「あとで〜」は 後回し,
  "title_decided": 「タイトルは『〇〇』に決まった」「〇〇という題名にする」のように、作品のタイトルが決まったと書かれているときだけ、そのタイトル。無ければ null,
  "task": kind が 次やる/後回し のとき、やることを短い一文で（「明日」「9/25まで」などの日付の言葉は、投稿にあるとおり残す）。無ければ null,
  "search_query": kind が 検索 のとき、探す言葉を1〜3個（空白区切り）。無ければ null
}}

投稿:
{text}"""

_CONSULT = """{label}のプロジェクトについて、ユーザーから{kind}が来ています。答えてください。

【ユーザーの投稿】
{text}

【対象の作品】{work}（ほかの作品: {others}）

【この作品のノート（新しい部分）】
{note}

{progress}【過去のノート・スクラップの検索結果】
{snippets}"""

_CONSULT_SYSTEM = """今回は、ユーザーが相談・検索を頼んでいるので、具体的で実行しやすい提案や答えを返してよい（ふだんの「助言しない」ルールは、この相談への返信では緩める）。
ただし、評価・説教・べき論は書かない。ノートや検索結果に書かれていないことは、推測せず「ノートには見当たりませんでした」と正直に言う。
スケジュールの相談では、執筆記録シートの数字（総文字数・目標・締切・ペース）を根拠にし、無理のない分け方を、日数と文字数の目安つきで示す。返信は簡潔に。"""


# ---------------------------------------------------------------- 純粋関数


def clean_name(name) -> str:
    """作品名として使える形にする（かぎ括弧・空白・ファイル名に使えない文字を除く）。"""
    if not isinstance(name, str):
        return ""
    n = unicodedata.normalize("NFC", name).strip().strip("『』「」\"' 　")
    n = re.sub(r'[\\/:*?"<>|\r\n\t]', "", n).strip()
    return n[:MAX_NAME]


def norm_key(name: str) -> str:
    return re.sub(r"[\s　]", "", unicodedata.normalize("NFKC", name)).lower()


def list_works(prefix: str, paths: list[str], room_dir: str) -> list[str]:
    """部屋フォルダ直下の `<prefix>_<作品名>.md` から、作品名を取り出す。"""
    out = []
    for p in paths:
        head, _, fname = p.rpartition("/")
        m = re.match(rf"^{re.escape(prefix)}_(.+)\.md$", fname)
        if head == room_dir and m:
            out.append(m.group(1))
    return sorted(out)


def resolve_work(ai_work, is_new: bool, existing: list[str], last: str | None, today: date) -> str:
    """投稿がどの作品についてかを決める。分からなければ直前の作品、それも無ければ `未命名_YYYYMMDD`。"""
    name = clean_name(ai_work)
    by_key = {norm_key(e): e for e in existing}
    if name and norm_key(name) in by_key:
        return by_key[norm_key(name)]
    if name and is_new:
        return name
    if last and last in existing:
        return last
    if len(existing) == 1:
        return existing[0]
    if name:
        return name
    return f"{UNTITLED}_{today:%Y%m%d}"


def normalize_kind(kind, title_decided) -> str:
    if title_decided:
        return "タイトル決定"
    return kind if kind in KINDS else "その他"


def format_entry(now: datetime, kind: str, text: str, files: list[dict]) -> str:
    lines = [f"## {now:%Y-%m-%d %H:%M}｜{kind}"]
    if text:
        lines.append(idea.escape_body(text))
    for f in files:
        if f["saved"] and f["image"]:
            lines.append(f"![[{f['name']}]]")
        elif f["saved"]:
            lines.append(f"📎 {f['name']}")
        else:
            lines.append(f"📎 {f['name']}（保存していません: {f['reason']}）")
    return "\n".join(lines)


def task_text(work: str, task: str) -> str:
    return f"{work}｜{task}"


def tail(text: str, limit: int = NOTE_CONTEXT_LIMIT) -> str:
    return text if len(text) <= limit else "…（前半は省略）\n" + text[-limit:]


def format_snippets(hits: list[dict], limit: int = 6) -> str:
    if not hits:
        return "（見つかりませんでした）"
    return "\n".join(f"- {h['path'].rsplit('/', 1)[-1]}: {h['snippet']}" for h in hits[:limit])


def consult_prompt(*, label: str, kind: str, text: str, work: str, others: list[str], note: str | None,
                   progress: str | None, snippets: list[dict]) -> str:
    return _CONSULT.format(label=label, kind=kind, text=text, work=work, others="、".join(others) or "なし",
                           note=tail(note) if note else "（この作品のノートはまだありません）",
                           progress=(f"【執筆記録シートの数字】\n{progress}\n\n" if progress else ""),
                           snippets=format_snippets(snippets))


# ---------------------------------------------------------------- 処理


async def _classify(cfg: dict, text: str, works: list[str], last: str | None) -> dict:
    fallback = {"work": None, "is_new_work": False, "kind": "その他", "title_decided": None, "task": None, "search_query": None}
    if not text:
        return fallback
    got = await claude_client.complete_json(_CLASSIFY.format(
        label=cfg["label"], desc=cfg["desc"], works="、".join(works) or "なし", last=last or "なし",
        kinds=" / ".join(KINDS), text=text), fallback)
    return {**fallback, **got} if isinstance(got, dict) else fallback


async def _rename(store, room_key: str, cfg: dict, old: str, new: str, works: list[str]) -> str | None:
    """作品の改名。ファイル名とノート内の見出しを変える。できないときは理由（文字列）を返す。成功なら None。"""
    if norm_key(new) == norm_key(old):
        return None
    if any(norm_key(new) == norm_key(w) for w in works):
        return f"『{new}』という作品がすでにあります"
    old_path, new_path = work_file(room_key, cfg, old), work_file(room_key, cfg, new)
    await asyncio.to_thread(store.rename, old_path, new_path)
    text = await asyncio.to_thread(store.read, new_path)
    if text is not None:
        await asyncio.to_thread(store.write, new_path, notes.replace_title(text, new))
    return None


def work_file(room_key: str, cfg: dict, work: str) -> str:
    return vault_paths.project(room_key, f"{cfg['prefix']}_{work}.md")


async def _progress_for(work: str) -> str | None:
    try:
        log_ = await asyncio.to_thread(writing_log.fetch)
        p = writing_log.progress_summary(log_, work, util.today()) if log_ else None
        return writing_log.format_progress(p) if p else None
    except Exception:  # noqa: BLE001  執筆記録が読めなくても、相談には答える
        log.warning("執筆記録シートを読めませんでした", exc_info=True)
        return None


async def _consult(message, room_key: str, cfg: dict, kind: str, text: str, work: str, works: list[str], info: dict) -> None:
    store = notes.get_store()
    note = await asyncio.to_thread(store.read, work_file(room_key, cfg, work)) if work in works else None
    query = str(info.get("search_query") or "").strip()
    hits: list[dict] = []
    if query:
        try:
            room_dir, scrap_dir = vault_paths.room(room_key), vault_paths.room("scrap")
            found = await asyncio.to_thread(store.search, query, 30)
            mine = [h for h in found if h["path"].startswith(room_dir + "/") or h["path"].startswith(scrap_dir + "/")]
            hits = mine or found
        except Exception:  # noqa: BLE001
            log.warning("ノートの検索に失敗しました", exc_info=True)
    progress = await _progress_for(work) if work in works or kind == "相談" else None
    prompt = consult_prompt(label=cfg["label"], kind=kind, text=text, work=work, others=[w for w in works if w != work],
                            note=note, progress=progress, snippets=hits)
    answer = await claude_client.complete(prompt, extra_system=_CONSULT_SYSTEM, max_tokens=1500)
    await util.ack(message, "💬")
    await util.send_long(message.channel, answer)


async def handle_room(room_key: str, message) -> None:
    cfg = ROOMS[room_key]
    text = (message.content or "").strip()
    if not text and not message.attachments:
        return
    store = notes.get_store()
    now = util.now()
    room_dir = vault_paths.room(room_key)
    paths = await asyncio.to_thread(store.list, room_dir)
    works = list_works(cfg["prefix"], paths, room_dir)
    last_key = f"project_last:{room_key}"
    last = state.get_kv(last_key)
    info = await _classify(cfg, text, works, last)

    title_decided = clean_name(info.get("title_decided"))
    kind = normalize_kind(info.get("kind"), title_decided)
    work = resolve_work(info.get("work"), bool(info.get("is_new_work")), works, last, now.date())
    lock = _locks.setdefault(room_key, asyncio.Lock())

    renamed = None
    if title_decided:
        async with lock:
            if work in works and norm_key(title_decided) != norm_key(work):
                problem = await _rename(store, room_key, cfg, work, title_decided, works)
                if problem:
                    await message.channel.send(f"改名できませんでした: {problem}。")
                    return
                renamed = (work, title_decided)
            work = title_decided  # 改名した作品、または、タイトルが決まって新しく始まる作品
    if renamed:
        state.set_kv(last_key, work)
        works = [work if w == renamed[0] else w for w in works]
        await util.ack(message, "🏷️")
        await message.channel.send(f"🏷️ 『{renamed[0]}』を『{work}』に改名しました。")

    if kind in CONSULT_KINDS:
        await _consult(message, room_key, cfg, kind, text, work, works, info)
        record_note = False
    else:
        record_note = True

    files = await idea._save_attachments(message, now, path_fn=lambda n: vault_paths.project_attachment(room_key, n))
    if record_note:
        entry = format_entry(now, kind, text, files)
        async with lock:  # Obsidian を先に書く（失敗したらシートにもタスクにも記録しない）
            await asyncio.to_thread(store.append_entry, work_file(room_key, cfg, work), entry, work)
        state.set_kv(last_key, work)
        await util.ack(message, "📝")

    warn = False
    try:
        await asyncio.to_thread(sheets.append, sheets.PROJECT_SHEETS[room_key], {
            "日時": util.fmt_datetime(now), "作品名": work, "種別": kind, "内容": idea.sheet_content(text, files)})
    except Exception:  # noqa: BLE001
        warn = True
        log.warning("プロジェクトの記録をシートに書けませんでした（Obsidian には保存済み）", exc_info=True)
    task = str(info.get("task") or "").strip()
    if task and kind in ("次やる", "後回し") and record_note:
        try:
            ext = task_dates.extract(task, now.date())
            await asyncio.to_thread(sheets.add_task, task_text(work, ext.content), scheduled=task_dates.iso(ext.scheduled),
                                    due=task_dates.iso(ext.due), priority="高" if kind == "次やる" else "",
                                    source=vault_paths.room(room_key).rsplit("/", 1)[-1])
            await asyncio.to_thread(task_sync.run_safely)
            await util.ack(message, "📋")
        except Exception:  # noqa: BLE001
            warn = True
            log.warning("タスクへの連携に失敗しました", exc_info=True)
    if warn:
        await util.ack(message, "⚠️")


def make_handler(room_key: str):
    async def handler(message) -> None:
        await handle_room(room_key, message)

    handler.__name__ = f"handle_{room_key}"
    return handler

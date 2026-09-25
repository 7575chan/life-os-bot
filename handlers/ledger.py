"""`07-ledger`: 事業の帳簿（SPEC §5）。

- 短文メモ（`10/5 サーバー代 1,200円` / `10/10 BOOTH売上 50,000円`）か、レシート・領収書の写真（Vision）を、
  `07-ledger` シートに1行ずつ記録する（日付 / 種別（売上・経費） / 金額（円） / 内容 / やよい転記 / 分析シート転記）
- 決まった書き方は正規表現で読み取る。売上か経費かは、書かれた言葉（売上・入金 / 経費・〜代 など）で決め、
  **どちらか分からないものは記録せず、聞き返す**（損益の符号を間違えないため）。読み取れない行だけ AI に構造化させる
- レシート画像は `06-Life-OS/07-ledger/attachments/` に保存し、シートの内容に `[レシート: 名前]` を残す。
  金額・日付・店名を Vision で読み取る（レシートは経費）
- 転記チェック2列: 「10月分転記した」と書くと、その月の未転記の行を転記済み（TRUE）にする。
  「やよい」「分析」の語で片方だけ、無ければ両方。月が書かれていなければ、何月分かを聞き返す。シート上で直接チェックしてもよい
- 返信には、今月の概算損益（売上 − 経費）と未転記件数を必ず添える。数字を淡々と示すだけで、評価・助言はしない
- リアクション: 📝 記録した（返信ではレシートからの行に 🧾） / ✅ 転記済みにした / 📷 画像を保存した / ⚠️ 読み取れない・記録や保存に失敗した
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

import claude_client
import sheets
import util
import vault_paths
from handlers import household as hh
from handlers import idea

log = logging.getLogger("life-os.ledger")

SALES, EXPENSE = "売上", "経費"
KINDS = (SALES, EXPENSE)
YAYOI, ANALYSIS = "やよい転記", "分析シート転記"
MAX_LINES = 20
MAX_RECEIPTS = 5  # 1投稿で読み取るレシート画像の枚数
MAX_IMAGE_BYTES = idea.MAX_IMAGE_BYTES
HINT = ("「10/5 サーバー代 1,200円」「10/10 BOOTH売上 50,000円」のように、売上か経費かが分かる言葉と金額を書いてください"
        "（「売上」「経費」と書き添えても大丈夫です）。レシートは写真のまま送れます。")
ASK_MONTH = "何月分を転記済みにするか、教えてください（例: 「10月分転記した」「やよいは今月分転記した」）。"

SALES_WORDS = ("売上", "売り上げ", "入金", "収入", "報酬", "印税", "収益")
EXPENSE_WORDS = ("経費", "代", "費", "購入", "支払", "仕入", "会費", "手数料", "料金", "送料", "利用料", "買")

_KIND_HEAD = re.compile(r"^(売上|経費)[\s、,:：]*")
_DONE = re.compile(r"(した|しました|済|完了|できた|終わ|おわ|終了)")
_MONTH = re.compile(r"(\d{1,2})月分?")
_YEN_WORD = re.compile(r"\d\s*円")

_LINE_PROMPT = """次は、事業の帳簿の部屋に投稿された、決まった書き方ではない行です（番号つき）。記録できるものだけを JSONだけで返してください。
種別は「売上」（お金が入ったもの）か「経費」（払ったもの）のどちらかです。書かれた言葉から明らかなときだけ決め、どちらか、または金額が判断できない行は含めないでください。推測で種別や金額を作らないでください。
{{
  "entries": [
    {{"line": 元の行の番号, "kind": "売上" または "経費", "content": 内容を短く（例: サーバー代・BOOTH売上）,
     "amount": 金額（円・整数）, "date": "YYYY-MM-DD"（書かれているときだけ。無ければ null）}}
  ]
}}
今日は {today} です。

行:
{lines}"""

_VISION_PROMPT = """添付は、事業の帳簿に記録するレシート・領収書などの画像です（{n}枚。添付の順に 1, 2, … と番号をつけます）。
読み取れた1件につき1つ、JSONだけで返してください。
{{
  "receipts": [
    {{"image": 画像の番号, "date": "YYYY-MM-DD"（印字された日付。読み取れなければ null）, "store": 店名・発行元（無ければ空文字）,
     "amount": 合計金額（税込・円・整数）, "kind": レシート・領収書は "経費"。売上の報告画面など、明らかに売上なら "売上"。判断できなければ null,
     "note": 買ったものの短い説明（無ければ空文字）}}
  ]
}}
金額を読み取れない画像は含めないでください。推測で金額や日付を作らないでください。
{hint}今日は {today} です。"""


@dataclass
class Entry:
    day: date
    kind: str
    content: str
    amount: int
    line: str = ""
    image: int = 0  # レシートのとき、Vision に渡した画像の番号（1始まり）。0 は文字での記録
    attachment: str = ""  # 保存したレシート画像の名前


@dataclass
class Transcription:
    month: date | None  # 月初。None は、月が書かれていない
    cols: tuple[str, ...]


# ---------------------------------------------------------------- 純粋関数: 読み取り


def detect_kind(text: str) -> str | None:
    """内容の言葉から種別を決める。売上の言葉と経費の言葉の両方がある・どちらも無いときは None（決めない）。"""
    sales = any(w in text for w in SALES_WORDS)
    expense = any(w in text for w in EXPENSE_WORDS)
    if sales == expense:
        return None
    return SALES if sales else EXPENSE


def _clean_content(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" 　、,:：")


def parse_line(line: str, today: date) -> Entry | None:
    """`[日付] [売上|経費] 内容 金額円` の1行を読み取る。読み取れない・種別が決められないときは None。"""
    dated = hh.take_date(hh.norm_text(line), today)
    if dated is None:
        return None
    day, text = dated
    head = _KIND_HEAD.match(text)
    if head:
        text = text[head.end():]
    got = hh.take_amount(text)
    if got is None:
        return None
    amount, remainder = got
    content = _clean_content(remainder)
    kind = head.group(1) if head else detect_kind(content)
    if kind is None:
        return None
    return Entry(day, kind, content, amount, line.strip())


def parse_transcription(line: str, today: date) -> Transcription | None:
    """「10月分転記した」「やよいは今月分転記した」のような報告。金額の書かれた行は、記録の行なので対象外。"""
    t = hh.norm_text(line)
    if "転記" not in t or not _DONE.search(t) or _YEN_WORD.search(t):
        return None
    cols = tuple(c for w, c in (("やよい", YAYOI), ("分析", ANALYSIS)) if w in t) or (YAYOI, ANALYSIS)
    month = None
    m = _MONTH.search(t)
    if m and 1 <= int(m.group(1)) <= 12:
        y = today.year if int(m.group(1)) <= today.month else today.year - 1  # 未来の月は、去年の月とみなす
        month = date(y, int(m.group(1)), 1)
    elif "先月" in t:
        month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    elif "今月" in t:
        month = today.replace(day=1)
    return Transcription(month, cols)


def _valid_amount(amount) -> bool:
    return (not isinstance(amount, bool) and isinstance(amount, (int, float)) and 0 < amount < hh.MAX_AMOUNT
            and amount == int(amount))


def clean_ai_entries(raw, today: date, lines: list[str]) -> tuple[list[Entry], set[int]]:
    """AI の返した entries を検証する。(記録する行, 読み取れた元の行の番号(0始まり))"""
    out: list[Entry] = []
    matched: set[int] = set()
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or e.get("kind") not in KINDS or not _valid_amount(e.get("amount")):
            continue
        idx = e.get("line")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 1 <= idx <= len(lines):
            continue
        day = util.parse_date_any(str(e.get("date") or "")) or today
        out.append(Entry(day, e["kind"], _clean_content(str(e.get("content") or ""))[:60], int(e["amount"]), lines[idx - 1]))
        matched.add(idx - 1)
    return out, matched


def clean_receipts(raw, today: date, n_images: int) -> list[Entry]:
    """Vision の返した receipts を検証する。種別が経費・売上でない・金額が不正・画像の番号が範囲外のものは捨てる。"""
    out: list[Entry] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or r.get("kind") not in KINDS or not _valid_amount(r.get("amount")):
            continue
        idx = r.get("image")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 1 <= idx <= n_images:
            continue
        day = util.parse_date_any(str(r.get("date") or "")) or today
        content = _clean_content(f"{r.get('store') or ''} {r.get('note') or ''}")[:60] or "レシート"
        out.append(Entry(day, r["kind"], content, int(r["amount"]), image=idx))
    return out


def drop_duplicates(receipts: list[Entry], typed: list[Entry]) -> tuple[list[Entry], list[Entry]]:
    """同じ投稿の文字での記録と、日付・種別・金額が同じレシートは、二重にならないよう記録しない。(残す, 除いた)"""
    keys = {(e.day, e.kind, e.amount) for e in typed}
    keep = [r for r in receipts if (r.day, r.kind, r.amount) not in keys]
    return keep, [r for r in receipts if (r.day, r.kind, r.amount) in keys]


# ---------------------------------------------------------------- 純粋関数: 集計と返信


def _amount(rec: dict) -> int | None:
    return util.to_int(str(rec.get("金額（円）", "")).replace("¥", "").replace("￥", ""))


def month_summary(records: list[tuple[int, dict]], month_start: date) -> tuple[int, int]:
    """`month_start` の月の (売上の合計, 経費の合計)。読み取れない金額・日付の行は数えない。"""
    nxt = (month_start + timedelta(days=32)).replace(day=1)
    sales = expense = 0
    for _, rec in records:
        d, amount, kind = util.parse_date_any(rec.get("日付", "")), _amount(rec), str(rec.get("種別", "")).strip()
        if d and amount is not None and month_start <= d < nxt:
            if kind == SALES:
                sales += amount
            elif kind == EXPENSE:
                expense += amount
    return sales, expense


def unposted_counts(records: list[tuple[int, dict]]) -> tuple[int, int]:
    """(やよい未転記, 分析シート未転記) の件数。売上・経費として読み取れる行だけ数える（全期間）。"""
    y = a = 0
    for _, rec in records:
        if str(rec.get("種別", "")).strip() in KINDS and _amount(rec) is not None:
            y += not util.is_done(rec.get(YAYOI, ""))
            a += not util.is_done(rec.get(ANALYSIS, ""))
    return y, a


def plan_transcription(records: list[tuple[int, dict]], cmd: Transcription) -> list[tuple[int, dict]]:
    """その月の、まだチェックされていない行を TRUE にする更新。[(行番号, {列名: "TRUE"})]"""
    if cmd.month is None:
        return []
    nxt = (cmd.month + timedelta(days=32)).replace(day=1)
    updates = []
    for row, rec in records:
        d = util.parse_date_any(rec.get("日付", ""))
        if not d or not cmd.month <= d < nxt or str(rec.get("種別", "")).strip() not in KINDS or _amount(rec) is None:
            continue
        todo = {c: "TRUE" for c in cmd.cols if not util.is_done(rec.get(c, ""))}
        if todo:
            updates.append((row, todo))
    return updates


def profit_text(sales: int, expense: int) -> str:
    diff = sales - expense
    return "±0円" if diff == 0 else f"{'+' if diff > 0 else '-'}{abs(diff):,}円"


def entry_text(e: Entry, today: date) -> str:
    when = "" if e.day == today else f"（{e.day.month}/{e.day.day}）"
    return f"{'🧾' if e.image else '📝'} {e.kind}｜{e.content or '（内容なし）'} {e.amount:,}円{when}"


def format_reply(entries: list[Entry], notes: list[str], stats: dict[date, tuple[int, int]] | None,
                 unposted: tuple[int, int] | None, today: date, unread: list[str]) -> str:
    """記録の確認 ＋ 概算損益 ＋ 未転記件数（数字だけ。評価はしない）＋ 読み取れなかったもの。"""
    lines = [entry_text(e, today) for e in entries] + notes
    if stats is not None:
        for m, (s, x) in sorted(stats.items(), reverse=True):
            lines.append(f"{hh.month_label(m, today)}の概算損益: {profit_text(s, x)}（売上 {s:,}円 − 経費 {x:,}円）")
    if unposted is not None:
        y, a = unposted
        lines.append(f"未転記: やよい {y}件 / 分析シート {a}件" + ("（暇なときに移せば大丈夫です）" if y or a else ""))
    elif entries:
        lines.append("（今月の概算損益と未転記件数は、今は出せませんでした）")
    if unread:
        lines.append("読み取れなかったもの（記録していません）: " + " / ".join(unread))
        lines.append(HINT)
    return "\n".join(lines)


# ---------------------------------------------------------------- 処理


async def _ai_entries(lines: list[str], today: date) -> tuple[list[Entry], set[int]]:
    numbered = "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))
    got = await claude_client.complete_json(_LINE_PROMPT.format(today=util.fmt_date(today), lines=numbered), {"entries": []})
    return clean_ai_entries(got.get("entries") if isinstance(got, dict) else None, today, lines)


async def _read_receipts(images: list[tuple[bytes, str]], hint_lines: list[str], today: date) -> list[Entry]:
    hint = ("投稿に添えられた文（種別・日付・内容の手がかり。画像より優先してよい）: " + " / ".join(hint_lines) + "\n") if hint_lines else ""
    got = await claude_client.vision_json(
        images, _VISION_PROMPT.format(n=len(images), hint=hint, today=util.fmt_date(today)), {"receipts": []})
    return clean_receipts(got.get("receipts") if isinstance(got, dict) else None, today, len(images))


async def _images_for_vision(message) -> list[tuple[int, bytes, str]]:
    """Vision に渡す画像。(添付の番号, 中身, 形式)。5MB を超える画像・画像以外は渡さない。最大 MAX_RECEIPTS 枚。"""
    out = []
    for i, a in enumerate(message.attachments):
        mime = (a.content_type or "").split(";")[0]
        if mime in util.IMAGE_TYPES and (a.size or 0) <= MAX_IMAGE_BYTES and len(out) < MAX_RECEIPTS:
            out.append((i, await a.read(), mime))
    return out


async def handle(message) -> None:
    text = (message.content or "").strip()
    if not text and not message.attachments:
        return
    now = util.now()
    today = now.date()
    warn = False

    # 1. 画像: 保存してから、Vision で読み取る
    files: list[dict] = []
    receipts: list[Entry] = []
    unread: list[str] = []
    vision = []
    if message.attachments:
        files = await idea._save_attachments(message, now, path_fn=vault_paths.ledger_attachment)
        if any(f["saved"] for f in files):
            await util.ack(message, "📷")
        bad = [f for f in files if not f["saved"]]
        if bad:
            warn = True
            await util.send_long(message.channel, "保存できなかった添付があります: "
                                 + "、".join(f"{f['name']}（{f['reason']}）" for f in bad))
        vision = await _images_for_vision(message)

    # 2. 本文を行ごとに: 転記の報告 / 決まった書き方の記録 / それ以外
    lines = [l.strip() for l in text.split("\n") if l.strip()][:MAX_LINES]
    cmds: list[Transcription] = []
    typed: list[Entry] = []
    other: list[str] = []
    for l in lines:
        cmd = parse_transcription(l, today)
        if cmd:
            cmds.append(cmd)
            continue
        e = parse_line(l, today)
        if e:
            typed.append(e)
        else:
            other.append(l)

    # 3. 読み取れなかった行は、画像があれば Vision への手がかり、無ければ AI で構造化
    notes: list[str] = []
    if vision:
        got = await _read_receipts([(b, m) for _, b, m in vision], other, today)
        got, dup = drop_duplicates(got, typed)
        for d in dup:
            notes.append(f"（レシートの {d.amount:,}円 は、文字での記録と同じなので追加していません）")
        for r in got:
            att_idx = vision[r.image - 1][0]
            r.attachment = files[att_idx]["name"] if files and files[att_idx]["saved"] else ""
        receipts = got
        read_images = {r.image for r in receipts} | {d.image for d in dup}
        unread += [f"レシート画像（{n}枚目）" for n in range(1, len(vision) + 1) if n not in read_images]
        if other and not read_images:
            unread += other  # 手がかりにしただけの文が、画像が読めなかったために宙に浮かないようにする
    elif other:
        ai, matched = await _ai_entries(other, today)
        typed += ai
        unread += [l for i, l in enumerate(other) if i not in matched]

    # 4. シートへ記録（1行ずつ。失敗したらそこで止める）
    entries = typed + receipts
    recorded: list[Entry] = []
    failed_rows = False
    for e in entries:
        content = e.content + (f" [レシート: {e.attachment}]" if e.attachment else "")
        try:
            await asyncio.to_thread(sheets.append, sheets.LEDGER, {
                "日付": util.fmt_date(e.day), "種別": e.kind, "金額（円）": e.amount, "内容": content})
            recorded.append(e)
        except Exception:  # noqa: BLE001  以降の行も書けない可能性が高いので、そこで止める
            log.warning("帳簿をシートに記録できませんでした", exc_info=True)
            failed_rows = True
            break
    if recorded:
        await util.ack(message, "📝")

    if not entries and not cmds and not unread:
        if warn:
            await util.ack(message, "⚠️")
        return

    # 5. 集計を読み、転記の報告を反映する（読み取りは1回だけ）
    recs = None
    if recorded or cmds:
        try:
            recs = await asyncio.to_thread(sheets.records, sheets.LEDGER)
        except Exception:  # noqa: BLE001  集計が出せなくても、記録の確認は返す
            log.warning("帳簿の集計を読めませんでした", exc_info=True)
    months = hh.month_starts(recorded, today)  # 今月 ＋ 記録した日付の月（Entry.day だけを使う）
    checked = False
    for cmd in cmds:
        if cmd.month is None:
            notes.append(ASK_MONTH)
            warn = True
            continue
        if cmd.month not in months:
            months.append(cmd.month)
        names = "・".join(c.replace("転記", "").replace("シート", "") for c in cmd.cols)
        label = f"{cmd.month.month}月分"
        if recs is None:
            notes.append(f"{label}の転記の報告は、シートを読めなかったため反映できませんでした。少し待ってからもう一度送ってください。")
            warn = True
            continue
        updates = plan_transcription(recs, cmd)
        if not updates:
            notes.append(f"{label}に、転記が済んでいない行（{names}）はありませんでした。")
            continue
        try:
            await asyncio.to_thread(sheets.update_rows, sheets.LEDGER, updates)
        except Exception:  # noqa: BLE001
            log.warning("転記チェックをシートに書けませんでした", exc_info=True)
            notes.append(f"{label}の転記チェックを書き込めませんでした。少し待ってからもう一度送ってください。")
            warn = True
            continue
        by_row = dict(recs)
        for row, changed in updates:
            by_row[row].update(changed)
        checked = True
        notes.append(f"✅ {label}の {names} を転記済みにしました（{len(updates)}件）")
    if checked:
        await util.ack(message, "✅")

    stats = unposted = None
    if recs is not None:
        stats = {m: month_summary(recs, m) for m in months}
        unposted = unposted_counts(recs)
    reply = format_reply(recorded, notes, stats, unposted, today, unread)
    if failed_rows:
        rest = [e.line or f"{e.kind} {e.content} {e.amount:,}円" for e in entries[len(recorded):]]
        reply += ("\n" if reply else "") + "記録できなかったものがあります（少し待ってから、もう一度送ってください）: " + " / ".join(rest)
    if unread or failed_rows or warn:
        await util.ack(message, "⚠️")
    await util.send_long(message.channel, reply)

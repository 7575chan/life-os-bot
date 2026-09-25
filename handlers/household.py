"""`06-household-accounts`: 私生活の家計簿（SPEC §5）。

- 手入力は「現金」と「娯楽」だけ。`現金 カフェ 800円` / `娯楽 ガチャ 3,000円` を、日付・区分・ジャンル・備考・金額に分けて
  `06-household-accounts` シートに1行ずつ記録する（1投稿に複数行を書いてもよい）
- 決まった書き方は正規表現で読み取る（AI を使わないので、金額を読み間違えない）。読み取れない行だけ AI に構造化させる
- 返信には、今月の娯楽費の累計を必ず添える。数字を淡々と示すだけで、評価・助言はしない
- 月末の楽天家計簿のスクショなどの画像は、`06-Life-OS/06-household-accounts/attachments/` に保存するだけ（金額は読み取らない）
- リアクション: 📝 記録した / 📷 画像を保存した / ⚠️ 読み取れない行がある・記録か保存に失敗した
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

import claude_client
import sheets
import util
import vault_paths
from handlers import idea

log = logging.getLogger("life-os.household")

KINDS = ("現金", "娯楽")
LEISURE = "娯楽"
MAX_AMOUNT = 100_000_000
MAX_LINES = 20  # 1投稿で記録する行数の上限
HINT = "「現金 カフェ 800円」「娯楽 ガチャ 3,000円」のように送ってください。"

_DATE_HEAD = re.compile(
    r"^(?:(?P<y>\d{4})\s*[-/年]\s*(?P<m1>\d{1,2})\s*[-/月]\s*(?P<d1>\d{1,2})\s*日?"
    r"|(?P<m2>\d{1,2})\s*[/月]\s*(?P<d2>\d{1,2})\s*日?"
    r"|(?P<w>一昨日|おととい|昨日|きのう|今日|きょう))[\s、,:：]*")
_REL = {"一昨日": 2, "おととい": 2, "昨日": 1, "きのう": 1, "今日": 0, "きょう": 0}
_KIND_HEAD = re.compile(r"^(現金|娯楽)[\s、,:：]*")
_YEN = re.compile(r"(\d[\d,]*)\s*円")
_TAIL_NUM = re.compile(r"(?:^|\s)(\d[\d,]*)\s*$")

_PROMPT = """次は、家計簿の部屋に投稿された、決まった書き方ではない行です（番号つき）。記録できるものだけを JSONだけで返してください。
記録するのは「現金」（現金での買い物）と「娯楽」（娯楽品・趣味の買い物）の2種類だけです。どちらか、または金額が判断できない行は、含めないでください。推測で金額や区分を作らないでください。
{{
  "entries": [
    {{"line": 元の行の番号, "kind": "現金" または "娯楽", "genre": 短いジャンル名（例: カフェ・ガチャ・漫画）, "note": 補足（無ければ空文字）,
     "amount": 金額（円・整数）, "date": "YYYY-MM-DD"（書かれているときだけ。無ければ null）}}
  ]
}}
今日は {today} です。

行:
{lines}"""


@dataclass
class Entry:
    day: date
    kind: str
    genre: str
    note: str
    amount: int
    line: str = ""


# ---------------------------------------------------------------- 純粋関数


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def _resolve_date(m: re.Match, today: date) -> date:
    """日付の言葉を date にする。年が無い `9/25` は今年。1か月以上先になるなら去年とみなす。無効な日付は ValueError。"""
    if m.group("w"):
        return today - timedelta(days=_REL[m.group("w")])
    if m.group("y"):
        return date(int(m.group("y")), int(m.group("m1")), int(m.group("d1")))
    d = date(today.year, int(m.group("m2")), int(m.group("d2")))
    return d.replace(year=today.year - 1) if d > today + timedelta(days=31) else d


def norm_text(text: str) -> str:
    """全角の数字・記号を半角にそろえる（`１，５００円` → `1,500円`）。"""
    return _norm(text)


def take_date(text: str, today: date) -> tuple[date, str] | None:
    """行頭の日付（`昨日` `9/20` `2026年9月1日` など）を取り出す。(日付, 残りの文)。日付が無ければ (今日, そのまま)。
    存在しない日付（2/30 など）は推測せず None。07-ledger と共通。"""
    try:
        m = _DATE_HEAD.match(text)
        return (_resolve_date(m, today), text[m.end():]) if m else (today, text)
    except ValueError:
        return None


def take_amount(rest: str) -> tuple[int, str] | None:
    """文の中の金額（`800円` / `3,000円`。「円」が無ければ、文末の数字）を取り出す。(金額, 金額を除いた文)。
    0 以下・大きすぎる金額、金額が無い文は None。07-ledger と共通。"""
    yen = list(_YEN.finditer(rest))
    hit = yen[-1] if yen else _TAIL_NUM.search(rest)
    if not hit:
        return None
    amount = int(hit.group(1).replace(",", ""))
    if not 0 < amount < MAX_AMOUNT:
        return None
    return amount, (rest[: hit.start()] + " " + rest[hit.end():]).strip()


def parse_line(line: str, today: date) -> Entry | None:
    """`[日付] 現金|娯楽 ジャンル [備考] 金額円` の1行を読み取る。読み取れなければ None（推測しない）。"""
    dated = take_date(norm_text(line), today)
    if dated is None:
        return None
    day, text = dated
    m = _KIND_HEAD.match(text)
    if not m:
        return None
    got = take_amount(text[m.end():])
    if got is None:
        return None
    amount, remainder = got
    words = [w for w in re.split(r"[\s、,]+", remainder) if w]
    return Entry(day, m.group(1), words[0] if words else "", " ".join(words[1:]), amount, line.strip())


def clean_ai_entries(raw, today: date, lines: list[str]) -> tuple[list[Entry], set[int]]:
    """AI の返した entries を検証する。(記録する行, 読み取れた元の行の番号(0始まり))

    区分が現金/娯楽でない・金額が正の整数でない・元の行の番号が範囲外のものは捨てる。"""
    out: list[Entry] = []
    matched: set[int] = set()
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or e.get("kind") not in KINDS:
            continue
        amount, idx = e.get("amount"), e.get("line")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount < MAX_AMOUNT or amount != int(amount):
            continue
        if isinstance(idx, bool) or not isinstance(idx, int) or not 1 <= idx <= len(lines):
            continue
        day = util.parse_date_any(str(e.get("date") or "")) or today
        out.append(Entry(day, e["kind"], str(e.get("genre") or "").strip()[:30], str(e.get("note") or "").strip()[:100],
                         int(amount), lines[idx - 1]))
        matched.add(idx - 1)
    return out, matched


def month_total(records: list[tuple[int, dict]], month_start: date, kind: str = LEISURE) -> int:
    """`month_start` の月の、指定した区分（既定: 娯楽）の合計。読み取れない金額・日付の行は数えない。"""
    nxt = (month_start + timedelta(days=32)).replace(day=1)
    total = 0
    for _, rec in records:
        d = util.parse_date_any(rec.get("日付", ""))
        amount = util.to_int(str(rec.get("金額（円）", "")).replace("¥", "").replace("￥", ""))
        if d and amount is not None and month_start <= d < nxt and str(rec.get("区分", "")).strip() == kind:
            total += amount
    return total


def entry_text(e: Entry, today: date) -> str:
    label = "｜".join(x for x in (e.kind, " ".join(x for x in (e.genre, e.note) if x)) if x)
    when = "" if e.day == today else f"（{e.day.month}/{e.day.day}）"
    return f"📝 {label} {e.amount:,}円{when}"


def month_label(month_start: date, today: date) -> str:
    return "今月" if (month_start.year, month_start.month) == (today.year, today.month) else f"{month_start.month}月"


def format_reply(entries: list[Entry], totals: dict[date, int] | None, today: date, unread: list[str]) -> str:
    """記録の確認 ＋ 娯楽費の累計（数字だけ。評価はしない）＋ 読み取れなかった行。"""
    lines = [entry_text(e, today) for e in entries]
    if totals is not None:
        lines += [f"{month_label(m, today)}の娯楽費: {t:,}円" for m, t in sorted(totals.items(), reverse=True)]
    elif entries:
        lines.append("（娯楽費の累計は、今は出せませんでした）")
    if unread:
        lines.append("読み取れなかった行（記録していません）: " + " / ".join(unread))
        lines.append(HINT)
    return "\n".join(lines)


def month_starts(entries: list[Entry], today: date) -> list[date]:
    """累計を出す月: 今月 ＋ 記録した日付の月（重複なし）。"""
    out = [today.replace(day=1)]
    for e in entries:
        m = e.day.replace(day=1)
        if m not in out:
            out.append(m)
    return out


# ---------------------------------------------------------------- 処理


async def _ai_entries(lines: list[str], today: date) -> tuple[list[Entry], set[int]]:
    numbered = "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))
    got = await claude_client.complete_json(_PROMPT.format(today=util.fmt_date(today), lines=numbered), {"entries": []})
    return clean_ai_entries(got.get("entries") if isinstance(got, dict) else None, today, lines)


async def _read_entries(text: str, today: date) -> tuple[list[Entry], list[str]]:
    """本文を行ごとに読み取る。(記録する行, 読み取れなかった行)。決まった書き方の行は、AI を使わずに読み取る。"""
    lines = [l.strip() for l in text.split("\n") if l.strip()][:MAX_LINES]
    entries: list[Entry] = []
    failed: list[str] = []
    for l in lines:
        e = parse_line(l, today)
        if e:
            entries.append(e)
        else:
            failed.append(l)
    unread = failed
    if failed:
        ai, matched = await _ai_entries(failed, today)
        entries += ai
        unread = [l for i, l in enumerate(failed) if i not in matched]
    return entries, unread


async def handle(message) -> None:
    text = (message.content or "").strip()
    if not text and not message.attachments:
        return
    now = util.now()
    today = now.date()

    saved = 0
    warn = False
    if message.attachments:  # 月末の楽天家計簿のスクショなど。金額は読み取らず、保存するだけ
        files = await idea._save_attachments(message, now, path_fn=vault_paths.household_attachment)
        saved = sum(1 for f in files if f["saved"])
        if saved:
            await util.ack(message, "📷")
        if saved < len(files):
            warn = True
            reasons = "、".join(f"{f['name']}（{f['reason']}）" for f in files if not f["saved"])
            await util.send_long(message.channel, f"保存できなかった添付があります: {reasons}")

    if not text:
        if warn:
            await util.ack(message, "⚠️")
        return

    entries, unread = await _read_entries(text, today)
    recorded: list[Entry] = []
    failed_rows = False
    for e in entries:
        try:
            await asyncio.to_thread(sheets.append, sheets.HOUSEHOLD, {
                "日付": util.fmt_date(e.day), "金額（円）": e.amount, "ジャンル": e.genre, "備考": e.note, "区分": e.kind})
            recorded.append(e)
        except Exception:  # noqa: BLE001  以降の行も書けない可能性が高いので、そこで止める
            log.warning("家計簿をシートに記録できませんでした", exc_info=True)
            failed_rows = True
            break
    if recorded:
        await util.ack(message, "📝")

    if not entries and not unread:
        return
    totals = None
    if recorded:
        try:
            recs = await asyncio.to_thread(sheets.records, sheets.HOUSEHOLD)
            totals = {m: month_total(recs, m) for m in month_starts(recorded, today)}
        except Exception:  # noqa: BLE001  累計が出せなくても、記録の確認は返す
            log.warning("娯楽費の累計を集計できませんでした", exc_info=True)
    reply = format_reply(recorded, totals, today, unread)
    if failed_rows:
        rest = [e.line or f"{e.kind} {e.genre} {e.amount:,}円" for e in entries[len(recorded):]]
        reply += ("\n" if reply else "") + "記録できなかった行があります（少し待ってから、もう一度送ってください）: " + " / ".join(rest)
    if unread or failed_rows or warn:
        await util.ack(message, "⚠️")
    await util.send_long(message.channel, reply)

"""タスクの文章から「実行日」と「期限」を取り出す（純粋関数。API は使わない）。

例（今日 = 2026-09-20 の場合）:
  「資料を送る 今日やる」        → 実行日 9/20、内容「資料を送る」
  「9/25にレポートを書く」       → 実行日 9/25
  「明日までに請求書を出す」     → 期限 9/21
  「企画書 期限は金曜」          → 期限 9/25
  「来週の月曜にやる 期限は月末」 → 実行日 9/21、期限 9/30（週は月曜始まり。日曜日の「来週の月曜」は明日）

判定ルール:
  - 日付の直後が「まで / までに / 以内 / 期限 / 締切」、または直前が「期限は / 締切:」なら **期限**、それ以外は **実行日**
  - 日付の直後が「の」（例: 10/5のライブ）なら、タスクの日付ではなく名詞の一部なので無視する
    （例外: 「明日の朝」のような時間帯の言葉が続く場合）
  - 「毎週月曜」のような繰り返し、「3日間」「1日3回」のような期間・回数は日付として扱わない
  - 解釈できない表現は、そのまま内容に残す（日付なしで登録する。勝手に推測しない）
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

WEEKDAYS = "月火水木金土日"

_REL = {"今日": 0, "本日": 0, "今夜": 0, "今晩": 0, "きょう": 0, "明日": 1, "あした": 1, "明後日": 2, "あさって": 2,
        "明々後日": 3, "しあさって": 3}

_DATE = re.compile(
    r"(?P<iso>(?P<iy>\d{4})\s*[-/年]\s*(?P<im>\d{1,2})\s*[-/月]\s*(?P<id>\d{1,2})\s*日?)"
    r"|(?P<md>(?P<mm>\d{1,2})\s*[/月]\s*(?P<md_d>\d{1,2})\s*日?)"
    r"|(?P<nextm>来月\s*の?\s*(?P<nmd>\d{1,2})\s*日)"
    r"|(?P<eom>(?:今月|来月)?末|月末)"
    r"|(?P<span>(?P<spk>今週|来週|今月|来月)\s*中)"
    r"|(?P<rel>" + "|".join(sorted(_REL, key=len, reverse=True)) + r")"
    r"|(?P<nd>(?P<n>\d+)\s*日後)"
    r"|(?P<nw>(?P<nwn>\d+)\s*週間後)"
    r"|(?P<wd>(?P<wk>今週|来週|再来週)?\s*の?\s*(?P<wc>[月火水木金土日])曜日?)"
    r"|(?P<dom>(?P<dd>\d{1,2})\s*日(?![間後目\d]))"
)
_TIME_OF_DAY = re.compile(r"^の\s*(?:朝|昼|夕方|夜|晩|午前|午後|うち|中)")
_DUE_AFTER = re.compile(r"^\s*(?:までに?|迄に?|以内に?|中に?|期限|締切|締め切り|〆切)(?:です|だ|に)?")
_DUE_BEFORE = re.compile(r"(?:期限|締切|締め切り|〆切|デッドライン)\s*(?:は|が|：|:)?\s*$")
_SCHED_AFTER = re.compile(r"^(?:に|は)?\s*(?:やる|する|やります|します|予定|から)?")  # 「明日は〜」「9/25に〜」の「は」「に」も取り除く
_RECURRING_BEFORE = re.compile(r"(?:毎週|毎月|毎日|隔週)\s*$")
_PUNCT = " 　、,。:：\t"


@dataclass
class Extracted:
    content: str
    scheduled: date | None = None
    due: date | None = None
    found: list[str] = field(default_factory=list)


def _last_day(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _add_month(d: date, months: int) -> tuple[int, int]:
    idx = d.year * 12 + (d.month - 1) + months
    return idx // 12, idx % 12 + 1


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _resolve(m: re.Match, today: date) -> date | None:
    g = m.groupdict()
    if g["iso"]:
        return _safe(int(g["iy"]), int(g["im"]), int(g["id"]))
    if g["md"]:
        d = _safe(today.year, int(g["mm"]), int(g["md_d"]))
        if d is None:
            return None
        return d if d >= today else _safe(today.year + 1, d.month, d.day)  # 過ぎた日付は、来年の同じ日とみなす
    if g["nextm"]:
        y, mo = _add_month(today, 1)
        return _safe(y, mo, int(g["nmd"]))
    if g["span"]:  # 「今週中」= その週の日曜、「今月中」= その月の末日
        k = g["spk"]
        if k in ("今週", "来週"):
            return today - timedelta(days=today.weekday()) + timedelta(days=6 + (7 if k == "来週" else 0))
        y, mo = _add_month(today, 1 if k == "来月" else 0)
        return _last_day(y, mo)
    if g["eom"]:
        text = m.group("eom")
        if text.startswith("来月"):
            y, mo = _add_month(today, 1)
            return _last_day(y, mo)
        return _last_day(today.year, today.month)
    if g["rel"]:
        return today + timedelta(days=_REL[g["rel"]])
    if g["nd"]:
        return today + timedelta(days=int(g["n"]))
    if g["nw"]:
        return today + timedelta(weeks=int(g["nwn"]))
    if g["wd"]:
        idx = WEEKDAYS.index(g["wc"])
        monday = today - timedelta(days=today.weekday())
        if g["wk"] == "来週":
            return monday + timedelta(days=7 + idx)
        if g["wk"] == "再来週":
            return monday + timedelta(days=14 + idx)
        if g["wk"] == "今週":
            d = monday + timedelta(days=idx)
            return d if d >= today else d + timedelta(days=7)  # 今週の残りの日が過ぎていれば、次の週
        delta = (idx - today.weekday()) % 7
        return today + timedelta(days=delta or 7)  # 「月曜」だけなら、次にくるその曜日
    if g["dom"]:
        day = int(g["dd"])
        d = _safe(today.year, today.month, day)
        if d is not None and d >= today:
            return d
        y, mo = _add_month(today, 1)
        return _safe(y, mo, day)
    return None


def extract(text: str, today: date) -> Extracted:
    """text から実行日・期限を取り出し、それらの言葉を取り除いた内容を返す。最初に見つかった実行日・期限だけを使う。"""
    src = unicodedata.normalize("NFKC", text)
    scheduled = due = None
    found: list[str] = []
    cuts: list[tuple[int, int]] = []
    for m in _DATE.finditer(src):
        start, end = m.start(), m.end()
        if _RECURRING_BEFORE.search(src[max(0, start - 3):start]):
            continue  # 毎週月曜 など
        rest = src[end:]
        tod = _TIME_OF_DAY.match(rest)
        if tod:
            end += tod.end()  # 「明日の朝」の「の朝」まで日付の一部として扱う
            rest = src[end:]
        elif rest.startswith("の") and not _DUE_AFTER.match(rest):
            continue  # 「10/5のライブ」: 日付は名詞の一部
        if m.group("wd") and re.match(r"[一-鿿]", rest[:1]):
            continue  # 「日曜大工」「月曜会議」のように、漢字が続く名詞は曜日として読まない
        d = _resolve(m, today)
        if d is None:
            continue
        before = _DUE_BEFORE.search(src[:start])
        after = _DUE_AFTER.match(rest)
        if before or after or m.group("span"):
            if due is not None:
                continue
            due = d
            if before:
                start = before.start()
            if after:
                end += after.end()
            elif m.group("span"):
                end += re.match(r"^\s*に?", rest).end()  # 「今週中に」の「に」
        else:
            if scheduled is not None:
                continue
            scheduled = d
            end += _SCHED_AFTER.match(rest).end()
        found.append(src[start:end])
        cuts.append((start, end))
    out, pos = [], 0
    for s, e in cuts:
        out.append(src[pos:s])
        pos = e
    out.append(src[pos:])
    content = re.sub(r"[ 　]{2,}", " ", "".join(out)).strip(_PUNCT)
    if not content:  # 日付の言葉だけの入力は、そのまま内容にする（何も失わない）
        content = src.strip()
    return Extracted(content=content, scheduled=scheduled, due=due, found=found)


def iso(d: date | None) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


def fmt_md(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def describe(scheduled: date | None, due: date | None) -> str:
    """確認用の短い表記。例: 「実行日 9/21(月)・期限 9/30(水)」。どちらも無ければ空。"""
    parts = []
    if scheduled:
        parts.append(f"実行日 {fmt_md(scheduled)}")
    if due:
        parts.append(f"期限 {fmt_md(due)}")
    return "・".join(parts)

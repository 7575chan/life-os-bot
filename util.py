"""副作用のない共通関数と Discord 補助関数。"""
import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta

import config

log = logging.getLogger("life-os.util")

DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def now() -> datetime:
    return datetime.now(config.TZ)


def today() -> date:
    return now().date()


def fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def fmt_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_date_any(value: str) -> date | None:
    """'2026-09-19' / '2026/9/19' / '2026-09-19 10:00' を date にする。"""
    if not value:
        return None
    m = DATE_RE.search(str(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def is_done(value: str) -> bool:
    return str(value).strip().upper() in {"TRUE", "✅", "済", "完了", "1", "YES"}


def is_stale(scheduled: str, today_: date, days: int = 3) -> bool:
    """実行予定日から days 日以上経過していれば True（バックログ退避の対象）。"""
    d = parse_date_any(scheduled)
    return d is not None and (today_ - d).days >= days


def to_int(value) -> int | None:
    try:
        return int(str(value).replace(",", "").replace("円", "").strip())
    except ValueError:
        return None


def extract_json(text: str):
    """応答文字列から最初の JSON オブジェクト/配列を取り出す。失敗時は None。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for start_char, end_char in (("{", "}"), ("[", "]")):
        s, e = text.find(start_char), text.rfind(end_char)
        if s != -1 and e > s:
            try:
                return json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                continue
    return None


_SEL_RE = re.compile(r"^[\d\s,、，と&・.]+$")


def parse_numbers(text: str) -> list[int] | None:
    """'1と3' '1, 3' '１、３' -> [1, 3]。番号だけの文でなければ None。"""
    t = unicodedata.normalize("NFKC", text).strip()
    if not t or not _SEL_RE.match(t):
        return None
    nums = [int(n) for n in re.findall(r"\d+", t)]
    return nums or None


def is_none_choice(text: str) -> bool:
    return unicodedata.normalize("NFKC", text).strip() in {"なし", "無し", "0", "ない", "なし。"}


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Discord の 2000 文字制限に合わせて改行優先で分割する。"""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))


def safe_filename(name: str, max_len: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "", name).strip()
    return (name or "untitled")[:max_len]


def month_range(d: date) -> tuple[date, date]:
    """d を含む月の (月初, 翌月初)。"""
    start = d.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt


def prev_month_start(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def is_last_day_of_month(d: date) -> bool:
    return (d + timedelta(days=1)).day == 1


# ---- Discord 補助 (discord を import しない純粋寄りの関数) ----

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


async def read_images(message) -> list[tuple[bytes, str]]:
    out = []
    for a in message.attachments:
        ctype = (a.content_type or "").split(";")[0]
        if ctype in IMAGE_TYPES:
            out.append((await a.read(), ctype))
    return out


async def ack(message, emoji: str = "📝"):
    """リアクションを付ける。失敗しても処理は止めないが、原因が分かるようにログに残す（権限不足など）。"""
    try:
        await message.add_reaction(emoji)
    except Exception:
        log.warning("リアクション %s を付けられませんでした", emoji, exc_info=True)


async def send_long(channel, text: str, reference=None):
    last = None
    for i, chunk in enumerate(split_message(text)):
        last = await channel.send(chunk, reference=reference if i == 0 else None)
    return last


_MOOD_LABEL = re.compile(r"(?:ご?機嫌度?|きげん|気分)\s*[:：は]?\s*([1-5])")
_MOOD_LEAD = re.compile(r"^\s*([1-5])(?:\s|[、,.。:：/／]|$)")


def parse_mood(text: str) -> int | None:
    """ご機嫌度(1〜5)を取り出す。「ご機嫌度4」「機嫌: 3」または先頭の数字。無ければ None。"""
    t = unicodedata.normalize("NFKC", text)
    m = _MOOD_LABEL.search(t) or _MOOD_LEAD.match(t)
    return int(m.group(1)) if m else None


def clamp_int(value, lo: int, hi: int) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None

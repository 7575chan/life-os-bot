"""執筆記録シートの読み取り（SPEC §4）。**読み取り専用**。書き込みはしない。

構造（`シート1`）: 1行目=作品名（B列〜）、2行目=目標、3行目=締切（シリアル値）、6行目〜=
A列に記録日時（1日1行）、B列〜にその時点の各作品の総文字数。作品は列位置ではなく列名で特定する。
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import config
import util

EPOCH = date(1899, 12, 30)  # Google スプレッドシート/Excel のシリアル値の起点
ACTIVE_DAYS = 14
_PREFIX = re.compile(r"^\d{4}-\d{2}\s+")


def normalize_title(title: str) -> str:
    """先頭の `YYYY-MM ` を除いた正規名。"""
    return _PREFIX.sub("", title.strip())


def to_datetime(value) -> datetime | None:
    """シリアル値、または日付文字列を JST の datetime にする。日付でなければ None。"""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value < 20000:  # 極端に小さい数値は日付ではない（ラベル行の「1」など）
            return None
        return datetime(1899, 12, 30, tzinfo=config.TZ) + timedelta(days=float(value))
    d = util.parse_date_any(str(value))
    return datetime(d.year, d.month, d.day, tzinfo=config.TZ) if d else None


def _int(value) -> int | None:
    if isinstance(value, bool) or value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return util.to_int(value)


@dataclass
class Work:
    name: str
    title: str
    target: int | None
    deadline: date | None


@dataclass
class Snapshot:
    ts: datetime
    totals: dict[str, int]

    @property
    def day(self) -> date:
        return self.ts.date()


@dataclass
class WritingLog:
    works: dict[str, Work] = field(default_factory=dict)
    snaps: list[Snapshot] = field(default_factory=list)


def parse(values: list[list]) -> WritingLog:
    """get_all_values(UNFORMATTED_VALUE) の2次元配列を解釈する。想定外の行は静かに無視する。"""
    log = WritingLog()
    if not values:
        return log
    header = values[0]
    cols: dict[int, str] = {}
    for j in range(1, len(header)):
        title = str(header[j]).strip()
        if title:
            cols[j] = normalize_title(title)
    labels = {str(r[0]).strip(): r for r in values[1:6] if r and isinstance(r[0], str) and to_datetime(r[0]) is None}
    targets, deadlines = labels.get("目標", []), labels.get("締切", [])
    for j, name in cols.items():
        dl = to_datetime(deadlines[j]) if j < len(deadlines) else None
        log.works[name] = Work(name, str(header[j]).strip(), _int(targets[j]) if j < len(targets) else None,
                               dl.date() if dl else None)
    for row in values[1:]:
        ts = to_datetime(row[0]) if row else None
        if ts is None:  # ラベル行（目標・締切など）や日付でない行は飛ばす
            continue
        totals = {}
        for j, name in cols.items():
            n = _int(row[j]) if j < len(row) else None
            totals[name] = n if n is not None else 0
        log.snaps.append(Snapshot(ts, totals))
    log.snaps.sort(key=lambda s: s.ts)
    return log


def stats_for(log: WritingLog, day: date) -> dict | None:
    """day 以前の最新の記録行と、その直前の行の差分。比較できなければ None。

    マイナス（削除・推敲）は増加に含めない。合計の増加は各作品の増加分の合計（0以上）。
    """
    upto = [s for s in log.snaps if s.day <= day]
    if len(upto) < 2:
        return None
    latest = upto[-1]
    prev = next((s for s in reversed(upto[:-1]) if s.day < latest.day), None)
    if prev is None:
        return None
    window_start = day - timedelta(days=ACTIVE_DAYS)
    base = next((s for s in upto if s.day >= window_start), upto[0])
    works = []
    for name, total in latest.totals.items():
        added = max(0, total - prev.totals.get(name, 0))
        recent = max(0, total - base.totals.get(name, 0))
        w = log.works.get(name)
        pct = round(total / w.target * 100) if w and w.target else None
        days_left = (w.deadline - day).days if w and w.deadline and w.deadline >= day else None
        works.append({"name": name, "added": added, "total": total, "target": w.target if w else None,
                      "pct": pct, "days_left": days_left, "active": recent > 0})
    return {"date": latest.day, "prev_date": prev.day, "total_added": sum(w["added"] for w in works),
            "works": sorted(works, key=lambda w: -w["added"])}


def format_morning(stats: dict | None, day_yesterday: date) -> str | None:
    """朝報告の執筆実績ブロック。増えていない日・記録が無い日は何も出さない（責めない）。"""
    if not stats or stats["total_added"] <= 0:
        return None
    lines = ["📊 昨日の執筆実績"]
    if stats["date"] != day_yesterday:
        lines[0] += f"（{stats['date']:%m/%d} 時点の記録）"
    lines.append(f"・合計 +{stats['total_added']:,}文字")
    for w in stats["works"]:
        if w["added"] <= 0 or not w["active"]:
            continue
        extra = ""
        if w["target"]:
            extra = f"（累計 {w['total']:,} / 目標 {w['target']:,}・達成 {w['pct']}%"
            extra += f"・締切まで{w['days_left']}日）" if w["days_left"] is not None else "）"
        else:
            extra = f"（累計 {w['total']:,}）"
        lines.append(f"・{w['name']} +{w['added']:,}{extra}")
    return "\n".join(lines)


def fetch() -> WritingLog | None:
    """Google スプレッドシートから読み取る（同期）。未設定なら None。"""
    if not config.WORDCOUNT_SHEET_ID:
        return None
    from gspread.utils import ValueRenderOption

    import sheets

    src = sheets.client().open_by_key(config.WORDCOUNT_SHEET_ID)
    w = src.worksheet(config.WORDCOUNT_TAB) if config.WORDCOUNT_TAB else src.sheet1
    return parse(w.get_all_values(value_render_option=ValueRenderOption.unformatted))


def yesterday_block(day: date) -> str | None:
    """day（今日）の朝報告に載せる、昨日の執筆実績。失敗しても None（朝報告を止めない）。"""
    try:
        log = fetch()
        if log is None:
            return None
        y = day - timedelta(days=1)
        return format_morning(stats_for(log, y), y)
    except Exception:  # noqa: BLE001
        return None

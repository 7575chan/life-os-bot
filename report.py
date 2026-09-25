"""`04-report`: 週次・月次レポート（SPEC §5。投稿専用）。

- 週次: 日曜 20:00 / 月次: 月末日 20:00（`scheduler.py`。時刻は `REPORT_TIME`）
- 内容: ① 体調・感情（ご機嫌度の平均と条件別の傾向。あわせて、取扱マニュアルを更新する）② お金（娯楽費・事業損益・未転記件数）
  ③ 執筆・タスク・プロジェクトの進捗 ④ スクラップの再浮上（進行中のプロジェクトに関連する過去記事を1本）
- 数字を淡々と示すだけで、評価・助言・比較による叱責はしない。増えていない・記録が無いことは、責めずに事実だけ書く
- 保存先: `06-Life-OS/04-report/Weekly_YYYY-MM-DD.md` / `Monthly_YYYY-MM.md`（新規ファイルなので GAS 中継で作成）と `04-report` シート
- 集計は、Bot 自身が書いたシートと執筆記録シートだけを使う（Obsidian の許可外ノートは読まない）。
  AI を使うのは、④の記事を選ぶときと、①で取扱マニュアル（`Health-Manual.md`）を作り直すときだけ
  （レポート本文の数字は AI に作らせない）
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

import claude_client
import health_analysis
import notes
import sheets
import state
import util
import vault_paths
import writing_log
from handlers import health, ledger

log = logging.getLogger("life-os.report")

TREND_DAYS = 90  # 条件別の傾向を見る、直近の日数
MIN_GROUP_DAYS = 2  # 条件別の平均を出すのに必要な、それぞれの日数（少なすぎるものは出さない）
MAX_SCRAP_CANDIDATES = 150
MAX_SHEET_TEXT = 30_000  # シートの1セルの上限（32,767 文字）に収める
ROOM_LABELS = {"novel": "小説", "trpg": "TRPG", "others": "その他の制作"}
CONSULT_KINDS = ("検索", "相談")  # プロジェクト部屋の投稿のうち、記録ではなく質問だったもの
SUB = "　"  # 行頭にこれがある行は、直前の行の下位の項目（作品ごとの増加など）


# ---------------------------------------------------------------- 期間


@dataclass
class Period:
    kind: str  # "weekly" / "monthly"
    start: date  # 期間の最初の日
    end: date  # 期間の次の日（含まない）
    label: str
    filename: str  # 拡張子なし

    @property
    def kind_jp(self) -> str:
        return "週次" if self.kind == "weekly" else "月次"

    @property
    def prefix(self) -> str:
        """投稿の先頭の文字。別の場所の Bot がすでに投稿したかを確かめるのにも使う。"""
        return f"📈 {self.kind_jp}レポート"

    @property
    def last_day(self) -> date:
        return self.end - timedelta(days=1)


def weekly_period(day: date) -> Period:
    """day（日曜）で終わる7日間。"""
    start = day - timedelta(days=6)
    return Period("weekly", start, day + timedelta(days=1), f"{start.month}/{start.day}〜{day.month}/{day.day}",
                  f"Weekly_{day:%Y-%m-%d}")


def monthly_period(day: date) -> Period:
    start = day.replace(day=1)
    return Period("monthly", start, util.month_range(day)[1], f"{day.year}年{day.month}月", f"Monthly_{day:%Y-%m}")


def period_for(kind: str, day: date) -> Period:
    return weekly_period(day) if kind == "weekly" else monthly_period(day)


def kinds_due(day: date) -> list[str]:
    """その日に発行するレポート。日曜なら週次、月末日なら月次（両方の日は両方）。"""
    out = []
    if day.weekday() == 6:
        out.append("weekly")
    if util.is_last_day_of_month(day):
        out.append("monthly")
    return out


# ---------------------------------------------------------------- 集計に使うデータ


@dataclass
class Data:
    diary: list = field(default_factory=list)  # 以下、sheets.records の形 [(行番号, {列名: 値})]
    health: list = field(default_factory=list)
    household: list = field(default_factory=list)
    ledger: list = field(default_factory=list)
    articles: list = field(default_factory=list)
    projects: dict = field(default_factory=dict)  # 部屋 -> records
    tasks_done: int | None = None
    writing: writing_log.WritingLog | None = None
    missing: list = field(default_factory=list)  # 読み取れなかったデータの名前（レポートに書く）


def collect(period: Period, today: date) -> Data:
    """シートを読む（同期）。1つが読めなくても、ほかのデータでレポートを作る。"""
    d = Data()

    def read(label: str, fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            log.warning("レポート用に %s を読めませんでした", label, exc_info=True)
            d.missing.append(label)
            return default

    d.diary = read("日記", lambda: sheets.records(sheets.DIARY), [])
    d.health = read("体調", lambda: sheets.records(sheets.HEALTH), [])
    d.household = read("家計簿", lambda: sheets.records(sheets.HOUSEHOLD), [])
    d.ledger = read("帳簿", lambda: sheets.records(sheets.LEDGER), [])
    d.articles = read("スクラップ", lambda: sheets.records(sheets.ARTICLES), [])
    for key, name in sheets.PROJECT_SHEETS.items():
        d.projects[key] = read(ROOM_LABELS[key] + "プロジェクト", lambda n=name: sheets.records(n), [])
    d.tasks_done = read("タスク", lambda: sheets.completed_count(period.start, period.end), None)
    d.writing = read("執筆記録", writing_log.fetch, None)
    return d


# ---------------------------------------------------------------- 純粋関数: 集計


def _rows_in(recs, col: str, start: date, end: date) -> list[dict]:
    out = []
    for _, rec in recs:
        day = util.parse_date_any(rec.get(col, ""))
        if day and start <= day < end:
            out.append(rec)
    return out


def _yen(rec: dict) -> int | None:
    return util.to_int(str(rec.get("金額（円）", "")).replace("¥", "").replace("￥", ""))


def _avg(xs: list[int | float]) -> float | None:
    """平均（小数第1位まで、四捨五入。Python の round は 3.25 → 3.2 と偶数丸めになるので使わない）。"""
    return math.floor(sum(xs) / len(xs) * 10 + 0.5) / 10 if xs else None


def _moods(rows: list[dict]) -> list[int]:
    return [m for m in (util.clamp_int(r.get("ご機嫌度"), 1, 5) for r in rows) if m]


def leisure_total(recs, start: date, end: date) -> int:
    """[start, end) の、娯楽費の合計。"""
    total = 0
    for rec in _rows_in(recs, "日付", start, end):
        amount = _yen(rec)
        if amount is not None and str(rec.get("区分", "")).strip() == "娯楽":
            total += amount
    return total


def leisure_by_genre(recs, start: date, end: date, top: int = 3) -> list[tuple[str, int]]:
    by: Counter = Counter()
    for rec in _rows_in(recs, "日付", start, end):
        amount = _yen(rec)
        if amount is not None and str(rec.get("区分", "")).strip() == "娯楽":
            by[str(rec.get("ジャンル", "")).strip() or "（ジャンルなし）"] += amount
    return by.most_common(top)


def section_health(d: Data, p: Period) -> list[str]:
    lines = []
    diary = _rows_in(d.diary, "日付", p.start, p.end)
    moods = _moods(diary)
    lines.append(f"ご機嫌度の平均 {_avg(moods)}（{len(moods)}日分）" if moods else "ご機嫌度の記録はありません")
    scores = [s for s in (util.clamp_int(r.get("体調スコア"), 1, 5) for r in _rows_in(d.health, "日時", p.start, p.end)) if s]
    if scores:
        lines.append(f"体調スコアの平均 {_avg(scores)}（{len(scores)}件）")
    since = p.end - timedelta(days=TREND_DAYS)
    diary_t, health_t = _rows_in(d.diary, "日付", since, p.end), _rows_in(d.health, "日時", since, p.end)
    tab = health_analysis.sleep_mood_table(health_t, diary_t)
    under, over, thr = tab["under"], tab["over"], f"{tab['threshold_hours']:g}"
    if under["days"] >= MIN_GROUP_DAYS and over["days"] >= MIN_GROUP_DAYS and under["avg_mood"] is not None and over["avg_mood"] is not None:
        lines.append(f"直近{TREND_DAYS}日の傾向: 睡眠{thr}時間未満の日のご機嫌度 {under['avg_mood']}（{under['days']}日）"
                     f" / {thr}時間以上 {over['avg_mood']}（{over['days']}日）")
    rain = _moods([r for r in diary_t if "雨" in str(r.get("天気", ""))])
    other = _moods([r for r in diary_t if r.get("天気") and "雨" not in str(r.get("天気", ""))])
    if len(rain) >= MIN_GROUP_DAYS and len(other) >= MIN_GROUP_DAYS:
        lines.append(f"直近{TREND_DAYS}日の傾向: 雨の日のご機嫌度 {_avg(rain)}（{len(rain)}日） / 雨以外 {_avg(other)}（{len(other)}日）")
    return lines


def _pct_change(cur: int, prev: int) -> str:
    if prev <= 0:
        return ""
    pct = round((cur - prev) / prev * 100)
    return f"・先月比 {'+' if pct > 0 else ''}{pct}%" if pct else "・先月と同じ"


def section_money(d: Data, p: Period) -> list[str]:
    lines = []
    cur_start = p.last_day.replace(day=1)
    prev_start = util.prev_month_start(cur_start)
    if p.kind == "weekly":
        lines.append(f"今週の娯楽費 {leisure_total(d.household, p.start, p.end):,}円")
        span = (p.end - cur_start).days  # 月初から期間の終わりまでの日数
        prev_end = min(prev_start + timedelta(days=span), cur_start)  # 先月の同じ日まで
        cur = leisure_total(d.household, cur_start, p.end)
        lines.append(f"今月の娯楽費（累計） {cur:,}円（先月の同じ日まで {leisure_total(d.household, prev_start, prev_end):,}円）")
    else:
        cur, prev = leisure_total(d.household, cur_start, p.end), leisure_total(d.household, prev_start, cur_start)
        lines.append(f"今月の娯楽費 {cur:,}円（先月 {prev:,}円{_pct_change(cur, prev)}）")
    genres = leisure_by_genre(d.household, p.start, p.end)
    if genres:
        lines.append("娯楽費の内訳: " + "・".join(f"{g} {n:,}円" for g, n in genres))
    sales, expense = ledger.month_summary(d.ledger, cur_start)
    lines.append(f"事業（今月）: 概算損益 {ledger.profit_text(sales, expense)}（売上 {sales:,}円 − 経費 {expense:,}円）")
    y, a = ledger.unposted_counts(d.ledger)
    lines.append(f"未転記: やよい {y}件 / 分析シート {a}件")
    return lines


def section_progress(d: Data, p: Period, today: date) -> list[str]:
    lines = []
    if d.writing is not None:
        g = writing_log.period_growth(d.writing, p.start, p.end, today)
        if g is None:
            lines.append("執筆記録シートに、この期間の記録はありません")
        else:
            span = f"（記録 {g['base_day']:%m/%d}〜{g['as_of']:%m/%d}）"
            if g["total_added"] <= 0:
                lines.append(f"執筆記録シートでは、この期間の増加は見当たりませんでした{span}")
            else:
                lines.append(f"執筆: 合計 +{g['total_added']:,}文字{span}")
                for w in g["works"]:
                    if w["added"] <= 0:
                        continue
                    extra = f"累計 {w['total']:,}"
                    if w["target"]:
                        extra += f" / 目標 {w['target']:,}・達成 {w['pct']}%"
                    if w["days_left"] is not None:
                        extra += f"・締切まで{w['days_left']}日"
                    lines.append(f"{SUB}{w['name']} +{w['added']:,}（{extra}）")
    if d.tasks_done is not None:
        lines.append(f"完了したタスク {d.tasks_done}件")
    for key, recs in d.projects.items():
        rows = [r for r in _rows_in(recs, "日時", p.start, p.end) if str(r.get("種別", "")).strip() not in CONSULT_KINDS]
        if rows:
            by = Counter(str(r.get("作品名", "")).strip() or "（作品名なし）" for r in rows)
            lines.append(f"{ROOM_LABELS[key]}のプロジェクト: {len(rows)}件の記録（" + "・".join(f"{n} {c}" for n, c in by.most_common()) + "）")
    return lines


def active_projects(d: Data, p: Period) -> list[str]:
    """期間中に触ったプロジェクトと、その最近の記録の一部（記事を選ぶ手がかり）。"""
    out = []
    for key, recs in d.projects.items():
        by: dict[str, list[str]] = {}
        for r in _rows_in(recs, "日時", p.start, p.end):
            if str(r.get("種別", "")).strip() in CONSULT_KINDS:
                continue
            by.setdefault(str(r.get("作品名", "")).strip() or "（作品名なし）", []).append(str(r.get("内容", "")).strip()[:50])
        for name, notes_ in by.items():
            out.append(f"{ROOM_LABELS[key]}「{name}」: " + " / ".join(n for n in notes_[-3:] if n))
    return out


def scrap_candidates(d: Data, p: Period) -> list[dict]:
    """期間より前に保存した記事（新しい順に最大 MAX_SCRAP_CANDIDATES 件）。"""
    older = [rec for _, rec in d.articles
             if (day := util.parse_date_any(rec.get("日時", ""))) and day < p.start and str(rec.get("タイトル", "")).strip()]
    return older[-MAX_SCRAP_CANDIDATES:][::-1]


# ---------------------------------------------------------------- スクラップの再浮上（AI は、記事を1本選ぶときだけ）

_SCRAP_PROMPT = """次は、ユーザーが今期に取り組んだプロジェクトと、過去に保存した記事の一覧（番号つき）です。
プロジェクトの参考になりそうな記事を1本だけ選び、JSONだけを返してください。関連が薄い・判断できないときは index を null にしてください。
{{"index": 記事の番号 または null, "reason": どうプロジェクトに関連するかを1文で（評価や助言はしない）}}

【プロジェクト】
{projects}

【記事】
{articles}"""


def clean_scrap_pick(raw, candidates: list[dict]) -> dict | None:
    """AI の選択を検証する。番号が範囲外・null なら None（記事は載せない）。"""
    if not isinstance(raw, dict):
        return None
    idx = raw.get("index")
    if isinstance(idx, bool) or not isinstance(idx, int) or not 1 <= idx <= len(candidates):
        return None
    rec = candidates[idx - 1]
    return {"title": str(rec.get("タイトル", "")).strip(), "url": str(rec.get("URL", "")).strip(),
            "reason": str(raw.get("reason") or "").strip()[:150]}


async def pick_scrap(d: Data, p: Period) -> dict | None:
    projects, cands = active_projects(d, p), scrap_candidates(d, p)
    if not projects or not cands:
        return None
    articles = "\n".join(
        f"{i}. {str(r.get('タイトル', '')).strip()}｜{str(r.get('タグ', '')).strip()}｜{str(r.get('3行要約', '')).strip()[:80]}"
        for i, r in enumerate(cands, 1))
    got = await claude_client.complete_json(_SCRAP_PROMPT.format(projects="\n".join(projects), articles=articles), {})
    return clean_scrap_pick(got, cands)


def section_scrap(pick: dict | None) -> list[str]:
    if not pick:
        return []
    text = f"📰 {pick['title']}" + (f"\n{pick['url']}" if pick["url"] else "")
    return [text + (f"\n{pick['reason']}" if pick["reason"] else "")]


# ---------------------------------------------------------------- 組み立てと保存


@dataclass
class Report:
    period: Period
    text: str  # Discord とシートに使う
    note: str  # Obsidian のノート


MANUAL_UPDATED = "取扱マニュアルを更新しました（Health-Manual.md。前の版は Health-Manual-前回.md に残してあります）"
MANUAL_SKIPPED = "取扱マニュアルの更新は、今回は見送りました（「取扱マニュアル」と 02-health に送ると、いつでも更新できます）"
MANUAL_ALREADY = "取扱マニュアルは、今日すでに更新しています"


async def refresh_manual(day: date) -> str:
    """週次・月次レポートの「体調・感情」で、取扱マニュアル（`Health-Manual.md`）を更新する。レポートに書く1行を返す。

    週次と月次が同じ日に重なっても、更新は1回だけ。更新できなくても、レポートは出す（責めずに、見送ったことだけ書く）。"""
    key = f"manual_updated:{day}"
    if state.get_kv(key):
        return MANUAL_ALREADY
    try:
        await health.update_manual(day)
    except Exception:  # noqa: BLE001
        log.warning("取扱マニュアルを更新できませんでした", exc_info=True)
        return MANUAL_SKIPPED
    state.set_kv(key, "1")
    return MANUAL_UPDATED


def build_sections(d: Data, p: Period, today: date, pick: dict | None, manual_line: str | None = None) -> list[tuple[str, list[str]]]:
    sections = [("体調・感情", section_health(d, p) + ([manual_line] if manual_line else [])), ("お金", section_money(d, p)),
                ("執筆・タスク・プロジェクト", section_progress(d, p, today))]
    scrap = section_scrap(pick)
    if scrap:
        sections.append(("スクラップの再浮上", scrap))
    return sections


def _discord_line(line: str) -> str:
    return line if line.startswith(SUB) else f"・{line}"


def _note_line(line: str) -> str:
    if line.startswith(SUB):
        return f"  - {line.lstrip(SUB)}"
    return "- " + line.replace("\n", "\n  ")  # 記事の URL などの続きの行は、同じ項目の中に


def render(p: Period, sections: list[tuple[str, list[str]]], missing: list[str], issued: date) -> Report:
    shown = [(title, lines) for title, lines in sections if lines]
    body = [f"■ {title}\n" + "\n".join(_discord_line(l) for l in lines) for title, lines in shown]
    if missing:
        body.append("※読み取れなかったため省略したデータ: " + "・".join(missing))
    text = f"{p.prefix}（{p.label}）\n\n" + "\n\n".join(body)
    note = [f"# {p.kind_jp}レポート {p.label}", f"発行日: {util.fmt_date(issued)}"]
    note += [f"## {title}\n" + "\n".join(_note_line(l) for l in lines) for title, lines in shown]
    if missing:
        note.append("> 読み取れなかったため省略したデータ: " + "・".join(missing))
    return Report(p, text, "\n\n".join(note) + "\n")


async def generate(kind: str, day: date, *, update_manual: bool = True) -> Report:
    """レポートを作る。update_manual=False なら、取扱マニュアルは更新しない（試し作りは、何も保存しない）。"""
    p = period_for(kind, day)
    d = await asyncio.to_thread(collect, p, day)
    try:
        pick = await pick_scrap(d, p)
    except Exception:  # noqa: BLE001  記事を選べなくても、レポートは出す
        log.warning("スクラップの再浮上を選べませんでした", exc_info=True)
        pick = None
    manual_line = await refresh_manual(day) if update_manual else None
    return render(p, build_sections(d, p, day, pick, manual_line), d.missing, day)


def save(rep: Report, issued: date) -> list[str]:
    """ノート（Obsidian）とシートに保存する（同期）。失敗したものの名前を返す。片方が失敗しても、もう片方は保存する。"""
    failed = []
    try:
        notes.get_store().write(vault_paths.report(rep.period.filename), rep.note)
    except Exception:  # noqa: BLE001
        log.warning("レポートをノートに保存できませんでした", exc_info=True)
        failed.append("ノート")
    try:
        sheets.append(sheets.REPORTS, {"発行日": util.fmt_date(issued), "種別": rep.period.kind_jp, "本文": rep.text[:MAX_SHEET_TEXT]})
    except Exception:  # noqa: BLE001
        log.warning("レポートをシートに保存できませんでした", exc_info=True)
        failed.append("シート")
    return failed

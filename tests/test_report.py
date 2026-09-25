import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import report
import scheduler
import sheets
import state
import util
import vault_paths
import writing_log
from handlers import health

WEEK_DAY = date(2026, 9, 27)  # 日曜
MONTH_DAY = date(2026, 9, 30)  # 月末日（水）


def run(coro):
    return asyncio.run(coro)


def recs(*rows):
    return [(i + 2, dict(r)) for i, r in enumerate(rows)]


@pytest.fixture(autouse=True)
def manual(monkeypatch):
    """取扱マニュアルの更新（AI とノートの書き込み）と、状態ストアを偽物にする。"""
    kv, calls = {}, []

    async def update_manual(day):
        calls.append(day)
        if kv.get("fail"):
            raise RuntimeError("api down")
        return "本文"

    monkeypatch.setattr(health, "update_manual", update_manual)
    monkeypatch.setattr(state, "get_kv", lambda key, default=None: kv.get(key, default))
    monkeypatch.setattr(state, "set_kv", lambda key, value: kv.__setitem__(key, value))
    return NS(kv=kv, calls=calls)


# ---------------------------------------------------------------- 期間と発行日


def test_periods():
    w = report.weekly_period(WEEK_DAY)
    assert (w.start, w.end, w.last_day, w.label, w.filename) == (date(2026, 9, 21), date(2026, 9, 28), WEEK_DAY, "9/21〜9/27", "Weekly_2026-09-27")
    assert w.kind_jp == "週次" and w.prefix == "📈 週次レポート"
    m = report.monthly_period(MONTH_DAY)
    assert (m.start, m.end, m.last_day, m.label, m.filename) == (date(2026, 9, 1), date(2026, 10, 1), MONTH_DAY, "2026年9月", "Monthly_2026-09")
    assert m.prefix == "📈 月次レポート"
    assert report.period_for("weekly", WEEK_DAY).kind == "weekly" and report.period_for("monthly", MONTH_DAY).kind == "monthly"


def test_kinds_due():
    assert report.kinds_due(WEEK_DAY) == ["weekly"]  # 日曜
    assert report.kinds_due(MONTH_DAY) == ["monthly"]  # 月末日
    assert report.kinds_due(date(2026, 5, 31)) == ["weekly", "monthly"]  # 日曜かつ月末日
    assert report.kinds_due(date(2026, 9, 28)) == []
    assert report.kinds_due(date(2028, 2, 29)) == ["monthly"]  # うるう年の月末


# ---------------------------------------------------------------- ① 体調・感情


def health_data():
    diary = recs(
        {"日付": "2026-09-21", "ご機嫌度": "2", "天気": "雨 15〜19℃"}, {"日付": "2026-09-22", "ご機嫌度": "2", "天気": "小雨 15〜19℃"},
        {"日付": "2026-09-23", "ご機嫌度": "4", "天気": "晴れ 15〜22℃"}, {"日付": "2026-09-24", "ご機嫌度": "5", "天気": "くもり 15〜22℃"},
        {"日付": "2026-09-25", "ご機嫌度": "", "天気": ""})
    health = recs(
        {"日時": "2026-09-21 08:00", "睡眠時間": "7", "体調スコア": "2"}, {"日時": "2026-09-22 08:00", "睡眠時間": "7時間30分", "体調スコア": "3"},
        {"日時": "2026-09-23 08:00", "睡眠時間": "10", "体調スコア": "4"}, {"日時": "2026-09-24 08:00", "睡眠時間": "10.5", "体調スコア": "5"})
    return report.Data(diary=diary, health=health)


def test_section_health_averages_and_conditional_trends():
    lines = report.section_health(health_data(), report.weekly_period(WEEK_DAY))
    assert lines[0] == "ご機嫌度の平均 3.3（4日分）"  # 記録の無い日は数えない
    assert lines[1] == "体調スコアの平均 3.5（4件）"
    assert "直近90日の傾向: 睡眠9時間未満の日のご機嫌度 2.0（2日） / 9時間以上 4.5（2日）" in lines
    assert "直近90日の傾向: 雨の日のご機嫌度 2.0（2日） / 雨以外 4.5（2日）" in lines


def test_section_health_hides_small_groups_and_handles_no_data():
    d = health_data()
    d.diary = d.diary[:3]  # 雨2日・雨以外1日
    d.health = d.health[:3]
    lines = report.section_health(d, report.weekly_period(WEEK_DAY))
    assert not any("傾向" in l for l in lines)  # 片方が MIN_GROUP_DAYS 未満なら出さない
    assert report.section_health(report.Data(), report.weekly_period(WEEK_DAY)) == ["ご機嫌度の記録はありません"]


def test_section_health_only_counts_the_period():
    d = health_data()
    d.diary += recs({"日付": "2026-09-10", "ご機嫌度": "1", "天気": ""})  # 期間外
    assert report.section_health(d, report.weekly_period(WEEK_DAY))[0] == "ご機嫌度の平均 3.3（4日分）"


# ---------------------------------------------------------------- ② お金


def money_data():
    hh = recs(
        {"日付": "2026-09-22", "金額（円）": 3000, "ジャンル": "ガチャ", "区分": "娯楽"}, {"日付": "2026-09-25", "金額（円）": "1,500", "ジャンル": "漫画", "区分": "娯楽"},
        {"日付": "2026-09-24", "金額（円）": 800, "ジャンル": "カフェ", "区分": "現金"}, {"日付": "2026-09-10", "金額（円）": 2000, "ジャンル": "ガチャ", "区分": "娯楽"},
        {"日付": "2026-08-20", "金額（円）": 5000, "ジャンル": "ゲーム", "区分": "娯楽"}, {"日付": "2026-08-27", "金額（円）": 1000, "ジャンル": "漫画", "区分": "娯楽"},
        {"日付": "2026-08-29", "金額（円）": 700, "ジャンル": "漫画", "区分": "娯楽"})
    lg = recs(
        {"日付": "2026-09-10", "種別": "売上", "金額（円）": 50000, "内容": "", "やよい転記": "TRUE", "分析シート転記": ""},
        {"日付": "2026-09-05", "種別": "経費", "金額（円）": 1200, "内容": "", "やよい転記": "", "分析シート転記": ""})
    return report.Data(household=hh, ledger=lg)


def test_section_money_weekly():
    lines = report.section_money(money_data(), report.weekly_period(WEEK_DAY))
    assert lines == ["今週の娯楽費 4,500円",
                     "今月の娯楽費（累計） 6,500円（先月の同じ日まで 6,000円）",  # 8/1〜8/27。8/29 は含めない
                     "娯楽費の内訳: ガチャ 3,000円・漫画 1,500円",
                     "事業（今月）: 概算損益 +48,800円（売上 50,000円 − 経費 1,200円）",
                     "未転記: やよい 1件 / 分析シート 2件"]


def test_section_money_monthly_with_change_from_last_month():
    lines = report.section_money(money_data(), report.monthly_period(MONTH_DAY))
    assert lines[0] == "今月の娯楽費 6,500円（先月 6,700円・先月比 -3%）"
    assert lines[1] == "娯楽費の内訳: ガチャ 5,000円・漫画 1,500円"


def test_money_change_wording_is_neutral():
    assert report._pct_change(115, 100) == "・先月比 +15%" and report._pct_change(100, 100) == "・先月と同じ"
    assert report._pct_change(100, 0) == ""  # 先月が0円なら、割合は出さない


def test_section_money_with_no_data():
    lines = report.section_money(report.Data(), report.weekly_period(WEEK_DAY))
    assert lines[0] == "今週の娯楽費 0円" and "未転記: やよい 0件 / 分析シート 0件" in lines and "事業（今月）: 概算損益 ±0円（売上 0円 − 経費 0円）" in lines


def test_leisure_helpers_ignore_unreadable_rows():
    rows = recs({"日付": "2026-09-22", "金額（円）": "たくさん", "区分": "娯楽"}, {"日付": "", "金額（円）": 100, "区分": "娯楽"},
                {"日付": "2026-09-22", "金額（円）": "¥300", "区分": "娯楽", "ジャンル": ""})
    assert report.leisure_total(rows, date(2026, 9, 1), date(2026, 10, 1)) == 300
    assert report.leisure_by_genre(rows, date(2026, 9, 1), date(2026, 10, 1)) == [("（ジャンルなし）", 300)]


# ---------------------------------------------------------------- ③ 執筆・タスク・プロジェクト


def writing():
    works = {"A": writing_log.Work("A", "A", 10000, date(2026, 10, 7)), "B": writing_log.Work("B", "B", None, None)}
    snaps = [writing_log.Snapshot(datetime(2026, 9, 14, 23, 0), {"A": 900, "B": 500}),
             writing_log.Snapshot(datetime(2026, 9, 20, 23, 0), {"A": 1000, "B": 500}),
             writing_log.Snapshot(datetime(2026, 9, 27, 23, 0), {"A": 1800, "B": 500})]
    return writing_log.WritingLog(works, snaps)


def test_period_growth():
    g = writing_log.period_growth(writing(), date(2026, 9, 21), date(2026, 9, 28), WEEK_DAY)
    assert (g["base_day"], g["as_of"], g["total_added"]) == (date(2026, 9, 20), date(2026, 9, 27), 800)
    a = g["works"][0]
    assert (a["name"], a["added"], a["total"], a["pct"], a["days_left"]) == ("A", 800, 1800, 18, 10)
    assert writing_log.period_growth(writing(), date(2026, 10, 1), date(2026, 10, 8), WEEK_DAY) is None  # 期間内に記録なし
    assert writing_log.period_growth(writing_log.WritingLog(), date(2026, 9, 21), date(2026, 9, 28), WEEK_DAY) is None
    first = writing_log.period_growth(writing(), date(2026, 9, 1), date(2026, 9, 28), WEEK_DAY)  # 期間の前の記録が無い → 最初の記録から
    assert first["base_day"] == date(2026, 9, 14) and first["total_added"] == 900


def test_period_growth_ignores_decreases():
    log = writing()
    log.snaps.append(writing_log.Snapshot(datetime(2026, 9, 28, 23, 0), {"A": 1500, "B": 400}))  # 推敲で減った
    g = writing_log.period_growth(log, date(2026, 9, 28), date(2026, 10, 5), date(2026, 9, 28))
    assert g["total_added"] == 0 and all(w["added"] == 0 for w in g["works"])


def progress_data():
    proj = {"novel": recs({"日時": "2026-09-22 10:00", "作品名": "X", "種別": "進捗", "内容": "3章まで"}, {"日時": "2026-09-23 10:00", "作品名": "X", "種別": "アイデア", "内容": "案"},
                          {"日時": "2026-09-24 10:00", "作品名": "Y", "種別": "設定", "内容": "設定"}, {"日時": "2026-09-24 11:00", "作品名": "X", "種別": "相談", "内容": "質問"},
                          {"日時": "2026-09-01 10:00", "作品名": "X", "種別": "進捗", "内容": "期間外"}),
            "trpg": [], "others": recs({"日時": "2026-09-25 10:00", "作品名": "", "種別": "進捗", "内容": "開発"})}
    return report.Data(writing=writing(), tasks_done=5, projects=proj)


def test_section_progress():
    lines = report.section_progress(progress_data(), report.weekly_period(WEEK_DAY), WEEK_DAY)
    assert lines == ["執筆: 合計 +800文字（記録 09/20〜09/27）",
                     "　A +800（累計 1,800 / 目標 10,000・達成 18%・締切まで10日）",
                     "完了したタスク 5件",
                     "小説のプロジェクト: 3件の記録（X 2・Y 1）",  # 相談は数えない
                     "その他の制作のプロジェクト: 1件の記録（（作品名なし） 1）"]


def test_section_progress_is_neutral_when_nothing_grew():
    d = report.Data(writing=writing(), tasks_done=0)
    lines = report.section_progress(d, report.weekly_period(date(2026, 10, 4)), date(2026, 10, 4))
    assert lines[0] == "執筆記録シートに、この期間の記録はありません" and lines[1] == "完了したタスク 0件"
    d.writing.snaps.append(writing_log.Snapshot(datetime(2026, 10, 3, 23, 0), {"A": 1800, "B": 500}))
    assert "増加は見当たりませんでした" in report.section_progress(d, report.weekly_period(date(2026, 10, 4)), date(2026, 10, 4))[0]
    assert report.section_progress(report.Data(), report.weekly_period(WEEK_DAY), WEEK_DAY) == []  # 未設定・未取得なら何も出さない


# ---------------------------------------------------------------- ④ スクラップの再浮上


def articles():
    return recs({"日時": "2026-08-01 10:00", "タイトル": "古い記事", "URL": "https://e.com/a", "3行要約": "要約A", "タグ": "#小説"},
                {"日時": "2026-09-10 10:00", "タイトル": "先月末の記事", "URL": "https://e.com/b", "3行要約": "要約B", "タグ": "#TRPG"},
                {"日時": "2026-09-23 10:00", "タイトル": "今週の記事", "URL": "https://e.com/c", "3行要約": "要約C", "タグ": ""},
                {"日時": "2026-07-01 10:00", "タイトル": "", "URL": "https://e.com/d", "3行要約": "", "タグ": ""})


def test_scrap_candidates_are_older_articles_newest_first():
    d = report.Data(articles=articles())
    assert [c["タイトル"] for c in report.scrap_candidates(d, report.weekly_period(WEEK_DAY))] == ["先月末の記事", "古い記事"]


def test_clean_scrap_pick_validates():
    cands = [{"タイトル": "A", "URL": "u"}, {"タイトル": "B", "URL": ""}]
    assert report.clean_scrap_pick({"index": 2, "reason": "関連"}, cands) == {"title": "B", "url": "", "reason": "関連"}
    for bad in [{"index": None}, {"index": 0}, {"index": 3}, {"index": True}, {"index": "1"}, "壊れた", None, {}]:
        assert report.clean_scrap_pick(bad, cands) is None


def fake_ai(monkeypatch, answer):
    calls = []

    async def fake(prompt, fallback, **kw):
        calls.append(prompt)
        return answer(prompt) if callable(answer) else answer

    monkeypatch.setattr(claude_client, "complete_json", fake)
    return calls


def test_pick_scrap_sends_only_sheet_data_and_returns_the_choice(monkeypatch):
    calls = fake_ai(monkeypatch, {"index": 1, "reason": "設定の参考になります"})
    d = progress_data()
    d.articles = articles()
    pick = run(report.pick_scrap(d, report.weekly_period(WEEK_DAY)))
    assert pick == {"title": "先月末の記事", "url": "https://e.com/b", "reason": "設定の参考になります"}
    assert "小説「X」" in calls[0] and "1. 先月末の記事｜#TRPG｜要約B" in calls[0] and "今週の記事" not in calls[0]


def test_pick_scrap_skips_ai_without_projects_or_articles(monkeypatch):
    calls = fake_ai(monkeypatch, {"index": 1})
    p = report.weekly_period(WEEK_DAY)
    assert run(report.pick_scrap(report.Data(articles=articles()), p)) is None  # 進行中のプロジェクトが無い
    assert run(report.pick_scrap(progress_data(), p)) is None  # 記事が無い
    assert calls == []


def test_pick_scrap_ai_failure_gives_none(monkeypatch):
    fake_ai(monkeypatch, {})  # complete_json のフォールバック値
    d = progress_data()
    d.articles = articles()
    assert run(report.pick_scrap(d, report.weekly_period(WEEK_DAY))) is None


def test_section_scrap():
    assert report.section_scrap(None) == []
    assert report.section_scrap({"title": "T", "url": "https://x", "reason": "理由"}) == ["📰 T\nhttps://x\n理由"]
    assert report.section_scrap({"title": "T", "url": "", "reason": ""}) == ["📰 T"]


# ---------------------------------------------------------------- 組み立て


def test_render_text_and_note():
    p = report.weekly_period(WEEK_DAY)
    secs = [("体調・感情", ["ご機嫌度の平均 3.3（4日分）"]), ("空の節", []),
            ("執筆", ["執筆: 合計 +800文字", f"{report.SUB}A +800（累計 1,800）"]), ("スクラップの再浮上", ["📰 T\nhttps://x\n理由"])]
    rep = report.render(p, secs, ["家計簿"], WEEK_DAY)
    assert rep.text == ("📈 週次レポート（9/21〜9/27）\n\n■ 体調・感情\n・ご機嫌度の平均 3.3（4日分）\n\n"
                        "■ 執筆\n・執筆: 合計 +800文字\n　A +800（累計 1,800）\n\n■ スクラップの再浮上\n・📰 T\nhttps://x\n理由\n\n"
                        "※読み取れなかったため省略したデータ: 家計簿")
    assert rep.note == ("# 週次レポート 9/21〜9/27\n\n発行日: 2026-09-27\n\n## 体調・感情\n- ご機嫌度の平均 3.3（4日分）\n\n"
                        "## 執筆\n- 執筆: 合計 +800文字\n  - A +800（累計 1,800）\n\n## スクラップの再浮上\n- 📰 T\n  https://x\n  理由\n\n"
                        "> 読み取れなかったため省略したデータ: 家計簿\n")
    assert "空の節" not in rep.text and "空の節" not in rep.note


def test_report_has_no_advice_or_judgement_words():
    d = money_data()
    d.diary, d.health = health_data().diary, health_data().health
    d.writing, d.tasks_done, d.projects = writing(), 0, progress_data().projects
    rep = report.render(report.monthly_period(MONTH_DAY), report.build_sections(d, report.monthly_period(MONTH_DAY), MONTH_DAY, None), [], MONTH_DAY)
    for word in ("しましょう", "べき", "ください", "残念", "使いすぎ", "頑張", "怠"):
        assert word not in rep.text, word


def test_generate_builds_from_collected_data(monkeypatch):
    d = progress_data()
    d.household, d.ledger, d.articles = money_data().household, money_data().ledger, articles()
    d.missing = ["日記"]
    monkeypatch.setattr(report, "collect", lambda p, today: d)
    fake_ai(monkeypatch, {"index": 2, "reason": "参考になります"})
    rep = run(report.generate("weekly", WEEK_DAY))
    assert rep.period.filename == "Weekly_2026-09-27" and rep.text.startswith("📈 週次レポート（9/21〜9/27）")
    assert "今週の娯楽費 4,500円" in rep.text and "📰 古い記事" in rep.text and "※読み取れなかったため省略したデータ: 日記" in rep.text


def test_generate_survives_scrap_failure(monkeypatch):
    monkeypatch.setattr(report, "collect", lambda p, today: progress_data())

    async def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(report, "pick_scrap", boom)
    rep = run(report.generate("monthly", MONTH_DAY))
    assert "■ お金" in rep.text and "スクラップの再浮上" not in rep.text


def test_report_updates_the_health_manual_and_says_so(monkeypatch, manual):
    monkeypatch.setattr(report, "collect", lambda p, today: progress_data())
    rep = run(report.generate("weekly", WEEK_DAY))
    assert manual.calls == [WEEK_DAY] and report.MANUAL_UPDATED in rep.text and report.MANUAL_UPDATED in rep.note
    assert rep.text.index("■ 体調・感情") < rep.text.index(report.MANUAL_UPDATED) < rep.text.index("■ お金")  # 体調・感情の節に書く


def test_manual_is_updated_once_a_day_even_if_weekly_and_monthly_overlap(monkeypatch, manual):
    monkeypatch.setattr(report, "collect", lambda p, today: progress_data())
    day = date(2026, 5, 31)  # 日曜かつ月末日
    weekly, monthly = run(report.generate("weekly", day)), run(report.generate("monthly", day))
    assert manual.calls == [day] and report.MANUAL_UPDATED in weekly.text and report.MANUAL_ALREADY in monthly.text


def test_manual_failure_does_not_stop_the_report_and_is_retried_next_time(monkeypatch, manual):
    monkeypatch.setattr(report, "collect", lambda p, today: progress_data())
    manual.kv["fail"] = True
    rep = run(report.generate("weekly", WEEK_DAY))
    assert report.MANUAL_SKIPPED in rep.text and "■ お金" in rep.text  # レポートは出す。責めずに、見送ったことだけ書く
    assert f"manual_updated:{WEEK_DAY}" not in manual.kv  # 成功していないので、済みにしない
    manual.kv["fail"] = False
    assert report.MANUAL_UPDATED in run(report.generate("weekly", WEEK_DAY)).text


def test_preview_does_not_update_the_manual(monkeypatch, manual):
    monkeypatch.setattr(report, "collect", lambda p, today: progress_data())
    rep = run(report.generate("weekly", WEEK_DAY, update_manual=False))
    assert manual.calls == [] and "取扱マニュアル" not in rep.text


def test_collect_reports_unreadable_data_and_continues(monkeypatch):
    def records(name):
        if name == sheets.HOUSEHOLD:
            raise RuntimeError("quota")
        return []

    monkeypatch.setattr(sheets, "records", records)
    monkeypatch.setattr(sheets, "completed_count", lambda s, e: 3)
    monkeypatch.setattr(writing_log, "fetch", lambda: None)
    d = report.collect(report.weekly_period(WEEK_DAY), WEEK_DAY)
    assert d.missing == ["家計簿"] and d.tasks_done == 3 and d.writing is None and set(d.projects) == {"novel", "trpg", "others"}


# ---------------------------------------------------------------- 保存


@pytest.fixture
def store(tmp_path, monkeypatch):
    (tmp_path / "06-Life-OS" / "04-report").mkdir(parents=True)
    s = notes.GuardedStore(notes.LocalStore(tmp_path))
    monkeypatch.setattr(notes, "get_store", lambda: s)
    return NS(root=tmp_path, store=s)


def make_report():
    return report.Report(report.weekly_period(WEEK_DAY), "本文テキスト", "# ノート\n")


def test_save_writes_note_and_sheet(store, monkeypatch):
    rows = []
    monkeypatch.setattr(sheets, "append", lambda name, data: rows.append((name, data)) or 2)
    assert report.save(make_report(), WEEK_DAY) == []
    assert (store.root / vault_paths.report("Weekly_2026-09-27")).read_text(encoding="utf-8") == "# ノート\n"
    assert rows == [("04-report", {"発行日": "2026-09-27", "種別": "週次", "本文": "本文テキスト"})]
    written = {p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file()}
    assert written == {"06-Life-OS/04-report/Weekly_2026-09-27.md"}  # 書き込み先は 04-report の中だけ


def boom(*a, **k):
    raise OSError("down")


def test_note_failure_still_saves_the_sheet(store, monkeypatch):
    rows = []
    monkeypatch.setattr(store.store, "write", boom)
    monkeypatch.setattr(sheets, "append", lambda name, data: rows.append(data) or 2)
    assert report.save(make_report(), WEEK_DAY) == ["ノート"] and len(rows) == 1


def test_sheet_failure_still_saves_the_note(store, monkeypatch):
    monkeypatch.setattr(sheets, "append", boom)
    assert report.save(make_report(), WEEK_DAY) == ["シート"]
    assert (store.root / vault_paths.report("Weekly_2026-09-27")).exists()


def test_sheet_text_is_capped(store, monkeypatch):
    rows = []
    monkeypatch.setattr(sheets, "append", lambda name, data: rows.append(data) or 2)
    report.save(report.Report(report.weekly_period(WEEK_DAY), "あ" * 40_000, "n"), WEEK_DAY)
    assert len(rows[0]["本文"]) == report.MAX_SHEET_TEXT


# ---------------------------------------------------------------- 投稿（scheduler）


class FakeChannel:
    def __init__(self, history=()):
        self.sent, self._history = [], list(history)

    async def send(self, text, **k):
        self.sent.append(text)
        return NS(id=len(self.sent))

    def history(self, limit=30):
        async def gen():
            for m in self._history:
                yield m

        return gen()


@pytest.fixture
def post_env(monkeypatch):
    done = set()
    e = NS(ch=FakeChannel(), saved=[], gen_calls=[], fail_save=[])
    monkeypatch.setattr(state, "mark_done", lambda key: (key not in done) and (done.add(key) or True))
    monkeypatch.setattr(scheduler, "_client", NS(user=NS(id=99)))
    monkeypatch.setattr(scheduler, "find_channel", lambda key: e.ch)
    monkeypatch.setattr(util, "today", lambda: WEEK_DAY)

    async def gen(kind, day):
        e.gen_calls.append((kind, day))
        return report.Report(report.period_for(kind, day), f"{report.period_for(kind, day).prefix}（本文）", "note")

    monkeypatch.setattr(report, "generate", gen)
    monkeypatch.setattr(report, "save", lambda rep, day: e.saved.append(rep.period.filename) or list(e.fail_save))
    return e


def test_post_report_posts_saves_and_does_not_repeat(post_env):
    assert run(scheduler.post_report("weekly")) is True
    assert post_env.ch.sent == ["📈 週次レポート（本文）"] and post_env.saved == ["Weekly_2026-09-27"]
    assert run(scheduler.post_report("weekly")) is False  # 同じ日に二重投稿しない
    assert len(post_env.ch.sent) == 1 and len(post_env.gen_calls) == 1


def test_weekly_and_monthly_on_the_same_day_are_independent(post_env, monkeypatch):
    monkeypatch.setattr(util, "today", lambda: date(2026, 5, 31))
    assert run(scheduler.post_report("weekly")) and run(scheduler.post_report("monthly"))
    assert [c[0] for c in post_env.gen_calls] == ["weekly", "monthly"]


def test_post_report_notes_a_failed_save_in_the_post(post_env):
    post_env.fail_save = ["ノート"]
    run(scheduler.post_report("weekly"))
    assert post_env.ch.sent == ["📈 週次レポート（本文）\n\n※ノートへの保存に失敗しました。"]


def test_post_report_skips_when_another_bot_already_posted(post_env):
    post_env.ch = FakeChannel([NS(author=NS(id=99), content="📈 週次レポート（9/21〜9/27）\n...", created_at=datetime(2026, 9, 27, 11, 0, tzinfo=config.TZ))])
    assert run(scheduler.post_report("weekly")) is False and post_env.gen_calls == []


def test_post_report_force_ignores_the_done_mark(post_env):
    run(scheduler.post_report("weekly"))
    assert run(scheduler.post_report("weekly", force=True)) is True and len(post_env.ch.sent) == 2


def test_post_report_without_channel(post_env, monkeypatch):
    monkeypatch.setattr(scheduler, "find_channel", lambda key: None)
    assert run(scheduler.post_report("weekly")) is False and post_env.gen_calls == []


def test_report_job_posts_what_is_due_and_survives_failures(post_env, monkeypatch):
    monkeypatch.setattr(util, "today", lambda: date(2026, 5, 31))
    called = []

    async def flaky(kind, force=False):
        called.append(kind)
        if kind == "weekly":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(scheduler, "post_report", flaky)
    run(scheduler.report_job.coro())  # 週次が失敗しても、月次は投稿する
    assert called == ["weekly", "monthly"]
    called.clear()
    monkeypatch.setattr(util, "today", lambda: date(2026, 9, 28))
    run(scheduler.report_job.coro())
    assert called == []  # 平日は何もしない

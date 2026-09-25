import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import sheets
import util
from handlers import household as hh

TODAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=config.TZ)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 純粋関数: 1行の読み取り


def test_parse_basic_lines():
    e = hh.parse_line("現金 カフェ 800円", TODAY)
    assert (e.day, e.kind, e.genre, e.note, e.amount) == (TODAY, "現金", "カフェ", "", 800)
    e = hh.parse_line("娯楽 ガチャ 3,000円", TODAY)
    assert (e.kind, e.genre, e.amount) == ("娯楽", "ガチャ", 3000)


def test_parse_variants():
    assert hh.parse_line("娯楽　漫画　１，５００円", TODAY).amount == 1500  # 全角
    assert hh.parse_line("娯楽 漫画 1500", TODAY).amount == 1500  # 円なし
    assert hh.parse_line("現金：カフェ 800円", TODAY).genre == "カフェ"
    e = hh.parse_line("現金 カフェ ラテとケーキ 950円", TODAY)
    assert (e.genre, e.note, e.amount) == ("カフェ", "ラテとケーキ", 950)
    e = hh.parse_line("現金 800円 カフェ", TODAY)  # 金額が途中にあってもよい
    assert (e.genre, e.amount) == ("カフェ", 800)
    e = hh.parse_line("現金 800円", TODAY)
    assert e.genre == "" and e.amount == 800  # ジャンルは推測しない


def test_parse_dates():
    assert hh.parse_line("昨日 現金 カフェ 800円", TODAY).day == date(2026, 9, 24)
    assert hh.parse_line("一昨日 現金 カフェ 800円", TODAY).day == date(2026, 9, 23)
    assert hh.parse_line("9/20 娯楽 ガチャ 500円", TODAY).day == date(2026, 9, 20)
    assert hh.parse_line("2026年9月1日 娯楽 ガチャ 500円", TODAY).day == date(2026, 9, 1)
    assert hh.parse_line("12/30 現金 カフェ 500円", date(2027, 1, 3)).day == date(2026, 12, 30)  # 年をまたぐ
    assert hh.parse_line("2/30 現金 カフェ 500円", TODAY) is None  # 無効な日付は推測しない


def test_parse_rejects_unreadable_lines():
    for bad in ["カフェ 800円", "現金 カフェ", "現金 カフェ 0円", "娯楽 ガチャ 999999999999円", "こんにちは", "", "クレカ 楽天 5,000円"]:
        assert hh.parse_line(bad, TODAY) is None, bad


def test_clean_ai_entries_validates():
    lines = ["カフェで800円 現金", "よくわからない"]
    raw = [{"line": 1, "kind": "現金", "genre": "カフェ", "note": "", "amount": 800, "date": None},
           {"line": 2, "kind": "その他", "genre": "x", "amount": 100},  # 区分が違う
           {"line": 2, "kind": "娯楽", "genre": "x", "amount": -5},  # 金額が違う
           {"line": 9, "kind": "娯楽", "genre": "x", "amount": 100},  # 行番号が範囲外
           {"line": 2, "kind": "娯楽", "genre": "x", "amount": "百円"},
           "壊れた要素"]
    entries, matched = hh.clean_ai_entries(raw, TODAY, lines)
    assert [(e.kind, e.amount, e.line) for e in entries] == [("現金", 800, "カフェで800円 現金")] and matched == {0}
    assert hh.clean_ai_entries(None, TODAY, lines) == ([], set()) and hh.clean_ai_entries("壊れた応答", TODAY, lines) == ([], set())
    e, _ = hh.clean_ai_entries([{"line": 1, "kind": "娯楽", "genre": "ガチャ", "amount": 500, "date": "2026-09-01"}], TODAY, lines)
    assert e[0].day == date(2026, 9, 1)


# ---------------------------------------------------------------- 純粋関数: 集計と返信


def rec(day, amount, kind):
    return (2, {"日付": day, "金額（円）": amount, "ジャンル": "", "備考": "", "区分": kind})


def test_month_total_counts_only_leisure_of_that_month():
    recs = [rec("2026-09-01", "3000", "娯楽"), rec("2026-09-30", "1,500", "娯楽"), rec("2026-09-10", "800", "現金"),
            rec("2026-08-31", "999", "娯楽"), rec("2026-10-01", "999", "娯楽"), rec("2026-09-05", "¥2,000", "娯楽"),
            rec("", "500", "娯楽"), rec("2026-09-06", "たくさん", "娯楽")]
    assert hh.month_total(recs, date(2026, 9, 1)) == 6500
    assert hh.month_total(recs, date(2026, 9, 1), "現金") == 800
    assert hh.month_total(recs, date(2026, 8, 1)) == 999
    assert hh.month_total([], date(2026, 9, 1)) == 0


def test_format_reply_is_plain_numbers():
    es = [hh.Entry(TODAY, "現金", "カフェ", "", 800), hh.Entry(date(2026, 9, 20), "娯楽", "ガチャ", "限定", 3000)]
    text = hh.format_reply(es, {date(2026, 9, 1): 7500}, TODAY, [])
    assert text == "📝 現金｜カフェ 800円\n📝 娯楽｜ガチャ 限定 3,000円（9/20）\n今月の娯楽費: 7,500円"
    two = hh.format_reply(es[:1], {date(2026, 9, 1): 7500, date(2026, 8, 1): 4000}, TODAY, [])
    assert "今月の娯楽費: 7,500円" in two and "8月の娯楽費: 4,000円" in two
    assert "累計は、今は出せませんでした" in hh.format_reply(es, None, TODAY, [])
    both = hh.format_reply(es[:1], {date(2026, 9, 1): 0}, TODAY, ["ふつうの文"])
    assert "読み取れなかった行（記録していません）: ふつうの文" in both and hh.HINT in both


def test_month_starts_this_month_first_then_entry_months():
    es = [hh.Entry(date(2026, 8, 30), "現金", "", "", 1), hh.Entry(TODAY, "現金", "", "", 1), hh.Entry(date(2026, 8, 1), "現金", "", "", 1)]
    assert hh.month_starts(es, TODAY) == [date(2026, 9, 1), date(2026, 8, 1)]


# ---------------------------------------------------------------- 処理（偽の Discord・AI・シート）


class Att:
    def __init__(self, filename, content_type, data=b"x"):
        self.filename, self.content_type, self.data, self.size = filename, content_type, data, len(data)

    async def read(self):
        return self.data


class Msg:
    def __init__(self, content="", attachments=None):
        self.content, self.attachments = content, attachments or []
        self.reactions, self.sent = [], []
        self.channel = NS(send=self._send)

    async def _send(self, text, **k):
        self.sent.append(text)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "06-Life-OS" / "06-household-accounts").mkdir(parents=True)
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(root=tmp_path, rows=[], ai_calls=[], fail_append_after=None, records_fail=False,
           ai=lambda prompt: {"entries": []})

    async def fake_ai(prompt, fallback, **kw):
        e.ai_calls.append(prompt)
        return e.ai(prompt)

    def fake_append(name, data):
        if e.fail_append_after is not None and len(e.rows) >= e.fail_append_after:
            raise RuntimeError("sheet down")
        e.rows.append((name, data))
        return len(e.rows) + 1

    def fake_records(name):
        if e.records_fail:
            raise RuntimeError("quota")
        return [(i + 2, d) for i, (_, d) in enumerate(e.rows)]

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(sheets, "append", fake_append)
    monkeypatch.setattr(sheets, "records", fake_records)
    monkeypatch.setattr(util, "now", lambda: NOW)
    return e


def test_records_and_replies_with_leisure_total(env):
    m = Msg("娯楽 ガチャ 3,000円")
    run(hh.handle(m))
    assert env.rows == [("06-household-accounts", {"日付": "2026-09-25", "金額（円）": 3000, "ジャンル": "ガチャ", "備考": "", "区分": "娯楽"})]
    assert m.reactions == ["📝"] and env.ai_calls == []  # 決まった書き方は AI を使わない
    assert m.sent == ["📝 娯楽｜ガチャ 3,000円\n今月の娯楽費: 3,000円"]


def test_cash_entry_still_shows_leisure_total(env):
    run(hh.handle(Msg("娯楽 漫画 1,500円")))
    m = Msg("現金 カフェ 800円")
    run(hh.handle(m))
    assert m.sent == ["📝 現金｜カフェ 800円\n今月の娯楽費: 1,500円"]  # 娯楽が無い投稿でも、累計を必ず添える
    assert env.rows[-1][1]["区分"] == "現金"


def test_total_accumulates_across_posts(env):
    run(hh.handle(Msg("娯楽 ガチャ 3000円")))
    m = Msg("娯楽 漫画 1500円")
    run(hh.handle(m))
    assert m.sent[0].endswith("今月の娯楽費: 4,500円")


def test_multiple_lines_in_one_post(env):
    m = Msg("現金 カフェ 800円\n娯楽 ガチャ 3,000円\n昨日 娯楽 漫画 500円")
    run(hh.handle(m))
    assert len(env.rows) == 3 and m.reactions == ["📝"] and len(m.sent) == 1
    assert "今月の娯楽費: 3,500円" in m.sent[0] and "（9/24）" in m.sent[0]


def test_past_month_entry_shows_both_totals(env):
    run(hh.handle(Msg("娯楽 ガチャ 1,000円")))
    m = Msg("8/30 娯楽 漫画 2,000円")
    run(hh.handle(m))
    assert "今月の娯楽費: 1,000円" in m.sent[0] and "8月の娯楽費: 2,000円" in m.sent[0]


def test_ai_reads_free_form_line(env):
    env.ai = lambda p: {"entries": [{"line": 1, "kind": "現金", "genre": "カフェ", "note": "", "amount": 800, "date": None}]}
    m = Msg("カフェで800円使った 現金で")
    run(hh.handle(m))
    assert len(env.ai_calls) == 1 and "1. カフェで800円使った 現金で" in env.ai_calls[0]
    assert env.rows[0][1]["金額（円）"] == 800 and env.rows[0][1]["区分"] == "現金" and m.reactions == ["📝"]


def test_ai_is_called_only_for_unreadable_lines(env):
    env.ai = lambda p: {"entries": [{"line": 1, "kind": "娯楽", "genre": "本", "amount": 700}]}
    run(hh.handle(Msg("現金 カフェ 800円\n今日は本を七百円で買った")))
    assert len(env.ai_calls) == 1 and "現金 カフェ 800円" not in env.ai_calls[0]
    assert len(env.rows) == 2


def test_unreadable_text_records_nothing_and_gently_hints(env):
    m = Msg("今日はいい天気だった")  # AI も読み取れない（フォールバック値）
    run(hh.handle(m))
    assert env.rows == [] and m.reactions == ["⚠️"]
    assert "読み取れなかった行（記録していません）: 今日はいい天気だった" in m.sent[0] and hh.HINT in m.sent[0]
    assert "累計" not in m.sent[0]


def test_partly_readable_post_records_the_readable_part(env):
    m = Msg("現金 カフェ 800円\nよくわからない行")
    run(hh.handle(m))
    assert len(env.rows) == 1 and m.reactions == ["📝", "⚠️"]
    assert "よくわからない行" in m.sent[0] and "今月の娯楽費: 0円" in m.sent[0]


def test_sheet_failure_reports_unrecorded_rows(env):
    env.fail_append_after = 1
    m = Msg("現金 カフェ 800円\n娯楽 ガチャ 3,000円")
    run(hh.handle(m))
    assert len(env.rows) == 1 and m.reactions == ["📝", "⚠️"]
    assert "記録できなかった行があります" in m.sent[0] and "娯楽 ガチャ 3,000円" in m.sent[0]


def test_sheet_failure_on_first_row_records_nothing(env):
    env.fail_append_after = 0
    m = Msg("娯楽 ガチャ 3,000円")
    run(hh.handle(m))
    assert env.rows == [] and m.reactions == ["⚠️"] and "記録できなかった行があります" in m.sent[0]


def test_total_failure_still_confirms_the_record(env):
    env.records_fail = True
    m = Msg("娯楽 ガチャ 3,000円")
    run(hh.handle(m))
    assert len(env.rows) == 1 and m.reactions == ["📝"] and "娯楽費の累計は、今は出せませんでした" in m.sent[0]


def test_screenshot_is_saved_silently_without_reading_amounts(env):
    m = Msg("", [Att("楽天家計簿.png", "image/png", b"\x89PNG")])
    run(hh.handle(m))
    saved = list((env.root / "06-Life-OS" / "06-household-accounts" / "attachments").glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"\x89PNG"
    assert m.reactions == ["📷"] and m.sent == [] and env.rows == [] and env.ai_calls == []


def test_screenshot_with_text_does_both(env):
    m = Msg("現金 カフェ 800円", [Att("s.jpg", "image/jpeg")])
    run(hh.handle(m))
    assert m.reactions == ["📷", "📝"] and len(env.rows) == 1 and len(m.sent) == 1


def test_unsavable_attachment_is_reported(env):
    m = Msg("", [Att("資料.pdf", "application/pdf", b"%PDF")])
    run(hh.handle(m))
    assert m.reactions == ["⚠️"] and "画像以外のファイル" in m.sent[0] and env.rows == []


def test_empty_message_is_ignored(env):
    m = Msg("  ")
    run(hh.handle(m))
    assert m.reactions == [] and m.sent == [] and env.rows == []


def test_only_the_household_room_paths_are_written(env):
    run(hh.handle(Msg("", [Att("a.png", "image/png")])))
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert written and all(p.startswith("06-Life-OS/06-household-accounts/") for p in written), written

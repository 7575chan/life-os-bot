import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import sheets
import util
from handlers import ledger as lg

TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=config.TZ)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 純粋関数: 種別と1行の読み取り


def test_detect_kind_only_when_unambiguous():
    assert lg.detect_kind("BOOTH売上") == "売上" and lg.detect_kind("印税") == "売上"
    assert lg.detect_kind("サーバー代") == "経費" and lg.detect_kind("文具の購入") == "経費"
    assert lg.detect_kind("ドメイン更新") is None  # どちらの言葉もない → 決めない
    assert lg.detect_kind("代行の報酬") is None  # 売上の言葉と経費の言葉の両方 → 決めない
    assert lg.detect_kind("") is None


def test_parse_examples_from_the_spec():
    e = lg.parse_line("10/5 サーバー代 1,200円", TODAY)
    assert (e.day, e.kind, e.content, e.amount) == (date(2026, 10, 5), "経費", "サーバー代", 1200)
    e = lg.parse_line("10/10 BOOTH売上 50,000円", TODAY)
    assert (e.day, e.kind, e.content, e.amount) == (date(2026, 10, 10), "売上", "BOOTH売上", 50000)


def test_parse_variants():
    e = lg.parse_line("経費 ドメイン更新 1,500円", TODAY)  # 種別を書き添えれば、言葉からは決められない内容でも記録できる
    assert (e.kind, e.content, e.day) == ("経費", "ドメイン更新", TODAY)
    assert lg.parse_line("売上 BOOTH 5000", TODAY).kind == "売上"  # 円なし
    assert lg.parse_line("昨日 サーバー代 １，２００円", TODAY).day == date(2026, 10, 11)  # 全角・相対日付
    assert lg.parse_line("経費 1,000円", TODAY).content == ""  # 種別があれば内容は空でもよい


def test_parse_rejects_what_it_cannot_decide():
    for bad in ["ドメイン更新 1,500円", "サーバー代", "サーバー代 0円", "10/5 BOOTH 50,000円", "こんにちは", "", "2/30 サーバー代 100円",
                "代行の報酬 3,000円"]:
        assert lg.parse_line(bad, TODAY) is None, bad


def test_parse_transcription():
    c = lg.parse_transcription("10月分転記した！", TODAY)
    assert c.month == date(2026, 10, 1) and c.cols == (lg.YAYOI, lg.ANALYSIS)
    assert lg.parse_transcription("やよいに9月分転記しました", TODAY).cols == (lg.YAYOI,)
    assert lg.parse_transcription("分析シートは先月分転記済み", TODAY).month == date(2026, 9, 1)
    assert lg.parse_transcription("今月分を転記した", TODAY).month == date(2026, 10, 1)
    assert lg.parse_transcription("やよいと分析に転記した", TODAY).month is None  # 月が無い → 聞き返す
    assert lg.parse_transcription("12月分転記した", TODAY).month == date(2025, 12, 1)  # 未来の月は去年
    assert lg.parse_transcription("転記しなきゃ", TODAY) is None  # 完了の報告ではない
    assert lg.parse_transcription("10/5 転記用の手数料 300円 払った", TODAY) is None  # 金額つきは記録の行
    assert lg.parse_transcription("10/5 サーバー代 1,200円", TODAY) is None


def test_clean_ai_entries_validates():
    lines = ["ドメインを更新して1500円払った", "ふつうの文"]
    raw = [{"line": 1, "kind": "経費", "content": "ドメイン更新", "amount": 1500, "date": None},
           {"line": 2, "kind": "その他", "content": "x", "amount": 100},
           {"line": 2, "kind": "売上", "content": "x", "amount": 0},
           {"line": 3, "kind": "売上", "content": "x", "amount": 100},
           {"line": 2, "kind": "売上", "content": "x", "amount": "千円"}, "壊れた要素"]
    entries, matched = lg.clean_ai_entries(raw, TODAY, lines)
    assert [(e.kind, e.amount, e.content) for e in entries] == [("経費", 1500, "ドメイン更新")] and matched == {0}
    assert lg.clean_ai_entries(None, TODAY, lines) == ([], set())


def test_clean_receipts_validates():
    raw = [{"image": 1, "date": "2026-10-01", "store": "文具店", "amount": 980, "kind": "経費", "note": "ペン"},
           {"image": 2, "date": None, "store": "", "amount": 500, "kind": "経費", "note": ""},
           {"image": 1, "amount": 100, "kind": None},  # 種別が決められない
           {"image": 3, "amount": 100, "kind": "経費"},  # 画像の番号が範囲外
           {"image": 1, "amount": -1, "kind": "経費"}, {"image": 1, "amount": 12.5, "kind": "経費"}]
    got = lg.clean_receipts(raw, TODAY, 2)
    assert [(r.image, r.amount, r.content, r.day) for r in got] == [(1, 980, "文具店 ペン", date(2026, 10, 1)), (2, 500, "レシート", TODAY)]
    assert lg.clean_receipts("壊れた", TODAY, 1) == []


def test_drop_duplicates():
    typed = [lg.Entry(TODAY, "経費", "文具", 980)]
    keep, dup = lg.drop_duplicates([lg.Entry(TODAY, "経費", "文具店", 980, image=1), lg.Entry(TODAY, "経費", "別の店", 500, image=2)], typed)
    assert [r.image for r in keep] == [2] and [r.image for r in dup] == [1]


# ---------------------------------------------------------------- 純粋関数: 集計と返信


def rec(day, kind, amount, y="", a=""):
    return {"日付": day, "種別": kind, "金額（円）": amount, "内容": "", lg.YAYOI: y, lg.ANALYSIS: a}


def recs(*rows):
    return [(i + 2, r) for i, r in enumerate(rows)]


def test_month_summary():
    rs = recs(rec("2026-10-01", "売上", "50,000"), rec("2026-10-31", "経費", "1200"), rec("2026-10-05", "経費", "¥800"),
              rec("2026-09-30", "売上", "999"), rec("2026-11-01", "経費", "999"), rec("", "売上", "1"), rec("2026-10-02", "売上", "たくさん"),
              rec("2026-10-03", "その他", "500"))
    assert lg.month_summary(rs, date(2026, 10, 1)) == (50000, 2000)
    assert lg.month_summary(rs, date(2026, 8, 1)) == (0, 0)


def test_unposted_counts_all_months_only_real_rows():
    rs = recs(rec("2026-10-01", "売上", "100"), rec("2026-09-01", "経費", "100", y="TRUE"), rec("2026-09-02", "経費", "100", y="TRUE", a="TRUE"),
              rec("2026-09-03", "経費", "100", y="済", a="✅"), rec("", "", ""), rec("2026-10-04", "売上", "たくさん"))
    assert lg.unposted_counts(rs) == (1, 2)


def test_plan_transcription_only_that_month_and_columns():
    rs = recs(rec("2026-10-01", "売上", "100"), rec("2026-10-02", "経費", "100", y="TRUE"), rec("2026-09-30", "経費", "100"),
              rec("2026-10-03", "経費", "100", y="TRUE", a="TRUE"))
    both = lg.plan_transcription(rs, lg.Transcription(date(2026, 10, 1), (lg.YAYOI, lg.ANALYSIS)))
    assert both == [(2, {lg.YAYOI: "TRUE", lg.ANALYSIS: "TRUE"}), (3, {lg.ANALYSIS: "TRUE"})]  # 済みの列は書き換えない
    only = lg.plan_transcription(rs, lg.Transcription(date(2026, 10, 1), (lg.YAYOI,)))
    assert only == [(2, {lg.YAYOI: "TRUE"})]
    assert lg.plan_transcription(rs, lg.Transcription(None, (lg.YAYOI,))) == []


def test_profit_text():
    assert lg.profit_text(50000, 1200) == "+48,800円" and lg.profit_text(0, 1200) == "-1,200円" and lg.profit_text(500, 500) == "±0円"


def test_format_reply_is_plain_numbers():
    es = [lg.Entry(TODAY, "経費", "サーバー代", 1200), lg.Entry(date(2026, 10, 5), "売上", "BOOTH売上", 50000, image=1)]
    text = lg.format_reply(es, [], {date(2026, 10, 1): (50000, 1200)}, (3, 2), TODAY, [])
    assert text == ("📝 経費｜サーバー代 1,200円\n🧾 売上｜BOOTH売上 50,000円（10/5）\n"
                    "今月の概算損益: +48,800円（売上 50,000円 − 経費 1,200円）\n未転記: やよい 3件 / 分析シート 2件（暇なときに移せば大丈夫です）")
    assert lg.format_reply(es[:1], [], {date(2026, 10, 1): (0, 0)}, (0, 0), TODAY, []).endswith("未転記: やよい 0件 / 分析シート 0件")
    two = lg.format_reply(es[:1], [], {date(2026, 10, 1): (0, 1200), date(2026, 9, 1): (10, 0)}, (0, 0), TODAY, [])
    assert "今月の概算損益: -1,200円" in two and "9月の概算損益: +10円" in two
    assert "今は出せませんでした" in lg.format_reply(es, [], None, None, TODAY, [])
    bad = lg.format_reply([], [], None, None, TODAY, ["ドメイン更新 1,500円"])
    assert "読み取れなかったもの（記録していません）: ドメイン更新 1,500円" in bad and lg.HINT in bad


# ---------------------------------------------------------------- 処理（偽の Discord・AI・Vision・シート）


class Att:
    def __init__(self, filename, content_type, data=b"x", size=None):
        self.filename, self.content_type, self.data = filename, content_type, data
        self.size = len(data) if size is None else size

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
    (tmp_path / "06-Life-OS" / "07-ledger").mkdir(parents=True)
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(root=tmp_path, rows=[], ai_calls=[], vision_calls=[], updates=[], fail_append_after=None, records_fail=False,
           update_fail=False, ai=lambda prompt: {"entries": []}, vision=lambda prompt, n: {"receipts": []})

    async def fake_ai(prompt, fallback, **kw):
        e.ai_calls.append(prompt)
        return e.ai(prompt)

    async def fake_vision(images, prompt, fallback, **kw):
        e.vision_calls.append((images, prompt))
        return e.vision(prompt, len(images))

    def fake_append(name, data):
        if e.fail_append_after is not None and len(e.rows) >= e.fail_append_after:
            raise RuntimeError("sheet down")
        row = {c: "" for c in sheets.SCHEMA[name]}
        row.update({k: (str(v) if k != "金額（円）" else v) for k, v in data.items()})
        e.rows.append(row)
        return len(e.rows) + 1

    def fake_records(name):
        if e.records_fail:
            raise RuntimeError("quota")
        return [(i + 2, dict(r)) for i, r in enumerate(e.rows)]

    def fake_update_rows(name, updates):
        if e.update_fail:
            raise RuntimeError("write quota")
        e.updates.append(updates)
        for row, changed in updates:
            e.rows[row - 2].update(changed)

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(claude_client, "vision_json", fake_vision)
    monkeypatch.setattr(sheets, "append", fake_append)
    monkeypatch.setattr(sheets, "records", fake_records)
    monkeypatch.setattr(sheets, "update_rows", fake_update_rows)
    monkeypatch.setattr(util, "now", lambda: NOW)
    return e


def test_records_expense_and_sale_with_profit_and_unposted(env):
    m = Msg("10/5 サーバー代 1,200円\n10/10 BOOTH売上 50,000円")
    run(hh_handle(m))
    assert [(r["日付"], r["種別"], r["金額（円）"], r["内容"]) for r in env.rows] == [
        ("2026-10-05", "経費", 1200, "サーバー代"), ("2026-10-10", "売上", 50000, "BOOTH売上")]
    assert env.rows[0][lg.YAYOI] == "" and env.rows[0][lg.ANALYSIS] == ""  # 未転記の状態で入る
    assert m.reactions == ["📝"] and env.ai_calls == [] and env.vision_calls == []
    assert len(m.sent) == 1 and "今月の概算損益: +48,800円（売上 50,000円 − 経費 1,200円）" in m.sent[0]
    assert "未転記: やよい 2件 / 分析シート 2件" in m.sent[0]


def hh_handle(m):
    return lg.handle(m)


def test_ambiguous_line_is_not_recorded_and_asks(env):
    m = Msg("ドメイン更新 1,500円")  # AI も決められない（フォールバック値）
    run(lg.handle(m))
    assert env.rows == [] and m.reactions == ["⚠️"]
    assert "読み取れなかったもの（記録していません）: ドメイン更新 1,500円" in m.sent[0] and lg.HINT in m.sent[0]


def test_ai_reads_free_form_line(env):
    env.ai = lambda p: {"entries": [{"line": 1, "kind": "経費", "content": "ドメイン更新", "amount": 1500, "date": None}]}
    m = Msg("ドメインを更新して1500円払った")
    run(lg.handle(m))
    assert len(env.ai_calls) == 1 and env.rows[0]["種別"] == "経費" and env.rows[0]["金額（円）"] == 1500 and m.reactions == ["📝"]


def test_ai_only_gets_unreadable_lines(env):
    env.ai = lambda p: {"entries": [{"line": 1, "kind": "売上", "content": "投げ銭", "amount": 300}]}
    run(lg.handle(Msg("10/5 サーバー代 1,200円\n投げ銭が300円届いた")))
    assert len(env.ai_calls) == 1 and env.ai_calls[0].endswith("行:\n1. 投げ銭が300円届いた") and len(env.rows) == 2


def test_partly_readable_post_records_the_readable_part(env):
    m = Msg("10/5 サーバー代 1,200円\nよくわからない行")
    run(lg.handle(m))
    assert len(env.rows) == 1 and m.reactions == ["📝", "⚠️"] and "よくわからない行" in m.sent[0]


def test_receipt_photo_is_saved_read_and_linked(env):
    env.vision = lambda p, n: {"receipts": [{"image": 1, "date": "2026-10-11", "store": "文具店", "amount": 980, "kind": "経費", "note": "ペン"}]}
    m = Msg("", [Att("レシート.jpg", "image/jpeg", b"JPEGDATA")])
    run(lg.handle(m))
    saved = list((env.root / "06-Life-OS" / "07-ledger" / "attachments").glob("*.jpg"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"JPEGDATA"
    row = env.rows[0]
    assert (row["日付"], row["種別"], row["金額（円）"]) == ("2026-10-11", "経費", 980)
    assert row["内容"] == f"文具店 ペン [レシート: {saved[0].name}]"
    assert m.reactions == ["📷", "📝"] and env.ai_calls == []
    assert "🧾 経費｜文具店 ペン 980円（10/11）" in m.sent[0]


def test_multiple_receipts_in_one_post(env):
    env.vision = lambda p, n: {"receipts": [{"image": 1, "amount": 500, "kind": "経費", "store": "A"},
                                            {"image": 2, "amount": 700, "kind": "経費", "store": "B"}]}
    m = Msg("", [Att("1.png", "image/png"), Att("2.png", "image/png")])
    run(lg.handle(m))
    assert [r["金額（円）"] for r in env.rows] == [500, 700] and len(env.vision_calls[0][0]) == 2
    names = sorted(p.name for p in (env.root / "06-Life-OS" / "07-ledger" / "attachments").glob("*.png"))
    assert names[0] in env.rows[0]["内容"] and names[1] in env.rows[1]["内容"]  # 画像ごとに対応する


def test_unreadable_receipt_is_reported_but_image_is_kept(env):
    m = Msg("", [Att("ぼやけ.jpg", "image/jpeg")])  # Vision が何も返さない
    run(lg.handle(m))
    assert env.rows == [] and m.reactions == ["📷", "⚠️"]
    assert "レシート画像（1枚目）" in m.sent[0]
    assert len(list((env.root / "06-Life-OS" / "07-ledger" / "attachments").glob("*.jpg"))) == 1


def test_text_with_photo_is_a_hint_for_vision(env):
    env.vision = lambda p, n: {"receipts": [{"image": 1, "amount": 3000, "kind": "売上", "store": "", "note": ""}]}
    m = Msg("BOOTHの入金", [Att("画面.png", "image/png")])
    run(lg.handle(m))
    assert "BOOTHの入金" in env.vision_calls[0][1] and env.ai_calls == []
    assert env.rows[0]["種別"] == "売上" and len(env.rows) == 1


def test_receipt_matching_typed_entry_is_not_recorded_twice(env):
    env.vision = lambda p, n: {"receipts": [{"image": 1, "date": "2026-10-12", "amount": 980, "kind": "経費", "store": "文具店"}]}
    m = Msg("文具代 980円", [Att("r.jpg", "image/jpeg")])
    run(lg.handle(m))
    assert len(env.rows) == 1 and env.rows[0]["内容"] == "文具代" and "同じなので追加していません" in m.sent[0]


def test_unusable_attachments_are_reported_without_vision(env):
    big = Att("big.png", "image/png", b"x", size=lg.MAX_IMAGE_BYTES + 1)
    pdf = Att("領収書.pdf", "application/pdf", b"%PDF")
    m = Msg("", [big, pdf])
    run(lg.handle(m))
    assert env.vision_calls == [] and env.rows == [] and m.reactions == ["⚠️"]
    assert "5MBを超えています" in m.sent[0] and "画像以外のファイル" in m.sent[0]


def test_at_most_five_images_are_sent_to_vision(env):
    m = Msg("", [Att(f"{i}.png", "image/png") for i in range(7)])
    run(lg.handle(m))
    assert len(env.vision_calls[0][0]) == lg.MAX_RECEIPTS


# ---- 転記チェック


def seed(env, *rows):
    env.rows += [dict(r) for r in rows]


def test_transcription_report_checks_that_month_only(env):
    seed(env, rec("2026-10-01", "売上", 100), rec("2026-10-05", "経費", 200), rec("2026-09-30", "経費", 300))
    m = Msg("10月分転記した！")
    run(lg.handle(m))
    assert [r[lg.YAYOI] for r in env.rows] == ["TRUE", "TRUE", ""] and [r[lg.ANALYSIS] for r in env.rows] == ["TRUE", "TRUE", ""]
    assert len(env.updates) == 1  # 1回の書き込みにまとめる
    assert m.reactions == ["✅"] and "✅ 10月分の やよい・分析 を転記済みにしました（2件）" in m.sent[0]
    assert "未転記: やよい 1件 / 分析シート 1件" in m.sent[0]  # 9月の行だけ残る
    assert env.ai_calls == [] and len(env.rows) == 3  # 記録の行としては扱わない


def test_transcription_one_column_by_keyword(env):
    seed(env, rec("2026-10-01", "売上", 100))
    run(lg.handle(Msg("やよいに10月分転記しました")))
    assert env.rows[0][lg.YAYOI] == "TRUE" and env.rows[0][lg.ANALYSIS] == ""


def test_transcription_without_month_asks_and_changes_nothing(env):
    seed(env, rec("2026-10-01", "売上", 100))
    m = Msg("転記した")
    run(lg.handle(m))
    assert env.updates == [] and m.reactions == ["⚠️"] and "何月分" in m.sent[0]


def test_transcription_with_nothing_to_do(env):
    seed(env, rec("2026-10-01", "売上", 100, y="TRUE", a="TRUE"))
    m = Msg("10月分転記した")
    run(lg.handle(m))
    assert env.updates == [] and "転記が済んでいない行" in m.sent[0] and m.reactions == []


def test_transcription_write_failure_is_reported(env):
    seed(env, rec("2026-10-01", "売上", 100))
    env.update_fail = True
    m = Msg("10月分転記した")
    run(lg.handle(m))
    assert env.rows[0][lg.YAYOI] == "" and m.reactions == ["⚠️"] and "書き込めませんでした" in m.sent[0]


def test_transcription_when_sheet_unreadable(env):
    env.records_fail = True
    m = Msg("10月分転記した")
    run(lg.handle(m))
    assert env.updates == [] and m.reactions == ["⚠️"] and "反映できませんでした" in m.sent[0]


def test_entry_and_transcription_in_one_post(env):
    seed(env, rec("2026-09-01", "経費", 100))
    m = Msg("9月分転記した\n10/5 サーバー代 1,200円")
    run(lg.handle(m))
    assert env.rows[0][lg.YAYOI] == "TRUE" and len(env.rows) == 2
    assert m.reactions == ["📝", "✅"] and "9月の概算損益: -100円" in m.sent[0] and "今月の概算損益: -1,200円" in m.sent[0]
    assert "未転記: やよい 1件 / 分析シート 1件" in m.sent[0]  # 新しい行だけ


# ---- 失敗と安全


def test_sheet_failure_reports_unrecorded_rows(env):
    env.fail_append_after = 1
    m = Msg("10/5 サーバー代 1,200円\n10/10 BOOTH売上 50,000円")
    run(lg.handle(m))
    assert len(env.rows) == 1 and m.reactions == ["📝", "⚠️"]
    assert "記録できなかったものがあります" in m.sent[0] and "BOOTH売上 50,000円" in m.sent[0]


def test_total_failure_still_confirms_the_record(env):
    env.records_fail = True
    m = Msg("10/5 サーバー代 1,200円")
    run(lg.handle(m))
    assert len(env.rows) == 1 and m.reactions == ["📝"] and "今は出せませんでした" in m.sent[0]


def test_empty_message_is_ignored(env):
    m = Msg("  ")
    run(lg.handle(m))
    assert m.reactions == [] and m.sent == [] and env.rows == []


def test_only_the_ledger_room_paths_are_written(env):
    env.vision = lambda p, n: {"receipts": [{"image": 1, "amount": 100, "kind": "経費"}]}
    run(lg.handle(Msg("", [Att("a.png", "image/png")])))
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert written and all(p.startswith("06-Life-OS/07-ledger/") for p in written), written
